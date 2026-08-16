<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Listener;

use OCA\NcMigaduPasswordSync\Service\PasswordSyncProvider;
use OCA\NcMigaduPasswordSync\Service\SyncException;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\User\Events\PasswordUpdatedEvent;
use OCP\User\Events\UserCreatedEvent;
use Psr\Log\LoggerInterface;

/**
 * Hooks the password-change funnel (PasswordUpdatedEvent — personal settings
 * GUI, admin reset, occ user:resetpassword, email reset link) and user
 * creation with a password (UserCreatedEvent), pushing the new password to
 * the mailbox.
 *
 * Never lets a sync failure break the password change itself: on error the
 * NC password stands, the mailbox keeps the previous password (graceful
 * divergence, see the runbook), and the error is logged with user context.
 * The password itself is never logged.
 */
class PasswordSyncListener implements IEventListener {
	public function __construct(
		private PasswordSyncProvider $provider,
		private LoggerInterface $logger,
	) {
	}

	public function handle(Event $event): void {
		$password = null;
		$user = null;

		if ($event instanceof PasswordUpdatedEvent) {
			$user = $event->getUser();
			$password = $event->getPassword();
		} elseif ($event instanceof UserCreatedEvent && $event->getPassword() !== null) {
			$user = $event->getUser();
			$password = $event->getPassword();
		}

		if ($user === null || $password === null) {
			return;
		}

		$uid = $user->getUID();

		try {
			$this->provider->syncPassword($user, $password);
		} catch (SyncException $e) {
			// Expected, controlled failure (API down, bad credentials, mailbox missing).
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
}
