<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Service;

use OCP\IConfig;
use OCP\IUser;
use Psr\Log\LoggerInterface;

/**
 * Migadu implementation of PasswordSyncProvider.
 *
 * Updates the mailbox password via the Migadu API:
 *   PUT https://api.migadu.com/v1/domains/{domain}/mailboxes/{local_part}
 *   Authorization: Basic base64(api_email:api_key)
 *   JSON body: {"password": <new password>}
 *   HTTP 200 = success (per Migadu's own rainloop password plugin).
 *
 * The mailbox is derived from the user's primary email address; users whose
 * email is empty or not on the configured Migadu domain are skipped (debug
 * log) — not every NC user is a mail user.
 *
 * TLS verification is ON (Migadu's own plugin disables it; we do not).
 */
class MigaduProvider implements PasswordSyncProvider {
	public const CONFIG_EMAIL = 'nc_migadu_password_sync_api_email';
	public const CONFIG_KEY = 'nc_migadu_password_sync_api_key';
	public const CONFIG_DOMAIN = 'nc_migadu_password_sync_domain';
	/** Comma-separated NC user ids that must never be synced (e.g. dummy emails). */
	public const CONFIG_EXCLUDE = 'nc_migadu_password_sync_exclude';

	private const API_BASE = 'https://api.migadu.com/v1/domains';

	/** Total attempts per sync, including the first. */
	private const MAX_ATTEMPTS = 3;

	/** Sleep seconds before attempts 2 and 3. */
	private const BACKOFF_SECONDS = [1, 2];

	/** Truncation limit for API response snippets in error messages. */
	private const RESPONSE_SNIPPET_LIMIT = 200;

	public function __construct(
		private IConfig $config,
		private LoggerInterface $logger,
	) {
	}

	public function syncPassword(IUser $user, string $password): void {
		$uid = $user->getUID();

		if (isset($this->getExcludedUids()[$uid])) {
			$this->logger->debug('migadu_sync: user {uid} is excluded from mailbox sync', ['uid' => $uid]);
			return;
		}

		$email = strtolower(trim((string)($user->getEMailAddress() ?? '')));

		if ($email === '') {
			$this->logger->debug('migadu_sync: user {uid} has no email address; nothing to sync', ['uid' => $uid]);
			return;
		}

		$domain = strtolower(trim($this->config->getSystemValueString(self::CONFIG_DOMAIN, '')));
		[$local, $mailDomain] = array_pad(explode('@', $email, 2), 2, '');

		if ($domain === '' || $mailDomain !== $domain) {
			$this->logger->debug(
				'migadu_sync: skipping {uid} ({email}) — mailbox is not on the configured Migadu domain {domain}',
				['uid' => $uid, 'email' => $email, 'domain' => $domain === '' ? '(unset)' : $domain]
			);
			return;
		}

		if ($local === '') {
			throw new SyncException("migadu_sync: malformed email address for user $uid");
		}

		$apiEmail = $this->config->getSystemValueString(self::CONFIG_EMAIL, '');
		$apiKey = $this->config->getSystemValueString(self::CONFIG_KEY, '');
		if ($apiEmail === '' || $apiKey === '') {
			throw new SyncException(
				'migadu_sync: Migadu API credentials are not configured — set system config '
				. self::CONFIG_EMAIL . ' and ' . self::CONFIG_KEY
			);
		}

		$this->updateMailboxPassword($domain, $local, $apiEmail, $apiKey, $password, $uid);
	}

	/**
	 * @return array<string,true> map of excluded NC user ids (from config)
	 */
	public function getExcludedUids(): array {
		$uids = [];
		foreach (explode(',', $this->config->getSystemValueString(self::CONFIG_EXCLUDE, '')) as $uid) {
			$uid = trim($uid);
			if ($uid !== '') {
				$uids[$uid] = true;
			}
		}
		return $uids;
	}

	private function updateMailboxPassword(
		string $domain,
		string $local,
		string $apiEmail,
		string $apiKey,
		string $password,
		string $uid
	): void {
		$url = self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes/' . rawurlencode($local);
		$payload = json_encode(['password' => $password], JSON_UNESCAPED_SLASHES);
		if ($payload === false) {
			throw new SyncException('migadu_sync: failed to encode the request payload');
		}

		$lastDetail = 'no response';
		for ($attempt = 1; $attempt <= self::MAX_ATTEMPTS; $attempt++) {
			if ($attempt > 1 && isset(self::BACKOFF_SECONDS[$attempt - 2])) {
				sleep(self::BACKOFF_SECONDS[$attempt - 2]);
			}

			$ch = curl_init($url);
			if ($ch === false) {
				throw new SyncException('migadu_sync: curl extension could not initialise a handle');
			}

			curl_setopt_array($ch, [
				CURLOPT_RETURNTRANSFER => true,
				CURLOPT_CUSTOMREQUEST => 'PUT',
				CURLOPT_POSTFIELDS => $payload,
				CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
				CURLOPT_USERPWD => $apiEmail . ':' . $apiKey,
				CURLOPT_SSL_VERIFYPEER => true,
				CURLOPT_SSL_VERIFYHOST => 2,
				CURLOPT_TIMEOUT => 10,
				CURLOPT_CONNECTTIMEOUT => 5,
			]);

			$response = curl_exec($ch);
			$code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
			$curlError = curl_error($ch);

			if ($response !== false && $code === 200) {
				$this->logger->info('migadu_sync: Migadu mailbox password updated for {uid} ({mailbox})', [
					'uid' => $uid,
					'mailbox' => $local . '@' . $domain,
				]);
				return;
			}

			$snippet = is_string($response) && $response !== ''
				? ' · ' . substr($response, 0, self::RESPONSE_SNIPPET_LIMIT)
				: '';
			$lastDetail = sprintf(
				'attempt %d/%d → HTTP %d%s%s',
				$attempt,
				self::MAX_ATTEMPTS,
				$code,
				$curlError !== '' ? ' (curl: ' . $curlError . ')' : '',
				$snippet
			);
		}

		throw new SyncException(
			'migadu_sync: failed to update Migadu mailbox ' . $local . '@' . $domain
			. ' for user ' . $uid . ': ' . $lastDetail
		);
	}
}
