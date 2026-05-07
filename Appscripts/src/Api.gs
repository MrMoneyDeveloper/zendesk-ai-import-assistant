function doGet(e) {
  try {
    const action = getRequestParam_(e, 'action') || 'health';

    if (action === 'health') {
      return jsonResponse_({
        ok: true,
        action: 'health',
        service: APP_CONFIG.APP_NAME,
        now: nowIso_(),
        schema: getSchemaBundleInfoSafe_()
      });
    }

    const apiKey = getRequestParam_(e, 'api_key');
    requireApiKey_(apiKey);

    if (action === 'schema_info') {
      return jsonResponse_({ ok: true, action: 'schema_info', data: getSchemaBundleInfoSafe_() });
    }

    if (action === 'get_batch_preview') {
      return jsonResponse_(getBatchPreview({ batch_id: getRequestParam_(e, 'batch_id') }));
    }

    if (action === 'get_execution_summary') {
      return jsonResponse_(getExecutionSummary({ batch_id: getRequestParam_(e, 'batch_id') }));
    }

    return jsonResponse_({ ok: false, action: action, error: 'Unsupported GET action.' });
  } catch (error) {
    return jsonResponse_({ ok: false, action: 'error', error: String(error.message || error) });
  }
}

function doPost(e) {
  try {
    const body = readJsonBody_(e);
    const action = String(body.action || '').trim();
    const payload = body.payload || {};
    const apiKey = String(body.api_key || '').trim();

    requireApiKey_(apiKey);

    if (action === 'setup_once') {
      return jsonResponse_(setupOnce(payload));
    }

    if (action === 'sync_schema') {
      return jsonResponse_(syncSchemaBundle(payload));
    }

    if (action === 'schema_info') {
      return jsonResponse_({ ok: true, action: 'schema_info', data: getSchemaBundleInfoSafe_() });
    }

    if (action === 'generate_api_key') {
      return jsonResponse_(generateAndStoreApiKeyInSheet(payload));
    }

    if (action === 'write_batch_to_sheets') {
      return jsonResponse_(writeBatchToSheets(payload));
    }

    if (action === 'validate_batch') {
      return jsonResponse_(validateBatch(payload));
    }

    if (action === 'get_batch_preview') {
      return jsonResponse_(getBatchPreview(payload));
    }

    if (action === 'update_approval_status') {
      return jsonResponse_(handleUpdateApprovalStatus_(payload));
    }

    if (action === 'write_execution_log') {
      return jsonResponse_(writeExecutionLog(payload));
    }

    if (action === 'get_execution_summary') {
      return jsonResponse_(getExecutionSummary(payload));
    }

    return jsonResponse_({ ok: false, action: action, error: 'Unsupported POST action.' });
  } catch (error) {
    return jsonResponse_({ ok: false, action: 'error', error: String(error.message || error) });
  }
}

function getSchemaBundleInfoSafe_() {
  if (typeof getSchemaBundleInfo === 'function') {
    return getSchemaBundleInfo();
  }
  const props = getScriptProperties_();
  return {
    has_schema: Boolean(props.getProperty(APP_CONFIG.SCHEMA_BUNDLE_PROPERTY)),
    schema_hash: props.getProperty(APP_CONFIG.LAST_SCHEMA_HASH_PROPERTY) || null,
    synced_at: props.getProperty(APP_CONFIG.LAST_SCHEMA_SYNC_AT_PROPERTY) || null,
    fallback_mode: true
  };
}

function handleUpdateApprovalStatus_(payload) {
  if (typeof updateApprovalStatus === 'function') {
    return updateApprovalStatus(payload);
  }
  return legacyUpdateApprovalStatus_(payload || {});
}

function legacyUpdateApprovalStatus_(payload) {
  const input = payload || {};
  const batchId = requireBatchId_(input.batch_id);
  const approvedBy = asString_(input.approved_by || 'local-user');
  const decisions = Array.isArray(input.records) ? input.records : [];

  const spreadsheet = openManagedSpreadsheet_();
  ensureRequiredTabs_(spreadsheet);

  const approvalSheet = getSheetByNameOrCreate_(spreadsheet, 'Approval Log');
  const approvalHeaders = getTabDefinitionByName_('Approval Log').headers;
  const summary = { approved: 0, skipped: 0, edit_later: 0, blocked: 0 };
  const decisionMap = {};
  const approvalRows = [];

  decisions.forEach(function eachDecision(decision) {
    const recordId = asString_(decision.record_id).trim();
    const importDecision = asString_(decision.import_decision || 'pending_review').trim();
    if (!recordId) {
      return;
    }
    decisionMap[recordId] = {
      import_decision: importDecision,
      approved_by: approvedBy,
      approved_at: nowIso_()
    };
    if (summary[importDecision] !== undefined) {
      summary[importDecision] += 1;
    }
    approvalRows.push({
      batch_id: batchId,
      record_id: recordId,
      import_decision: importDecision,
      approved_at: decisionMap[recordId].approved_at,
      approved_by: approvedBy
    });
  });

  appendRowsByHeader_(approvalSheet, approvalHeaders, approvalRows);
  legacyApplyApprovalToObjectTabs_(spreadsheet, batchId, decisionMap);

  return {
    ok: true,
    action: 'update_approval_status',
    batch_id: batchId,
    updated_records: approvalRows.length,
    summary: summary,
    fallback_mode: true
  };
}

function legacyApplyApprovalToObjectTabs_(spreadsheet, batchId, decisionMap) {
  const decisions = decisionMap || {};
  OBJECT_SHEET_NAMES.forEach(function eachSheetName(sheetName) {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const values = sheet.getDataRange().getValues();
    if (!values || values.length <= 1) {
      return;
    }

    const headers = values[0].map(function mapHeader(value) {
      return asString_(value).trim();
    });
    const idx = indexByHeaders_(headers);
    const decisionCol = idx.import_decision;
    const approvedByCol = idx.approved_by;
    const approvedAtCol = idx.approved_at;
    if (decisionCol === undefined || approvedByCol === undefined || approvedAtCol === undefined) {
      return;
    }

    let touched = false;
    for (let row = 1; row < values.length; row += 1) {
      const rowBatchId = asString_(values[row][idx.batch_id]).trim();
      if (rowBatchId !== batchId) {
        continue;
      }

      const recordId = asString_(values[row][idx.record_id]).trim();
      if (!decisions[recordId]) {
        continue;
      }
      values[row][decisionCol] = decisions[recordId].import_decision;
      values[row][approvedByCol] = decisions[recordId].approved_by;
      values[row][approvedAtCol] = decisions[recordId].approved_at;
      touched = true;
    }

    if (touched) {
      sheet.getRange(1, 1, values.length, values[0].length).setValues(values);
    }
  });
}
