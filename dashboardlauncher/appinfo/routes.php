<?php

return [
    'routes' => [
    	['name' => 'page#index', 'url' => '/', 'verb' => 'GET'],
        ['name' => 'page#newWhiteboard', 'url' => '/new-whiteboard', 'verb' => 'GET'],
        ['name' => 'admin#getSiteSettings', 'url' => '/api/admin/site-settings', 'verb' => 'GET'],
        ['name' => 'admin#saveSiteSettings', 'url' => '/api/admin/site-settings', 'verb' => 'POST'],
        
        // Vue d'administration (Page HTML)
        ['name' => 'admin#index', 'url' => '/settings/admin/dashboardlauncher', 'verb' => 'GET'],
        ['name' => 'admin#listButtons', 'url' => '/api/admin/buttons', 'verb' => 'GET'],
        ['name' => 'admin#listIcons', 'url' => '/api/admin/icons', 'verb' => 'GET'],
        ['name' => 'admin#deleteIcon', 'url' => '/api/admin/icon/{filename}', 'verb' => 'DELETE'],
        ['name' => 'admin#listLibraryIcons', 'url' => '/api/admin/library-icons', 'verb' => 'GET'], 
        ['name' => 'admin#getLibraryIcon', 'url' => '/library-icon/{filename}', 'verb' => 'GET'],

        // API REST pour la gestion des boutons (AJAX / Fetch)
        ['name' => 'admin#saveButton', 'url' => '/api/admin/button', 'verb' => 'POST'],
        ['name' => 'admin#deleteButton', 'url' => '/api/admin/button/{id}', 'verb' => 'DELETE'],
        ['name' => 'admin#uploadIcon', 'url' => '/api/admin/icon', 'verb' => 'POST'],
        ['name' => 'admin#getIcon', 'url' => '/icon/{filename}', 'verb' => 'GET'],
    ]
];
