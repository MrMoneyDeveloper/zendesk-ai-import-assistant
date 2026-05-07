function setupOnce(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const props = getScriptProperties_();
    const spreadsheet = openManagedSpreadsheet_();

    const rootFolderId = String((payload && payload.root_folder_id) || props.getProperty(APP_CONFIG.ROOT_FOLDER_ID_PROPERTY) || '').trim();
    const folderState = ensureProjectFolderTree_(rootFolderId);
    const createdTabs = [];

    REQUIRED_TABS.forEach(function eachTab(tabDef) {
      const sheet = getSheetByNameOrCreate_(spreadsheet, tabDef.name);
      if (sheet.getLastRow() === 0) {
        createdTabs.push(tabDef.name);
      }
      ensureHeader_(sheet, tabDef.headers);
    });

    if (folderState.root_folder_id) {
      props.setProperty(APP_CONFIG.ROOT_FOLDER_ID_PROPERTY, folderState.root_folder_id);
    }

    props.setProperty(APP_CONFIG.LAST_SETUP_AT_PROPERTY, nowIso_());

    return {
      ok: true,
      action: 'setup_once',
      spreadsheet_id: spreadsheet.getId(),
      spreadsheet_url: spreadsheet.getUrl(),
      created_tabs: createdTabs,
      root_folder_id: folderState.root_folder_id || null,
      folders: folderState.folders || [],
      setup_at: props.getProperty(APP_CONFIG.LAST_SETUP_AT_PROPERTY)
    };
  } finally {
    lock.releaseLock();
  }
}

function setApiKeyFromEditor(apiKey) {
  const key = String(apiKey || '').trim();
  if (!key) {
    throw new Error('API key cannot be empty.');
  }

  getScriptProperties_().setProperty(APP_CONFIG.API_KEY_PROPERTY, key);
  return key.length;
}

function setBootstrapConfigFromEditor(config) {
  const props = getScriptProperties_();
  const input = config || {};

  if (input.sheet_name) {
    props.setProperty('APPS_SCRIPT_SHEET_NAME', String(input.sheet_name));
  }
  if (input.root_folder_id) {
    props.setProperty(APP_CONFIG.ROOT_FOLDER_ID_PROPERTY, String(input.root_folder_id));
  }

  return {
    ok: true,
    sheet_name: props.getProperty('APPS_SCRIPT_SHEET_NAME') || APP_CONFIG.DEFAULT_SPREADSHEET_NAME,
    root_folder_id: props.getProperty(APP_CONFIG.ROOT_FOLDER_ID_PROPERTY) || null
  };
}

function ensureProjectFolderTree_(rootFolderId) {
  const childFolderNames = ['contracts', 'exports', 'templates', 'logs', 'validation'];
  let rootFolder = null;

  if (rootFolderId) {
    rootFolder = DriveApp.getFolderById(rootFolderId);
  } else {
    rootFolder = DriveApp.createFolder(APP_CONFIG.APP_NAME + ' Assets');
  }

  const existing = {};
  const childFolders = rootFolder.getFolders();
  while (childFolders.hasNext()) {
    const folder = childFolders.next();
    existing[folder.getName()] = folder;
  }

  childFolderNames.forEach(function eachName(name) {
    if (!existing[name]) {
      rootFolder.createFolder(name);
    }
  });

  return {
    root_folder_id: rootFolder.getId(),
    folders: childFolderNames
  };
}

