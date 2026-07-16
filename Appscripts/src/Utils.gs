function nowIso_() {
  return new Date().toISOString();
}

function jsonResponse_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function readJsonBody_(e) {
  if (!e || !e.postData || !e.postData.contents) {
    return {};
  }

  try {
    return JSON.parse(e.postData.contents);
  } catch (error) {
    throw new Error('Request body is not valid JSON.');
  }
}

function sha256Base64_(value) {
  const raw = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, value, Utilities.Charset.UTF_8);
  return Utilities.base64Encode(raw);
}

function getScriptProperties_() {
  return PropertiesService.getScriptProperties();
}

function requireApiKey_(providedKey) {
  const props = getScriptProperties_();
  const expected = String(props.getProperty(APP_CONFIG.API_KEY_PROPERTY) || '').trim();
  if (!expected) {
    throw new Error('Script property APPS_SCRIPT_API_KEY is not configured.');
  }
  if (!providedKey || String(providedKey).trim() !== expected) {
    throw new Error('Unauthorized: invalid APPS_SCRIPT_API_KEY.');
  }
}

function openManagedSpreadsheet_() {
  const props = getScriptProperties_();
  let sheetId = String(props.getProperty(APP_CONFIG.SHEET_ID_PROPERTY) || '').trim();
  if (sheetId) {
    return SpreadsheetApp.openById(sheetId);
  }

  const sheetName = String(props.getProperty('APPS_SCRIPT_SHEET_NAME') || APP_CONFIG.DEFAULT_SPREADSHEET_NAME).trim();
  const spreadsheet = SpreadsheetApp.create(sheetName || APP_CONFIG.DEFAULT_SPREADSHEET_NAME);
  sheetId = spreadsheet.getId();
  props.setProperty(APP_CONFIG.SHEET_ID_PROPERTY, sheetId);
  return spreadsheet;
}

function getSheetByNameOrCreate_(spreadsheet, name) {
  let sheet = spreadsheet.getSheetByName(name);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(name);
  }
  return sheet;
}

function ensureHeader_(sheet, headers) {
  const hasHeader = sheet.getLastRow() >= 1 && sheet.getRange(1, 1).getValue() !== '';
  if (!hasHeader) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    return;
  }

  const existingWidth = Math.max(sheet.getLastColumn(), 1);
  const existing = sheet.getRange(1, 1, 1, existingWidth).getValues()[0].map(function mapHeader(value) {
    return asString_(value).trim();
  });
  const missing = headers.filter(function eachHeader(header) {
    return existing.indexOf(header) < 0;
  });
  if (missing.length > 0) {
    sheet.getRange(1, existing.length + 1, 1, missing.length).setValues([missing]);
  }
}

function appendRowsByHeader_(sheet, headers, rows) {
  if (!rows || rows.length === 0) {
    return 0;
  }

  const values = rows.map(function mapRow(row) {
    return headers.map(function eachHeader(header) {
      const value = row[header];
      if (value === undefined || value === null) {
        return '';
      }
      if (typeof value === 'object') {
        return JSON.stringify(value);
      }
      return value;
    });
  });

  const startRow = sheet.getLastRow() + 1;
  sheet.getRange(startRow, 1, values.length, headers.length).setValues(values);
  return values.length;
}

function rowValuesByHeader_(headers, row) {
  return headers.map(function eachHeader(header) {
    const value = row[header];
    if (value === undefined || value === null) {
      return '';
    }
    if (typeof value === 'object') {
      return JSON.stringify(value);
    }
    return value;
  });
}

function upsertRowsByKeys_(sheet, headers, rows, keyHeaders) {
  if (!rows || rows.length === 0) {
    return 0;
  }
  ensureHeader_(sheet, headers);
  const values = sheet.getLastRow() > 0
    ? sheet.getRange(1, 1, sheet.getLastRow(), headers.length).getValues()
    : [headers.slice()];
  const indexes = {};
  keyHeaders.forEach(function eachKey(header) {
    indexes[header] = headers.indexOf(header);
  });
  const rowIndexByKey = {};
  for (let i = 1; i < values.length; i += 1) {
    const key = keyHeaders.map(function eachKey(header) {
      return asString_(values[i][indexes[header]]).trim();
    }).join('::');
    if (key) {
      rowIndexByKey[key] = i;
    }
  }

  rows.forEach(function eachRow(row) {
    const rowValues = rowValuesByHeader_(headers, row);
    const key = keyHeaders.map(function eachKey(header) {
      return asString_(row[header]).trim();
    }).join('::');
    if (rowIndexByKey[key] !== undefined) {
      values[rowIndexByKey[key]] = rowValues;
    } else {
      rowIndexByKey[key] = values.length;
      values.push(rowValues);
    }
  });

  sheet.getRange(1, 1, values.length, headers.length).setValues(values);
  return rows.length;
}

