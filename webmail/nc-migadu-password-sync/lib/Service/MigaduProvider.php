<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Service;

use OCP\IConfig;
use OCP\IUser;
use Psr\Log\LoggerInterface;

/**
 * Migadu implementation of PasswordSyncProvider.
 *
 * Mailbox lifecycle via the Migadu API:
 *   GET    https://api.migadu.com/v1/domains/{domain}/mailboxes/{local_part}
 *   POST   https://api.migadu.com/v1/domains/{domain}/mailboxes           (create: local_part, name, password)
 *   PUT    https://api.migadu.com/v1/domains/{domain}/mailboxes/{local_part} (set password)
 *   DELETE https://api.migadu.com/v1/domains/{domain}/mailboxes/{local_part}
 *   Authorization: Basic base64(api_email:api_key)
 *
 * Notes:
 * - PUT returns HTTP 200 on success (per Migadu's own rainloop password plugin).
 * - DELETE is a known-Migadu quirk: a *successful* delete returns HTTP 500
 *   (some deployments return 200/204/404). We therefore treat 200, 204, 404
 *   and 500 as success — 404 additionally makes the operation idempotent.
 * - The mailbox is derived from the user's primary email address; users whose
 *   email is empty or not on the configured Migadu domain are skipped (debug
 *   log) — not every NC user is a mail user.
 *
 * TLS verification is ON (Migadu's own plugin disables it; we do not).
 */
class MigaduProvider implements PasswordSyncProvider {
	public const CONFIG_EMAIL = 'nc_migadu_password_sync_api_email';
	public const CONFIG_KEY = 'nc_migadu_password_sync_api_key';
	public const CONFIG_DOMAIN = 'nc_migadu_password_sync_domain';
	/** Comma-separated NC user ids that must never be synced (e.g. dummy emails). */
	public const CONFIG_EXCLUDE = 'nc_migadu_password_sync_exclude';
	/** When false, NC users are never removed from Migadu (safety valve, default true). */
	public const CONFIG_DELETE = 'nc_migadu_password_sync_delete_mailboxes';

	private const API_BASE = 'https://api.migadu.com/v1/domains';

	/** Total attempts per operation, including the first. */
	private const MAX_ATTEMPTS = 3;

	/** Sleep seconds before attempts 2 and 3. */
	private const BACKOFF_SECONDS = [1, 2];

	/** Truncation limit for API response snippets in error messages. */
	private const RESPONSE_SNIPPET_LIMIT = 200;

	/** HTTP statuses that count as a successful mailbox deletion (Migadu quirk: 500). */
	private const DELETE_SUCCESS_CODES = [200, 204, 404, 500];

	public function __construct(
		private IConfig $config,
		private LoggerInterface $logger,
	) {
	}

	public function syncPassword(IUser $user, string $password): void {
		$mailbox = $this->resolveMailbox($user);
		if ($mailbox === null) {
			return;
		}

		$this->syncMailbox($mailbox['domain'], $mailbox['local'], $user, $password);
	}

	public function resolveMailbox(IUser $user): ?array {
		$uid = $user->getUID();

		if (isset($this->getExcludedUids()[$uid])) {
			$this->logger->debug('migadu_sync: user {uid} is excluded from mailbox sync', ['uid' => $uid]);
			return null;
		}

		$email = strtolower(trim((string)($user->getEMailAddress() ?? '')));

		if ($email === '') {
			$this->logger->debug('migadu_sync: user {uid} has no email address; nothing to sync', ['uid' => $uid]);
			return null;
		}

		$domain = strtolower(trim($this->config->getSystemValueString(self::CONFIG_DOMAIN, '')));
		[$local, $mailDomain] = array_pad(explode('@', $email, 2), 2, '');

		if ($domain === '' || $mailDomain !== $domain) {
			$this->logger->debug(
				'migadu_sync: skipping {uid} ({email}) — mailbox is not on the configured Migadu domain {domain}',
				['uid' => $uid, 'email' => $email, 'domain' => $domain === '' ? '(unset)' : $domain]
			);
			return null;
		}

		if ($local === '') {
			throw new SyncException("migadu_sync: malformed email address for user $uid");
		}

		return ['local' => $local, 'domain' => $domain];
	}

	public function deleteMailbox(string $uid, string $local, string $domain): void {
		if (!$this->config->getSystemValueBool(self::CONFIG_DELETE, true)) {
			$this->logger->debug(
				'migadu_sync: mailbox deletion for {uid} ({local}@{domain}) skipped — ' . self::CONFIG_DELETE . ' is false',
				['uid' => $uid, 'local' => $local, 'domain' => $domain]
			);
			return;
		}

		$mailbox = $local . '@' . $domain;
		[$apiEmail, $apiKey] = $this->credentials();

		// Idempotent: a 404 (already gone) is a success, as are the statuses
		// that Migadu returns on a successful delete (500 quirk).
		$this->requestWithRetry(
			'DELETE',
			self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes/' . rawurlencode($local),
			$apiEmail,
			$apiKey,
			null,
			'delete mailbox ' . $mailbox . ' (NC user ' . $uid . ' deleted)',
			static fn (int $code): bool => in_array($code, self::DELETE_SUCCESS_CODES, true)
		);

		$this->logger->info('migadu_sync: Migadu mailbox deleted for {uid} ({mailbox})', [
			'uid' => $uid,
			'mailbox' => $mailbox,
		]);
	}

