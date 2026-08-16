<?php

declare(strict_types=1);

namespace OCA\NcMigaduPasswordSync\AppInfo;

use OCA\NcMigaduPasswordSync\Listener\PasswordSyncListener;
use OCA\NcMigaduPasswordSync\Service\MigaduProvider;
use OCA\NcMigaduPasswordSync\Service\PasswordSyncProvider;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;
use OCP\User\Events\PasswordUpdatedEvent;
use OCP\User\Events\UserCreatedEvent;

class Application extends App implements IBootstrap {
	public const APP_ID = 'nc_migadu_password_sync';

	public function __construct() {
		parent::__construct(self::APP_ID);
	}

	public function register(IRegistrationContext $context): void {
		// Provider portability: everything depends on the interface; a future
		// mail provider is a new implementation + one alias swap.
		$context->registerServiceAlias(PasswordSyncProvider::class, MigaduProvider::class);
		$context->registerEventListener(PasswordUpdatedEvent::class, PasswordSyncListener::class);
		$context->registerEventListener(UserCreatedEvent::class, PasswordSyncListener::class);
	}

	public function boot(IBootContext $context): void {
	}
}