function replaceBatchRows_(sheet, headers, batchId, rows) {
  ensureHeader_(sheet, headers);
  const previousLastRow = sheet.getLastRow();
  const existing = previousLastRow > 1
    ? sheet.getRange(2, 1, previousLastRow - 1, headers.length).getValues()
    : [];
  const batchIndex = headers.indexOf('batch_id');
  const retained = existing.filter(function eachExisting(row) {
    return asString_(row[batchIndex]).trim() !== batchId;
  });
  const replacement = (rows || []).map(function eachRow(row) {
    return rowValuesByHeader_(headers, row);
  });
  const output = [headers.slice()].concat(retained, replacement);

  if (previousLastRow > 1) {
    sheet.getRange(2, 1, previousLastRow - 1, headers.length).clearContent();
  }
  sheet.getRange(1, 1, output.length, headers.length).setValues(output);
  return replacement.length;
}

function parseJsonCell_(value) {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value;
  }
  try {
    return JSON.parse(String(value));
  } catch (error) {
    return [];
  }
}

function requireBatchId_(batchId) {
  const cleaned = String(batchId || '').trim();
  if (!cleaned) {
    throw new Error('batch_id is required.');
  }
  return cleaned;
}

function getRequestParam_(e, key) {
  if (!e || !e.parameter) {
    return '';
  }
  return String(e.parameter[key] || '').trim();
}

function asString_(value) {
  if (value === null || value === undefined) {
    return '';
  }
  return String(value);
}

function getTabDefinitionByName_(name) {
  for (let i = 0; i < REQUIRED_TABS.length; i += 1) {
    if (REQUIRED_TABS[i].name === name) {
      return REQUIRED_TABS[i];
    }
  }
  return null;
}

function ensureRequiredTabs_(spreadsheet) {
  REQUIRED_TABS.forEach(function eachTab(tabDef) {
    const sheet = getSheetByNameOrCreate_(spreadsheet, tabDef.name);
    ensureHeader_(sheet, tabDef.headers);
  });
}

function indexByHeaders_(headers) {
  const idx = {};
  headers.forEach(function eachHeader(header, i) {
    idx[asString_(header)] = i;
  });
  return idx;
}

function rowsFromSheet_(sheet) {
  const values = sheet.getDataRange().getValues();
  if (!values || values.length <= 1) {
    return [];
  }

  const headers = values[0].map(function mapHeader(item) {
    return asString_(item).trim();
  });
  const rows = [];
  for (let i = 1; i < values.length; i += 1) {
    const row = {};
    for (let c = 0; c < headers.length; c += 1) {
      row[headers[c]] = values[i][c];
    }
    rows.push(row);
  }
  return rows;
}

function normalizeObjectType_(rawObjectType) {
  const normalized = asString_(rawObjectType).trim().toLowerCase();
  if (!normalized) {
    return 'recommendations';
  }
  if (CANONICAL_OBJECT_TYPE_BY_TYPE[normalized]) {
    return CANONICAL_OBJECT_TYPE_BY_TYPE[normalized];
  }
  return 'recommendations';
}

function objectTypeToSheetName_(objectType) {
  const normalized = normalizeObjectType_(objectType);
  return OBJECT_TAB_BY_TYPE[normalized] || 'Recommendations';
}

function parseWarnings_(warnings) {
  if (!warnings) {
    return [];
  }
  if (Array.isArray(warnings)) {
    return warnings.map(function eachWarning(item) {
      return asString_(item);
    });
  }
  try {
    const parsed = JSON.parse(asString_(warnings));
    if (Array.isArray(parsed)) {
      return parsed.map(function eachWarning(item) {
        return asString_(item);
      });
    }
  } catch (error) {
  }
  return [asString_(warnings)];
}

function uniqueList_(values) {
  const seen = {};
  const out = [];
  (values || []).forEach(function eachValue(item) {
    const key = asString_(item);
    if (!key || seen[key]) {
      return;
    }
    seen[key] = true;
    out.push(key);
  });
  return out;
}

function isDeployableObjectType_(objectType) {
  const normalized = normalizeObjectType_(objectType);
  if (DEPLOYABLE_OBJECT_TYPES[normalized] === undefined) {
    return false;
  }
  return Boolean(DEPLOYABLE_OBJECT_TYPES[normalized]);
}

