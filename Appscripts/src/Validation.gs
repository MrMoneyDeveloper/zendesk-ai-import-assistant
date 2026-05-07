function validateBatch(payload) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);

  try {
    const batchId = requireBatchId_(payload && payload.batch_id);
    const spreadsheet = openManagedSpreadsheet_();
    ensureRequiredTabs_(spreadsheet);

    const validationRows = [];
    const summary = { passed: 0, warnings: 0, blocked: 0 };

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
        const record = {
          record_id: asString_(row.record_id).trim(),
          title: asString_(row.title).trim(),
          object_type: objectType,
          conditions: parseJsonCell_(row.conditions_json),
          actions: parseJsonCell_(row.actions_json),
          warning_message: asString_(row.warning_message).trim(),
          blocked_reason: asString_(row.blocked_reason).trim()
        };

        const result = validateRecordByType_(record);
        if (result.validation_status === 'passed') {
          summary.passed += 1;
        } else if (result.validation_status === 'warning') {
          summary.warnings += 1;
        } else {
          summary.blocked += 1;
        }

        validationRows.push({
          batch_id: batchId,
          record_id: record.record_id,
          object_type: record.object_type,
          title: record.title,
          validation_status: result.validation_status,
          warnings: JSON.stringify(result.warnings),
          blocked_reason: result.blocked_reason,
          checked_at: nowIso_()
        });
      });
    });

    const validationSheet = getSheetByNameOrCreate_(spreadsheet, 'Validation Log');
    const validationHeaders = getTabDefinitionByName_('Validation Log').headers;
    appendRowsByHeader_(validationSheet, validationHeaders, validationRows);

    getScriptProperties_().setProperty(APP_CONFIG.LAST_VALIDATION_AT_PROPERTY, nowIso_());

    return {
      ok: true,
      action: 'validate_batch',
      batch_id: batchId,
      summary: summary,
      validated_rows: validationRows.length,
      validated_at: getScriptProperties_().getProperty(APP_CONFIG.LAST_VALIDATION_AT_PROPERTY)
    };
  } finally {
    lock.releaseLock();
  }
}

function validateRecordByType_(record) {
  const warnings = [];
  const blocking = [];
  const objectType = normalizeObjectType_(record.object_type);
  const title = asString_(record.title).trim();

  if (!title) {
    blocking.push('Missing title.');
  }

  if (objectType === 'triggers') {
    validateTriggersRecord_(record, warnings, blocking);
  } else if (objectType === 'macros') {
    validateMacroRecord_(record, warnings, blocking);
  } else if (objectType === 'views') {
    validateViewRecord_(record, warnings, blocking);
  } else if (objectType === 'ticket_fields') {
    validateTicketFieldRecord_(record, warnings, blocking);
  } else if (objectType === 'ticket_forms') {
    validateTicketFormRecord_(record, warnings, blocking);
  } else if (objectType === 'tag_dictionary') {
    validateTagDictionaryRecord_(record, warnings, blocking);
  } else if (objectType === 'recommendations') {
    warnings.push('Recommendation item is not deployable in this phase.');
  }

  let validationStatus = 'passed';
  const uniqueWarnings = uniqueList_(warnings);
  const uniqueBlocking = uniqueList_(blocking);
  if (uniqueBlocking.length > 0) {
    validationStatus = 'failed';
  } else if (uniqueWarnings.length > 0) {
    validationStatus = 'warning';
  }

  return {
    validation_status: validationStatus,
    warnings: uniqueWarnings,
    blocked_reason: uniqueBlocking.join(' | ')
  };
}

function validateTriggersRecord_(record, warnings, blocking) {
  if (!Array.isArray(record.conditions) || record.conditions.length === 0) {
    blocking.push('Trigger must have at least one condition.');
  }
  if (!Array.isArray(record.actions) || record.actions.length === 0) {
    blocking.push('Trigger must have at least one action.');
  }

  if (Array.isArray(record.conditions)) {
    record.conditions.forEach(function eachCondition(condition) {
      const field = asString_(condition.field).trim();
      const operator = asString_(condition.operator).trim();
      if (!field || !operator) {
        warnings.push('Trigger condition is missing field/operator.');
      }
    });
  }

  if (Array.isArray(record.actions)) {
    record.actions.forEach(function eachAction(action) {
      const field = asString_(action.field).trim();
      if (!field) {
        warnings.push('Trigger action is missing target field.');
      }
      if (field.indexOf('group') >= 0 && !asString_(action.value).trim()) {
        blocking.push('Trigger group assignment is missing group value.');
      }
    });
  }
}

function validateMacroRecord_(record, warnings, blocking) {
  if (!Array.isArray(record.actions) || record.actions.length === 0) {
    blocking.push('Macro must have at least one action.');
    return;
  }

  record.actions.forEach(function eachAction(action) {
    if (!asString_(action.field).trim()) {
      warnings.push('Macro action missing field.');
    }
    if (action.value === undefined || action.value === null || asString_(action.value).trim() === '') {
      warnings.push('Macro action missing value.');
    }
  });
}

function validateViewRecord_(record, warnings, blocking) {
  if (!Array.isArray(record.conditions) || record.conditions.length === 0) {
    blocking.push('View must define at least one condition.');
  }
  if (Array.isArray(record.actions) && record.actions.length === 0) {
    warnings.push('View has no explicit output actions/columns.');
  }
}

function validateTicketFieldRecord_(record, warnings, blocking) {
  const typeCondition = Array.isArray(record.conditions)
    ? record.conditions.find(function findType(item) {
      return asString_(item.field).trim() === 'type';
    })
    : null;

  if (!typeCondition) {
    warnings.push('Ticket field type was not provided in conditions.');
  }
}

function validateTicketFormRecord_(record, warnings, blocking) {
  if ((!Array.isArray(record.conditions) || record.conditions.length === 0) &&
      (!Array.isArray(record.actions) || record.actions.length === 0)) {
    blocking.push('Ticket form has no field mapping content.');
  }
}

function validateTagDictionaryRecord_(record, warnings, blocking) {
  const normalizedTag = record.title.toLowerCase().replace(/\s+/g, '_');
  if (!normalizedTag || normalizedTag.length < 2) {
    blocking.push('Tag entry is missing a valid tag name.');
  }
  if (normalizedTag !== record.title && record.title.indexOf(' ') >= 0) {
    warnings.push('Tag dictionary title contains spaces; snake_case is recommended.');
  }
}
