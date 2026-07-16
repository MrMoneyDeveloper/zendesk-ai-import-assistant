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
    const rejected = [];
    const summary = { approved: 0, skipped: 0, edit_later: 0, blocked: 0 };
    const decisionMap = {};
    const recordState = loadApprovalRecordState_(spreadsheet, batchId);
    const validationByRecord = loadLatestValidationByRecord_(spreadsheet, batchId);
    const allowedDecisions = {
      approved: true,
      skipped: true,
      edit_later: true,
      blocked: true,
      pending_review: true
    };

    decisions.forEach(function eachDecision(decision) {
      const recordId = asString_(decision.record_id).trim();
      const requestedDecision = asString_(decision.import_decision || 'pending_review').trim();
      if (!recordId) {
        return;
      }
      if (!recordState[recordId]) {
        rejected.push({
          record_id: recordId,
          requested_decision: requestedDecision,
          reason: 'Record does not exist in this batch.'
        });
        return;
      }
      if (!allowedDecisions[requestedDecision]) {
        rejected.push({
          record_id: recordId,
          requested_decision: requestedDecision,
          reason: 'Unsupported approval decision.'
        });
        return;
      }

      const validation = validationByRecord[recordId] || {};
      const stored = recordState[recordId] || {};
      const validationStatus = asString_(
        validation.validation_status || stored.validation_status || ''
      ).trim().toLowerCase();
      const blockedReason = asString_(
        validation.blocked_reason || stored.blocked_reason || ''
      ).trim();
      let importDecision = requestedDecision;
      if (requestedDecision === 'approved' && (validationStatus === 'failed' || blockedReason)) {
        importDecision = 'blocked';
        rejected.push({
          record_id: recordId,
          requested_decision: requestedDecision,
          effective_decision: importDecision,
          reason: blockedReason || 'Record failed deterministic validation.'
        });
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
      summary: summary,
      rejected_decisions: rejected
    };
  } finally {
    lock.releaseLock();
  }
}

function loadApprovalRecordState_(spreadsheet, batchId) {
  const output = {};
  OBJECT_SHEET_NAMES.forEach(function eachSheetName(sheetName) {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }
    rowsFromSheet_(sheet).forEach(function eachRow(row) {
      if (asString_(row.batch_id).trim() !== batchId) {
        return;
      }
      const recordId = asString_(row.record_id).trim();
      if (recordId) {
        output[recordId] = row;
      }
    });
  });
  return output;
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