	/**
	 * Ensure the mailbox exists with the given password: create it when
	 * missing (new user, or legacy user whose mailbox was never created),
	 * otherwise push the password — the NC password *is* the mailbox password.
	 */
	private function syncMailbox(string $domain, string $local, IUser $user, string $password): void {
		$uid = $user->getUID();
		$mailbox = $local . '@' . $domain;
		[$apiEmail, $apiKey] = $this->credentials();

		$lookup = $this->requestWithRetry(
			'GET',
			self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes/' . rawurlencode($local),
			$apiEmail,
			$apiKey,
			null,
			'lookup mailbox ' . $mailbox . ' for user ' . $uid,
			static fn (int $code): bool => in_array($code, [200, 404], true)
		);

		if ($lookup['code'] === 404) {
			$payload = json_encode([
				'local_part' => $local,
				'name' => $user->getDisplayName(),
				'password' => $password,
			], JSON_UNESCAPED_SLASHES);
			if ($payload === false) {
				throw new SyncException('migadu_sync: failed to encode the create-mailbox payload');
			}

			$this->requestWithRetry(
				'POST',
				self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes',
				$apiEmail,
				$apiKey,
				$payload,
				'create mailbox ' . $mailbox . ' for user ' . $uid,
				static fn (int $code): bool => $code === 200
			);
			$this->logger->info('migadu_sync: Migadu mailbox created for {uid} ({mailbox})', [
				'uid' => $uid,
				'mailbox' => $mailbox,
			]);
			return;
		}

		$payload = json_encode(['password' => $password], JSON_UNESCAPED_SLASHES);
		if ($payload === false) {
			throw new SyncException('migadu_sync: failed to encode the request payload');
		}

		$this->requestWithRetry(
			'PUT',
			self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes/' . rawurlencode($local),
			$apiEmail,
			$apiKey,
			$payload,
			'update password of mailbox ' . $mailbox . ' for user ' . $uid,
			static fn (int $code): bool => $code === 200
		);
		$this->logger->info('migadu_sync: Migadu mailbox password updated for {uid} ({mailbox})', [
			'uid' => $uid,
			'mailbox' => $mailbox,
		]);
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

	/**
	 * @return array{string, string} [api email, api key]
	 */
	private function credentials(): array {
		$apiEmail = $this->config->getSystemValueString(self::CONFIG_EMAIL, '');
		$apiKey = $this->config->getSystemValueString(self::CONFIG_KEY, '');
		if ($apiEmail === '' || $apiKey === '') {
			throw new SyncException(
				'migadu_sync: Migadu API credentials are not configured — set system config '
				. self::CONFIG_EMAIL . ' and ' . self::CONFIG_KEY
			);
		}
		return [$apiEmail, $apiKey];
	}

	/**
	 * Perform a Migadu API request with the standard retry/backoff policy.
	 *
	 * @param callable(int $code): bool $isSuccess
	 * @return array{code: int, body: string} the successful response
	 * @throws SyncException when all attempts fail
	 */
	private function requestWithRetry(
		string $method,
		string $url,
		string $apiEmail,
		string $apiKey,
		?string $payload,
		string $describe,
		callable $isSuccess
	): array {
		$lastDetail = 'no response';
		for ($attempt = 1; $attempt <= self::MAX_ATTEMPTS; $attempt++) {
			if ($attempt > 1 && isset(self::BACKOFF_SECONDS[$attempt - 2])) {
				sleep(self::BACKOFF_SECONDS[$attempt - 2]);
			}

			$response = $this->request($method, $url, $apiEmail, $apiKey, $payload);
			$code = $response['code'];

			if ($isSuccess($code)) {
				return $response;
			}

			$snippet = $response['body'] !== ''
				? ' · ' . substr($response['body'], 0, self::RESPONSE_SNIPPET_LIMIT)
				: '';
			$lastDetail = sprintf(
				'attempt %d/%d → HTTP %d%s',
				$attempt,
				self::MAX_ATTEMPTS,
				$code,
				$snippet
			);
		}

		throw new SyncException('migadu_sync: ' . $describe . ': ' . $lastDetail);
	}

	/**
	 * Single Migadu API request.
	 *
	 * @return array{code: int, body: string} HTTP status (0 = transport error,
	 *         in which case body carries the curl error message)
	 */
	private function request(
		string $method,
		string $url,
		string $apiEmail,
		string $apiKey,
		?string $payload
	): array {
		$ch = curl_init($url);
		if ($ch === false) {
			throw new SyncException('migadu_sync: curl extension could not initialise a handle');
		}

		curl_setopt_array($ch, [
			CURLOPT_RETURNTRANSFER => true,
			CURLOPT_CUSTOMREQUEST => $method,
			CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Accept: application/json'],
			CURLOPT_USERPWD => $apiEmail . ':' . $apiKey,
			CURLOPT_SSL_VERIFYPEER => true,
			CURLOPT_SSL_VERIFYHOST => 2,
			CURLOPT_TIMEOUT => 10,
			CURLOPT_CONNECTTIMEOUT => 5,
		]);
		if ($payload !== null) {
			curl_setopt($ch, CURLOPT_POSTFIELDS, $payload);
		}

		$response = curl_exec($ch);
		$code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
		$curlError = curl_error($ch);
		curl_close($ch);

		if ($response === false) {
			return ['code' => 0, 'body' => 'curl: ' . $curlError];
		}

		return ['code' => $code, 'body' => (string) $response];
	}
}