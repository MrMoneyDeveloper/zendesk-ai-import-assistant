function doGet(e) {
  try {
    const action = getRequestParam_(e, 'action') || 'health';

    if (action === 'health') {
      return jsonResponse_({
        ok: true,
        action: 'health',
        service: APP_CONFIG.APP_NAME,
        now: nowIso_(),
        schema: getSchemaBundleInfo()
      });
    }

    const apiKey = getRequestParam_(e, 'api_key');
    requireApiKey_(apiKey);

    if (action === 'schema_info') {
      return jsonResponse_({ ok: true, action: 'schema_info', data: getSchemaBundleInfo() });
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
      return jsonResponse_(updateApprovalStatus(payload));
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
