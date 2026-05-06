function syncSchemaBundle(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const schemaBundle = (payload && payload.schema_bundle) || payload || {};
    if (!schemaBundle || typeof schemaBundle !== 'object') {
      throw new Error('schema_bundle payload is required.');
    }

    const serialized = JSON.stringify(schemaBundle);
    const schemaHash = sha256Base64_(serialized);
    const props = getScriptProperties_();
    const spreadsheet = openManagedSpreadsheet_();
    const registrySheet = getSheetByNameOrCreate_(spreadsheet, 'Schema Registry');

    const registryHeaders = ['schema_name', 'schema_version', 'hash', 'updated_at', 'schema_json'];
    ensureHeader_(registrySheet, registryHeaders);

    const version = String(schemaBundle.generated_at || nowIso_());

    appendRowsByHeader_(registrySheet, registryHeaders, [
      {
        schema_name: 'fastapi_schema_bundle',
        schema_version: version,
        hash: schemaHash,
        updated_at: nowIso_(),
        schema_json: serialized
      }
    ]);

    props.setProperty(APP_CONFIG.SCHEMA_BUNDLE_PROPERTY, serialized);
    props.setProperty(APP_CONFIG.LAST_SCHEMA_HASH_PROPERTY, schemaHash);
    props.setProperty(APP_CONFIG.LAST_SCHEMA_SYNC_AT_PROPERTY, nowIso_());

    return {
      ok: true,
      action: 'sync_schema',
      schema_hash: schemaHash,
      schema_version: version,
      synced_at: props.getProperty(APP_CONFIG.LAST_SCHEMA_SYNC_AT_PROPERTY)
    };
  } finally {
    lock.releaseLock();
  }
}

function getSchemaBundleInfo() {
  const props = getScriptProperties_();
  return {
    has_schema: Boolean(props.getProperty(APP_CONFIG.SCHEMA_BUNDLE_PROPERTY)),
    schema_hash: props.getProperty(APP_CONFIG.LAST_SCHEMA_HASH_PROPERTY) || null,
    synced_at: props.getProperty(APP_CONFIG.LAST_SCHEMA_SYNC_AT_PROPERTY) || null
  };
}
