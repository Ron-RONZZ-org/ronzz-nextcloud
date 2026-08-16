<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Service;

use OCP\IUser;

/**
 * Syncs a Nextcloud user's password to their mail-provider mailbox.
 *
 * Implementations must never log or embed the password itself.
 *
 * @see SyncException thrown when the mailbox password could not be updated
 */
interface PasswordSyncProvider {
	/**
	 * Push the user's new password to their mailbox.
	 *
	 * Implementations decide which users qualify (e.g. only addresses on
	 * the configured mail domain); non-qualifying users are a no-op.
	 *
	 * @throws SyncException when the mailbox password could not be updated
	 */
	public function syncPassword(IUser $user, string $password): void;
}
