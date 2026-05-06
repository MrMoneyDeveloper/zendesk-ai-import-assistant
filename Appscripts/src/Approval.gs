function updateApprovalStatus(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const input = payload || {};
    const batchId = requireBatchId_(input.batch_id);
    const approvedBy = asString_(input.approved_by || 'local-user');
    const decisions = Array.isArray(input.records) ? input.records : [];

    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);

    const approvalSheet = getSheetByNameOrCreate_(spreadsheet, 'Approval Log');
    const approvalHeaders = getTabDefinitionByName_('Approval Log').headers;

    const rows = [];
    const summary = { approved: 0, skipped: 0, edit_later: 0, blocked: 0 };
    const decisionMap = {};

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
    });

    Object.keys(decisionMap).forEach(function eachRecordId(recordId) {
      rows.push({
        batch_id: batchId,
        record_id: recordId,
        import_decision: decisionMap[recordId].import_decision,
        approved_at: decisionMap[recordId].approved_at,
        approved_by: decisionMap[recordId].approved_by
      });
    });

    appendRowsByHeader_(approvalSheet, approvalHeaders, rows);
    applyApprovalToObjectTabs_(spreadsheet, batchId, decisionMap);

    return {
      ok: true,
      action: 'update_approval_status',
      batch_id: batchId,
      updated_records: rows.length,
      summary: summary
    };
  } finally {
    lock.releaseLock();
  }
}

function applyApprovalToObjectTabs_(spreadsheet, batchId, decisionMap) {
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
