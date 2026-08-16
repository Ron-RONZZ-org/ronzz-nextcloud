<?php
/**
 * Nextcloud - dashboardlauncher
 *
 * @author DPFPIC
 * @copyright 2026
 */

namespace OCA\DashboardLauncher\Controller;

use OCP\IRequest;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\TemplateResponse;
use OCP\AppFramework\Http\RedirectResponse;
use OCP\IUserSession;
use OCP\IGroupManager;
use OCP\IConfig;
use OCP\IL10N;
use OCP\IURLGenerator;
use OCP\DirectEditing\IManager as IDirectEditingManager;
use OCP\DirectEditing\RegisterDirectEditorEvent;
use OCP\Files\IRootFolder;
use OCP\EventDispatcher\IEventDispatcher;
use Psr\Log\LoggerInterface;
use OCA\DashboardLauncher\Service\ButtonService;

class PageController extends Controller {

    public function __construct(
        string $appName,
        IRequest $request,
        private IUserSession $userSession,
        private IGroupManager $groupManager,
        private ButtonService $buttonService,
        private IConfig $config,
        private IL10N $l,
        private IURLGenerator $urlGenerator,
        private IDirectEditingManager $directEditingManager,
        private IRootFolder $rootFolder,
        private IEventDispatcher $eventDispatcher,
        private LoggerInterface $logger,
    ) {
        parent::__construct($appName, $request);
    }

    /**
     * @NoAdminRequired
     * @NoCSRFRequired
     */
    public function index() {
        $user = $this->userSession->getUser();
        $displayName = $user !== null ? $user->getDisplayName() : '';
        $isAdmin = $user !== null && $this->groupManager->isAdmin($user->getUID());

        $buttons = $this->buttonService->getAuthorizedButtonsForUser();

        $siteTitle = $this->config->getAppValue($this->appName, 'site_title', $this->l->t('My Dashboard'));
        $welcomeTextRaw = $this->config->getAppValue($this->appName, 'welcome_text', $this->l->t('Hello {displayName}, select a service below to access your tools and shared folders'));
        $footerText = $this->config->getAppValue($this->appName, 'footer_text', $this->l->t('Secure space powered by Nextcloud'));

        $welcomeText = str_replace('{displayName}', $displayName, $welcomeTextRaw);

        \OCP\Util::addTranslations($this->appName);

        return new TemplateResponse('dashboardlauncher', 'main', [
            'displayName' => $displayName,
            'isAdmin'     => $isAdmin,
            'buttons'     => $buttons,
            'siteTitle'   => $siteTitle,
            'welcomeText' => $welcomeText,
            'footerText'  => $footerText,
        ]);
    }

    /**
     * Create a new whiteboard file and redirect to the direct-editing session.
     *
     * Used by the "Tableau blanc" portal tile (Whiteboard is a file-type app
     * with no standalone page — see README §6.1). Creates a timestamped
     * `.whiteboard` file in the user's Whiteboards folder via the
     * DirectEditing API, then redirects to the editor.
     *
     * @NoAdminRequired
     * @NoCSRFRequired
     */
    public function newWhiteboard() {
        $user = $this->userSession->getUser();
        if ($user === null) {
            return new RedirectResponse($this->urlGenerator->linkToRouteAbsolute('core.login.showLoginForm'));
        }

        $uid = $user->getUID();
        $folderName = 'Whiteboards';

        try {
            $userFolder = $this->rootFolder->getUserFolder($uid);
            if (!$userFolder->nodeExists('/' . $folderName)) {
                $userFolder->newFolder('/' . $folderName);
            }

            $fileName = 'Whiteboard-' . date('Y-m-d-H-i-s') . '-' . substr(bin2hex(random_bytes(2)), 0, 4) . '.whiteboard';
            $path = '/' . $folderName . '/' . $fileName;

            // Register the whiteboard editor (like the Files DirectEditing controller does)
            // before creating a token — otherwise the manager knows no editor for 'whiteboard'.
            $this->eventDispatcher->dispatchTyped(new RegisterDirectEditorEvent($this->directEditingManager));
            $token = $this->directEditingManager->create($path, 'whiteboard', 'whiteboard');
            $editUrl = $this->urlGenerator->linkToRouteAbsolute('files.DirectEditingView.edit', ['token' => $token]);
            return new RedirectResponse($editUrl);
        } catch (\Throwable $e) {
            $this->logger->error('newWhiteboard failed: ' . $e->getMessage(), ['exception' => $e]);
            return new RedirectResponse($this->urlGenerator->linkToRouteAbsolute('files.view.indexView', ['view' => 'files']));
        }
    }
}
