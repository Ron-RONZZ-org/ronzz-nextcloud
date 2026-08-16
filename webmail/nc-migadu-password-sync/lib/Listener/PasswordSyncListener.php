<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Listener;

use OCA\NcMigaduPasswordSync\Service\PasswordSyncProvider;
use OCA\NcMigaduPasswordSync\Service\SyncException;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\IUser;
use OCP\User\Events\BeforeUserDeletedEvent;
use OCP\User\Events\PasswordUpdatedEvent;
use OCP\User\Events\UserCreatedEvent;
use OCP\User\Events\UserDeletedEvent;
use Psr\Log\LoggerInterface;

/**
 * Hooks the NC user lifecycle into the mail-provider mailbox lifecycle:
 *
 *  - PasswordUpdatedEvent (personal settings GUI, admin reset,
 *    occ user:resetpassword, email reset link) and UserCreatedEvent-with-
 *    password → push the new password; the mailbox is created on the fly if
 *    it does not exist yet, so adding a user also adds their mailbox (the
 *    Migadu API requires a password on creation — a user created without
 *    one gets its mailbox on the first password change instead).
 *  - BeforeUserDeletedEvent → capture which mailbox belongs to the user
 *    (after deletion the email address is no longer readable).
 *  - UserDeletedEvent → delete the captured mailbox, mirroring the removal.
 *
 * Never lets a sync failure break the NC operation itself: on error the
 * password change / user deletion stands, the divergence is logged at error
 * level with user context (the password itself is never logged), and the
 * next password change or a manual re-create retries.
 */
class PasswordSyncListener implements IEventListener {
	/**
	 * uid → mailbox captured before the user got deleted; null = the user
	 * does not qualify for a mailbox. The same listener instance serves both
	 * deletion events within one process (the server container caches it).
	 *
	 * @var array<string, array{local: string, domain: string}|null>
	 */
	private array $pendingDeletions = [];

	public function __construct(
		private PasswordSyncProvider $provider,
		private LoggerInterface $logger,
	) {
	}

	public function handle(Event $event): void {
		if ($event instanceof BeforeUserDeletedEvent) {
			$this->captureMailboxForDeletion($event->getUser());
			return;
		}

		if ($event instanceof UserDeletedEvent) {
			$this->deleteCapturedMailbox($event->getUser()->getUID());
			return;
		}

		$pair = $this->userWithNewPassword($event);
		if ($pair === null) {
			return;
		}
		[$user, $password] = $pair;
		$uid = $user->getUID();

		try {
			$this->provider->syncPassword($user, $password);
		} catch (SyncException $e) {
			// Expected, controlled failure (API down, bad credentials, weak password).
			$this->logger->error('migadu_sync: password sync FAILED for user {uid}: {message}', [
				'uid' => $uid,
				'message' => $e->getMessage(),
			]);
		} catch (\Throwable $e) {
			// Defensive: an unexpected bug must still not break the password change.
			$this->logger->error('migadu_sync: unexpected error while syncing password for user {uid}: {message}', [
				'uid' => $uid,
				'message' => $e->getMessage(),
				'exception' => $e,
			]);
		}
	}

	/**
	 * A user with a new password (the single funnel for all password-change
	 * paths and for user creation with a password), or null.
	 *
	 * @return array{IUser, string}|null [user, new password]
	 */
	private function userWithNewPassword(Event $event): ?array {
		if ($event instanceof PasswordUpdatedEvent) {
			return [$event->getUser(), $event->getPassword()];
		}
		if ($event instanceof UserCreatedEvent && $event->getPassword() !== null) {
			return [$event->getUser(), $event->getPassword()];
		}
		return null;
	}

	private function captureMailboxForDeletion(IUser $user): void {
		$uid = $user->getUID();
		try {
			$this->pendingDeletions[$uid] = $this->provider->resolveMailbox($user);
		} catch (SyncException $e) {
			// Hard to resolve (e.g. malformed email) — record "no mailbox" so the
			// deletion below is a no-op; the user deletion itself must not break.
			$this->logger->warning('migadu_sync: could not resolve mailbox for user {uid} before deletion: {message}', [
				'uid' => $uid,
				'message' => $e->getMessage(),
			]);
			$this->pendingDeletions[$uid] = null;
		}
	}

	private function deleteCapturedMailbox(string $uid): void {
		if (!array_key_exists($uid, $this->pendingDeletions)) {
			// Should not happen (both events fire together), but never crash the deletion.
			$this->logger->warning(
				'migadu_sync: UserDeletedEvent for {uid} without a matching BeforeUserDeletedEvent — mailbox deletion skipped',
				['uid' => $uid]
			);
			return;
		}

		$mailbox = $this->pendingDeletions[$uid];
		unset($this->pendingDeletions[$uid]);

		if ($mailbox === null) {
			return; // did not qualify — no mailbox to delete
		}

		try {
			$this->provider->deleteMailbox($uid, $mailbox['local'], $mailbox['domain']);
		} catch (SyncException $e) {
			$this->logger->error('migadu_sync: mailbox deletion FAILED for user {uid}: {message}', [
				'uid' => $uid,
				'message' => $e->getMessage(),
			]);
		} catch (\Throwable $e) {
			$this->logger->error('migadu_sync: unexpected error while deleting the mailbox of user {uid}: {message}', [
				'uid' => $uid,
				'message' => $e->getMessage(),
				'exception' => $e,
			]);
		}
	}
}
