function writeBatchToSheets(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const input = payload || {};
    const batchId = requireBatchId_(input.batch_id);
    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);

    const requestHeaders = getTabDefinitionByName_('Requests').headers;
    const planningHeaders = getTabDefinitionByName_('Planning Output').headers;
    const requestSheet = getSheetByNameOrCreate_(spreadsheet, 'Requests');
    const planningSheet = getSheetByNameOrCreate_(spreadsheet, 'Planning Output');

    upsertRowsByKeys_(requestSheet, requestHeaders, [
      {
        batch_id: batchId,
        prompt: asString_(input.prompt),
        requester: asString_(input.requester || 'local-user'),
        status: asString_(input.status || 'staging'),
        target_environment: asString_(input.target_environment || 'sandbox'),
        created_at: asString_(input.created_at || nowIso_())
      }
    ], ['batch_id']);

    const planning = input.planning_summary || {};
    upsertRowsByKeys_(planningSheet, planningHeaders, [
      {
        batch_id: batchId,
        object_type: asString_(planning.object_type),
        intent: asString_(planning.intent),
        confidence: asString_(planning.confidence)
      }
    ], ['batch_id']);

    const records = Array.isArray(input.records) ? input.records : [];
    const grouped = {};
    records.forEach(function eachRecord(record, index) {
      const objectType = normalizeObjectType_(record.object_type);
      const targetSheet = objectTypeToSheetName_(objectType);
      if (!grouped[targetSheet]) {
        grouped[targetSheet] = [];
      }

      const warnings = parseWarnings_(record.warnings);
      grouped[targetSheet].push({
        batch_id: batchId,
        record_id: asString_(record.record_id || ('REC-' + (index + 1))),
        title: asString_(record.title || ''),
        object_type: objectType,
        preview_summary: asString_(record.preview_summary || ''),
        conditions_json: JSON.stringify(record.conditions || []),
        actions_json: JSON.stringify(record.actions || []),
        validation_status: asString_(record.validation_status || 'pending'),
        warning_message: warnings.join('; '),
        blocked_reason: asString_(record.blocked_reason || ''),
        import_decision: asString_(record.import_decision || 'pending_review'),
        approved_by: asString_(record.approved_by || ''),
        approved_at: asString_(record.approved_at || ''),
        deployment_status: asString_(record.deployment_status || 'pending'),
        zendesk_object_id: asString_(record.zendesk_object_id || ''),
        chunk_id: asString_(record.chunk_id || record._supervisor_chunk_id || ''),
        record_key: asString_(record.record_key || record._supervisor_record_key || ''),
        department: asString_(record.department || record.department_name || ''),
        topic: asString_(record.topic || ''),
        source_provider: asString_(record.source_provider || ''),
        source_model: asString_(record.source_model || '')
      });
    });

    const writeSummary = {};
    OBJECT_SHEET_NAMES.forEach(function eachSheetName(sheetName) {
      const sheet = getSheetByNameOrCreate_(spreadsheet, sheetName);
      const headers = getTabDefinitionByName_(sheetName).headers;
      const written = replaceBatchRows_(sheet, headers, batchId, grouped[sheetName] || []);
      writeSummary[sheetName] = written;
    });

    const metadataWritten = typeof writeBatchMetadataUnlocked_ === 'function'
      ? writeBatchMetadataUnlocked_(spreadsheet, input)
      : 0;
    const progressWritten = typeof appendProgressEventsUnlocked_ === 'function'
      ? appendProgressEventsUnlocked_(spreadsheet, {
          batch_id: batchId,
          events: Array.isArray(input.progress_events) ? input.progress_events : []
        })
      : 0;

    return {
      ok: true,
      action: 'write_batch_to_sheets',
      batch_id: batchId,
      spreadsheet_id: spreadsheet.getId(),
      spreadsheet_url: spreadsheet.getUrl(),
      request_rows_written: 1,
      planning_rows_written: 1,
      object_rows_written: writeSummary,
      metadata_rows_written: metadataWritten,
      progress_rows_written: progressWritten,
      total_records: records.length
    };
  } finally {
    lock.releaseLock();
  }
}

