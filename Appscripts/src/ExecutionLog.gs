function writeExecutionLog(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const input = payload || {};
    const batchId = requireBatchId_(input.batch_id);
    const results = Array.isArray(input.results) ? input.results : [];
    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);

    const executionSheet = getSheetByNameOrCreate_(spreadsheet, 'Execution Log');
    const executionHeaders = getTabDefinitionByName_('Execution Log').headers;

    const rows = results.map(function mapResult(result) {
      return {
        batch_id: batchId,
        record_id: asString_(result.record_id),
        object_type: normalizeObjectType_(result.object_type),
        title: asString_(result.title),
        deployment_status: asString_(result.deployment_status || 'pending'),
        zendesk_object_id: asString_(result.zendesk_object_id || ''),
        execution_message: asString_(result.execution_message || ''),
        executed_at: asString_(result.executed_at || nowIso_())
      };
    });

    const writtenRows = appendRowsByHeader_(executionSheet, executionHeaders, rows);
    applyExecutionStatusToObjectTabs_(spreadsheet, batchId, rows);

    return {
      ok: true,
      action: 'write_execution_log',
      batch_id: batchId,
      written_rows: writtenRows
    };
  } finally {
    lock.releaseLock();
  }
}

function getExecutionSummary(payload) {
  const batchId = requireBatchId_(payload && payload.batch_id);
  const spreadsheet = openManagedSpreadsheet_();
  ensureRequiredTabs_(spreadsheet);

  const sheet = spreadsheet.getSheetByName('Execution Log');
  if (!sheet) {
    return {
      ok: true,
      action: 'get_execution_summary',
      batch_id: batchId,
      summary: { deployed: 0, failed: 0, skipped: 0, pending: 0 },
      results: []
    };
  }

  const latestByRecord = {};
  rowsFromSheet_(sheet).forEach(function eachRow(row) {
    if (asString_(row.batch_id).trim() !== batchId) {
      return;
    }
    const recordId = asString_(row.record_id).trim();
    if (recordId) {
      latestByRecord[recordId] = row;
    }
  });
  const rows = Object.keys(latestByRecord).map(function eachRecordId(recordId) {
    return latestByRecord[recordId];
  });

  const summary = { deployed: 0, failed: 0, skipped: 0, pending: 0 };
  const results = rows.map(function mapRow(row) {
    const deploymentStatus = asString_(row.deployment_status).trim() || 'pending';
    if (summary[deploymentStatus] === undefined) {
      summary[deploymentStatus] = 0;
    }
    summary[deploymentStatus] += 1;
    return {
      record_id: asString_(row.record_id),
      object_type: asString_(row.object_type),
      title: asString_(row.title),
      deployment_status: deploymentStatus,
      zendesk_object_id: asString_(row.zendesk_object_id),
      execution_message: asString_(row.execution_message),
      executed_at: asString_(row.executed_at)
    };
  });

  return {
    ok: true,
    action: 'get_execution_summary',
    batch_id: batchId,
    summary: summary,
    results: results
  };
}

function applyExecutionStatusToObjectTabs_(spreadsheet, batchId, executionRows) {
  const byRecord = {};
  executionRows.forEach(function eachRow(row) {
    const recordId = asString_(row.record_id).trim();
    if (!recordId) {
      return;
    }
    byRecord[recordId] = row;
  });

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
    const deploymentCol = idx.deployment_status;
    const objectIdCol = idx.zendesk_object_id;
    if (deploymentCol === undefined || objectIdCol === undefined) {
      return;
    }

    let touched = false;
    for (let row = 1; row < values.length; row += 1) {
      const rowBatchId = asString_(values[row][idx.batch_id]).trim();
      if (rowBatchId !== batchId) {
        continue;
      }
      const recordId = asString_(values[row][idx.record_id]).trim();
      if (!byRecord[recordId]) {
        continue;
      }

      values[row][deploymentCol] = asString_(byRecord[recordId].deployment_status || 'pending');
      values[row][objectIdCol] = asString_(byRecord[recordId].zendesk_object_id || '');
      touched = true;
    }

    if (touched) {
      sheet.getRange(1, 1, values.length, values[0].length).setValues(values);
    }
  });
}
