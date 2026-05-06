function getBatchPreview(payload) {
  const batchId = requireBatchId_(payload && payload.batch_id);
  const spreadsheet = openManagedSpreadsheet_();
  ensureRequiredTabs_(spreadsheet);

  const validationByRecord = loadLatestValidationByRecord_(spreadsheet, batchId);
  const approvalByRecord = loadLatestApprovalByRecord_(spreadsheet, batchId);
  const records = [];
  const generatedCounts = {};

  OBJECT_SHEET_NAMES.forEach(function eachSheetName(sheetName) {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const rows = rowsFromSheet_(sheet);
    rows.forEach(function eachRow(row) {
      if (asString_(row.batch_id).trim() !== batchId) {
        return;
      }

      const objectType = normalizeObjectType_(row.object_type);
      const recordId = asString_(row.record_id).trim();
      const validation = validationByRecord[recordId] || {};
      const approval = approvalByRecord[recordId] || {};
      const warningArray = parseWarnings_(validation.warnings || row.warning_message);

      generatedCounts[objectType] = (generatedCounts[objectType] || 0) + 1;
      records.push({
        record_id: recordId,
        object_type: objectType,
        title: asString_(row.title),
        preview_summary: asString_(row.preview_summary),
        validation_status: asString_(validation.validation_status || row.validation_status || 'warning'),
        warnings: warningArray,
        blocked_reason: asString_(validation.blocked_reason || row.blocked_reason || ''),
        import_decision: asString_(approval.import_decision || row.import_decision || 'pending_review'),
        deployable: asString_(validation.validation_status || row.validation_status || '') !== 'failed',
        conditions: parseJsonCell_(row.conditions_json),
        actions: parseJsonCell_(row.actions_json),
        approved_by: asString_(approval.approved_by || row.approved_by || ''),
        approved_at: asString_(approval.approved_at || row.approved_at || ''),
        deployment_status: asString_(row.deployment_status || 'pending'),
        zendesk_object_id: asString_(row.zendesk_object_id || '')
      });
    });
  });

  const planningSummary = readPlanningSummary_(spreadsheet, batchId);
  const validationSummary = summarizeValidationForPreview_(records);

  return {
    ok: true,
    action: 'get_batch_preview',
    batch_id: batchId,
    status: 'preview_ready',
    planning_summary: planningSummary,
    generated_counts: generatedCounts,
    validation_summary: validationSummary,
    records: records
  };
}

function readPlanningSummary_(spreadsheet, batchId) {
  const sheet = spreadsheet.getSheetByName('Planning Output');
  if (!sheet) {
    return {};
  }

  const rows = rowsFromSheet_(sheet).filter(function eachRow(row) {
    return asString_(row.batch_id).trim() === batchId;
  });
  if (rows.length === 0) {
    return {};
  }

  const latest = rows[rows.length - 1];
  return {
    object_type: asString_(latest.object_type),
    intent: asString_(latest.intent),
    confidence: asString_(latest.confidence)
  };
}

function loadLatestValidationByRecord_(spreadsheet, batchId) {
  const sheet = spreadsheet.getSheetByName('Validation Log');
  if (!sheet) {
    return {};
  }

  const map = {};
  rowsFromSheet_(sheet).forEach(function eachRow(row) {
    if (asString_(row.batch_id).trim() !== batchId) {
      return;
    }
    const recordId = asString_(row.record_id).trim();
    if (!recordId) {
      return;
    }
    map[recordId] = row;
  });
  return map;
}

function loadLatestApprovalByRecord_(spreadsheet, batchId) {
  const sheet = spreadsheet.getSheetByName('Approval Log');
  if (!sheet) {
    return {};
  }

  const map = {};
  rowsFromSheet_(sheet).forEach(function eachRow(row) {
    if (asString_(row.batch_id).trim() !== batchId) {
      return;
    }
    const recordId = asString_(row.record_id).trim();
    if (!recordId) {
      return;
    }
    map[recordId] = row;
  });
  return map;
}

function summarizeValidationForPreview_(records) {
  const summary = { passed: 0, warnings: 0, blocked: 0 };
  records.forEach(function eachRecord(record) {
    if (record.validation_status === 'passed') {
      summary.passed += 1;
    } else if (record.validation_status === 'warning') {
      summary.warnings += 1;
    } else {
      summary.blocked += 1;
    }
  });
  return summary;
}
