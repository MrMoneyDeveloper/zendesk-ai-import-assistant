function initializeFromScratchFromEditor() {
  const setupResult = setupOnce({});
  const secretsLocation = debugSecretsStorageLocation();
  const rotateResult = rotateApiKeyFromEditor();

  return {
    ok: true,
    action: 'initialize_from_scratch',
    setup: setupResult,
    secrets_location: secretsLocation,
    key_rotation: rotateResult,
    next_steps: [
      'Copy APPS_SCRIPT_API_KEY from Integration Secrets sheet.',
      'Paste it into backend/.env as APPS_SCRIPT_API_KEY.',
      'Restart backend so env changes load.'
    ]
  };
}

function seedSampleBatchFromEditor() {
  const batchId = 'BATCH-DEMO-' + Utilities.formatDate(new Date(), 'UTC', 'yyyyMMdd-HHmmss');

  const writeResult = writeBatchToSheets({
    batch_id: batchId,
    prompt: 'Demo setup: claims routing with a macro and tag recommendations.',
    requester: 'editor-demo',
    status: 'staging',
    target_environment: 'sandbox',
    created_at: nowIso_(),
    planning_summary: {
      object_type: 'triggers',
      intent: 'Route claims tickets and suggest supporting tags.',
      confidence: 0.95
    },
    records: [
      {
        record_id: 'REC-0001',
        object_type: 'triggers',
        title: 'Route Claims Tickets',
        preview_summary: 'Assign claims tickets to the Claims queue.',
        conditions: [
          { field: 'ticket_form', operator: 'is', value: 'Claims' },
          { field: 'status', operator: 'is_not', value: 'closed' }
        ],
        actions: [
          { field: 'group_id', value: 'claims-team-id' },
          { field: 'add_tags', value: 'claims_intake' }
        ],
        import_decision: 'pending_review'
      },
      {
        record_id: 'REC-0002',
        object_type: 'macros',
        title: 'Claims::Request Missing Docs',
        preview_summary: 'Macro to request missing claim documents.',
        conditions: [],
        actions: [
          { field: 'comment_value_html', value: 'Please attach missing claim documents.' },
          { field: 'add_tags', value: 'claims_missing_docs' }
        ],
        import_decision: 'pending_review'
      },
      {
        record_id: 'REC-0003',
        object_type: 'tag_dictionary',
        title: 'claims_intake',
        preview_summary: 'Tag standard for new claims intake.',
        conditions: [],
        actions: [],
        import_decision: 'pending_review'
      }
    ]
  });

  const validationResult = validateBatch({ batch_id: batchId });
  const previewResult = getBatchPreview({ batch_id: batchId });

  return {
    ok: true,
    action: 'seed_sample_batch',
    batch_id: batchId,
    write: writeResult,
    validation: validationResult,
    preview_summary: previewResult.validation_summary,
    records_count: previewResult.records ? previewResult.records.length : 0
  };
}

