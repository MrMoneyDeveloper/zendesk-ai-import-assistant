function jsonForSheetCell_(value, keyName) {
  const source = value && typeof value === 'object' ? value : {};
  let compact = source;

  if (keyName === 'supervisor') {
    compact = {
      enabled: source.enabled,
      available: source.available,
      model: source.model,
      call_counts: source.call_counts || {},
      patch_counts: source.patch_counts || {},
      blocked_chunk_ids: source.blocked_chunk_ids || [],
      regeneration_attempts: source.regeneration_attempts || [],
      verified_memory: source.verified_memory || [],
      reviews: (Array.isArray(source.reviews) ? source.reviews : []).map(function eachReview(review) {
        return {
          review_unit_id: review.review_unit_id,
          bundle_key: review.bundle_key,
          wave: review.wave,
          raw_quality_score: review.raw_quality_score,
          effective_quality_score: review.effective_quality_score,
          effective_approved: review.effective_approved,
          requires_regeneration: review.requires_regeneration,
          public_reasoning_summary: review.public_reasoning_summary,
          approval_gate_reasons: review.approval_gate_reasons || []
        };
      })
    };
  } else if (keyName === 'usage_report') {
    compact = {
      schema_version: source.schema_version,
      label: source.label,
      started_at: source.started_at,
      finished_at: source.finished_at,
      totals: source.totals || {},
      phase_timings: source.phase_timings || {},
      by_provider: source.by_provider || [],
      by_model: source.by_model || [],
      by_task: source.by_task || []
    };
  }

  let serialized = JSON.stringify(compact || {});
  if (serialized.length <= 48000) {
    return serialized;
  }
  serialized = JSON.stringify({
    truncated: true,
    original_chars: serialized.length,
    keys: Object.keys(compact || {}),
    note: 'Full value remains in the FastAPI batch store; this Sheet cell contains a size-safe summary.'
  });
  return serialized;
}

function writeBatchMetadataUnlocked_(spreadsheet, payload) {
  const input = payload || {};
  const batchId = requireBatchId_(input.batch_id);
  const metadata = input.metadata && typeof input.metadata === 'object' ? input.metadata : {};
  const qualityGates = metadata.quality_gates || {};
  const departmentCoverage = metadata.department_coverage || {};
  const coverageGate = qualityGates.coverage || {};
  const sheet = getSheetByNameOrCreate_(spreadsheet, 'Batch Metadata');
  const headers = getTabDefinitionByName_('Batch Metadata').headers;
  return upsertRowsByKeys_(sheet, headers, [
    {
      batch_id: batchId,
      status: asString_(input.status || 'staging'),
      coverage_status: asString_(coverageGate.status || departmentCoverage.status || 'unknown'),
      coverage_manifest_json: jsonForSheetCell_(metadata.coverage_manifest || {}, 'coverage_manifest'),
      department_coverage_json: jsonForSheetCell_(departmentCoverage, 'department_coverage'),
      quality_gates_json: jsonForSheetCell_(qualityGates, 'quality_gates'),
      supervisor_json: jsonForSheetCell_(metadata.supervisor || {}, 'supervisor'),
      llm_routes_json: jsonForSheetCell_(metadata.llm_routes || {}, 'llm_routes'),
      usage_report_json: jsonForSheetCell_(metadata.usage_report || {}, 'usage_report'),
      orchestration_json: jsonForSheetCell_(metadata.orchestration || {}, 'orchestration'),
      progress_narration_json: jsonForSheetCell_(metadata.progress_narration || {}, 'progress_narration'),
      updated_at: nowIso_()
    }
  ], ['batch_id']);
}

function writeBatchMetadata(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);
    const written = writeBatchMetadataUnlocked_(spreadsheet, payload || {});
    return {
      ok: true,
      action: 'write_batch_metadata',
      batch_id: requireBatchId_(payload && payload.batch_id),
      rows_written: written
    };
  } finally {
    lock.releaseLock();
  }
}

function appendProgressEventsUnlocked_(spreadsheet, payload) {
  const input = payload || {};
  const batchId = requireBatchId_(input.batch_id);
  const events = Array.isArray(input.events) ? input.events : [];
  if (events.length === 0) {
    return 0;
  }
  const sheet = getSheetByNameOrCreate_(spreadsheet, 'Progress Log');
  const headers = getTabDefinitionByName_('Progress Log').headers;
  const rows = events.map(function eachEvent(event, index) {
    const at = asString_(event.at || nowIso_());
    const message = asString_(event.message || '');
    const eventId = asString_(event.event_id || '').trim()
      || sha256Base64_([batchId, at, message, index].join('|')).substring(0, 24);
    return {
      batch_id: batchId,
      event_id: eventId,
      status: asString_(event.status || 'wave_execution'),
      wave: asString_(event.wave || ''),
      department: asString_(event.department || ''),
      object_type: asString_(event.object_type || ''),
      message: message,
      source: asString_(event.source || 'deterministic'),
      provider: asString_(event.provider || ''),
      model: asString_(event.model || ''),
      at: at
    };
  });
  return upsertRowsByKeys_(sheet, headers, rows, ['batch_id', 'event_id']);
}

function appendProgressEvents(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);
    const written = appendProgressEventsUnlocked_(spreadsheet, payload || {});
    return {
      ok: true,
      action: 'append_progress_events',
      batch_id: requireBatchId_(payload && payload.batch_id),
      rows_written: written
    };
  } finally {
    lock.releaseLock();
  }
}

function getBatchOperationalState(payload) {
  const batchId = requireBatchId_(payload && payload.batch_id);
  const spreadsheet = openManagedSpreadsheet_();
  ensureRequiredTabs_(spreadsheet);
  const metadataRows = rowsFromSheet_(spreadsheet.getSheetByName('Batch Metadata')).filter(function eachRow(row) {
    return asString_(row.batch_id).trim() === batchId;
  });
  const progressRows = rowsFromSheet_(spreadsheet.getSheetByName('Progress Log')).filter(function eachRow(row) {
    return asString_(row.batch_id).trim() === batchId;
  });
  return {
    ok: true,
    action: 'get_batch_operational_state',
    batch_id: batchId,
    metadata: metadataRows.length > 0 ? metadataRows[metadataRows.length - 1] : {},
    progress_events: progressRows.slice(-100)
  };
}
