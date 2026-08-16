<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Service;

use RuntimeException;

/**
 * Raised when a mailbox password could not be synced.
 *
 * Contract: the message must NEVER contain the password or the API key;
 * it may contain user id, mailbox address, HTTP status and a truncated
 * API response snippet (the Migadu response never echoes the password).
 */
class SyncException extends RuntimeException {
}
