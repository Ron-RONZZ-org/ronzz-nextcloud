<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\Command;

use OCA\NcMigaduPasswordSync\Service\MigaduProvider;
use OCP\IConfig;
use OCP\IUserManager;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

/**
 * occ migadu:test — pre-flight connectivity check for the password-sync app:
 *   1. configuration present
 *   2. Migadu API accepts the credentials and can see the domain
 *   3. mailboxes of all NC users on the configured domain exist in Migadu
 *
 * Exit code 0 = ready, 1 = something needs fixing.
 */
class MigaduTestCommand extends Command {
	private const API_BASE = 'https://api.migadu.com/v1/domains';

	public function __construct(
		private IConfig $config,
		private IUserManager $userManager,
		private MigaduProvider $provider,
	) {
		parent::__construct();
	}

	protected function configure(): void {
		$this->setName('migadu:test')
			->setDescription('Check the Migadu API connectivity and configuration of the password-sync app');
	}

	protected function execute(InputInterface $input, OutputInterface $output): int {
		$apiEmail = $this->config->getSystemValueString(MigaduProvider::CONFIG_EMAIL, '');
		$apiKey = $this->config->getSystemValueString(MigaduProvider::CONFIG_KEY, '');
		$domain = strtolower($this->config->getSystemValueString(MigaduProvider::CONFIG_DOMAIN, ''));

		if ($apiEmail === '' || $apiKey === '' || $domain === '') {
			$output->writeln('<error>Missing configuration. Set the following system config values first:</error>');
			$output->writeln('  occ config:system:set ' . MigaduProvider::CONFIG_EMAIL . ' --value=<migadu-admin-email>');
			$output->writeln('  occ config:system:set ' . MigaduProvider::CONFIG_KEY . ' --value=<migadu-api-key>');
			$output->writeln('  occ config:system:set ' . MigaduProvider::CONFIG_DOMAIN . ' --value=<mail-domain>');
			return Command::FAILURE;
		}

		$output->writeln('Migadu API account : ' . $apiEmail);
		$output->writeln('Synced mail domain : ' . $domain);

		// 1) Auth + domain access
		$code = $this->request('GET', self::API_BASE . '/' . rawurlencode($domain), $apiEmail, $apiKey);
		if ($code !== 200) {
			$output->writeln(sprintf(
				'<error>Auth/domain check failed (HTTP %d). Check the API email + key, and that the Migadu account can access domain %s.</error>',
				$code,
				$domain
			));
			return Command::FAILURE;
		}
		$output->writeln('<info>OK — Migadu API accepts the credentials and can see the domain.</info>');

		// 2) Mailbox listing (also exercises a second endpoint)
		$mailboxes = $this->listMailboxes($domain, $apiEmail, $apiKey);
		$output->writeln(sprintf('Mailboxes on %s : %d', $domain, count($mailboxes)));

		// 3) Per-NC-user mailbox presence on the configured domain
		$output->writeln('');
		$output->writeln('Nextcloud users on ' . $domain . ':');
		$excluded = $this->provider->getExcludedUids();
		$failed = false;
		foreach ($this->userManager->search('') as $user) {
			$uid = $user->getUID();
			$email = strtolower(trim((string)($user->getEMailAddress() ?? '')));
			if ($email === '') {
				continue;
			}
			[$local, $mailDomain] = array_pad(explode('@', $email, 2), 2, '');
			if ($mailDomain !== $domain) {
				continue;
			}

			if (isset($excluded[$uid])) {
				$output->writeln(sprintf('  %-30s %s (excluded from sync)', $uid, $email));
				continue;
			}

			if ($local === '') {
				$output->writeln(sprintf('  <error>%-30s malformed email</error>', $uid));
				$failed = true;
				continue;
			}

			$code = $this->request('GET', self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes/' . rawurlencode($local), $apiEmail, $apiKey);
			if ($code === 200) {
				$output->writeln(sprintf('  <info>%-30s %s → mailbox exists</info>', $uid, $email));
			} else {
				$output->writeln(sprintf('  <error>%-30s %s → mailbox lookup HTTP %d (will fail on next password change)</error>', $uid, $email, $code));
				$failed = true;
			}
		}

		$output->writeln('');
		if ($failed) {
			$output->writeln('<error>Fix the issues above before relying on password sync.</error>');
			return Command::FAILURE;
		}

		$output->writeln('<info>migadu:test OK — password sync is ready.</info>');
		return Command::SUCCESS;
	}

	/**
	 * @return string[] mailbox addresses, e.g. ["ron@ronzz.org"]
	 */
	private function listMailboxes(string $domain, string $apiEmail, string $apiKey): array {
		$body = $this->request('GET', self::API_BASE . '/' . rawurlencode($domain) . '/mailboxes', $apiEmail, $apiKey, true);
		if ($body === '') {
			return [];
		}
		$json = json_decode($body, true);
		if (!is_array($json) || !isset($json['mailboxes']) || !is_array($json['mailboxes'])) {
			return [];
		}
		$addresses = [];
		foreach ($json['mailboxes'] as $mailbox) {
			if (is_array($mailbox) && isset($mailbox['address']) && is_string($mailbox['address'])) {
				$addresses[] = $mailbox['address'];
			}
		}
		sort($addresses);
		return $addresses;
	}

	/**
	 * Raw GET request against the Migadu API.
	 *
	 * @return int HTTP status code (200 = OK); 0 = transport error
	 */
	private function request(string $method, string $url, string $apiEmail, string $apiKey, bool $wantBody = false): int|string {
		$ch = curl_init($url);
		if ($ch === false) {
			return $wantBody ? '' : 0;
		}
		curl_setopt_array($ch, [
			CURLOPT_RETURNTRANSFER => true,
			CURLOPT_CUSTOMREQUEST => $method,
			CURLOPT_HTTPHEADER => ['Accept: application/json'],
			CURLOPT_USERPWD => $apiEmail . ':' . $apiKey,
			CURLOPT_SSL_VERIFYPEER => true,
			CURLOPT_SSL_VERIFYHOST => 2,
			CURLOPT_TIMEOUT => 10,
			CURLOPT_CONNECTTIMEOUT => 5,
		]);
		$response = curl_exec($ch);
		$code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);

		if ($wantBody) {
			return is_string($response) ? $response : '';
		}
		return $code;
	}
}
