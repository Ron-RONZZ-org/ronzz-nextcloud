<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Service;

use OCP\IUser;

/**
 * Syncs a Nextcloud user's account lifecycle to their mail-provider mailbox:
 * the mailbox password (unified login), mailbox creation on user creation,
 * and mailbox deletion when the user is removed.
 *
 * Implementations must never log or embed the password itself.
 *
 * @see SyncException thrown when a mailbox operation could not be performed
 */
interface PasswordSyncProvider {
	/**
	 * Push the user's new password to their mailbox, creating the mailbox
	 * first if it does not exist yet.
	 *
	 * Implementations decide which users qualify (e.g. only addresses on
	 * the configured mail domain); non-qualifying users are a no-op.
	 *
	 * @throws SyncException when the mailbox password could not be synced
	 */
	public function syncPassword(IUser $user, string $password): void;

	/**
	 * Resolve the mailbox that belongs to a user, if any.
	 *
	 * Must NOT perform API calls; used by the listener to capture the
	 * mailbox identity *before* the user is deleted from Nextcloud (after
	 * deletion the email address is no longer readable).
	 *
	 * @return array{local: string, domain: string}|null the mailbox local
	 *         part and mail domain, or null when the user has no mailbox
	 *         (no email, foreign domain, or excluded)
	 * @throws SyncException on hard errors (e.g. malformed email address)
	 */
	public function resolveMailbox(IUser $user): ?array;

	/**
	 * Delete the user's mailbox (idempotent — a mailbox that is already
	 * gone is a success). Called after the Nextcloud user was deleted.
	 *
	 * @param string $uid Nextcloud user id (for logging only)
	 * @param string $local mailbox local part (as returned by resolveMailbox)
	 * @param string $domain mail domain (as returned by resolveMailbox)
	 *
	 * @throws SyncException when the mailbox could not be deleted
	 */
	public function deleteMailbox(string $uid, string $local, string $domain): void;
}
