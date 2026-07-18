function nowIso_() {
  return new Date().toISOString();
}

let MANAGED_SPREADSHEET_CACHE_ = null;
let REQUIRED_TABS_READY_THIS_EXECUTION_ = {};
let SHEET_ROWS_CACHE_ = {};

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
  if (MANAGED_SPREADSHEET_CACHE_) {
    return MANAGED_SPREADSHEET_CACHE_;
  }

  const props = getScriptProperties_();
  let sheetId = String(props.getProperty(APP_CONFIG.SHEET_ID_PROPERTY) || '').trim();
  if (sheetId) {
    MANAGED_SPREADSHEET_CACHE_ = SpreadsheetApp.openById(sheetId);
    return MANAGED_SPREADSHEET_CACHE_;
  }

  const sheetName = String(props.getProperty('APPS_SCRIPT_SHEET_NAME') || APP_CONFIG.DEFAULT_SPREADSHEET_NAME).trim();
  const spreadsheet = SpreadsheetApp.create(sheetName || APP_CONFIG.DEFAULT_SPREADSHEET_NAME);
  sheetId = spreadsheet.getId();
  props.setProperty(APP_CONFIG.SHEET_ID_PROPERTY, sheetId);
  MANAGED_SPREADSHEET_CACHE_ = spreadsheet;
  return MANAGED_SPREADSHEET_CACHE_;
}

function getSheetByNameOrCreate_(spreadsheet, name) {
  let sheet = spreadsheet.getSheetByName(name);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(name);
    invalidateRequiredTabsState_(spreadsheet);
  }
  return sheet;
}

function ensureHeader_(sheet, headers) {
  const hasHeader = sheet.getLastRow() >= 1 && sheet.getRange(1, 1).getValue() !== '';
  if (!hasHeader) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    invalidateRowsCache_(sheet);
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
    invalidateRowsCache_(sheet);
  }
}

function getSheetHeaders_(sheet, requiredHeaders) {
  ensureHeader_(sheet, requiredHeaders || []);
  const width = Math.max(sheet.getLastColumn(), (requiredHeaders || []).length, 1);
  return sheet.getRange(1, 1, 1, width).getValues()[0].map(function mapHeader(value) {
    return asString_(value).trim();
  });
}

function valueForSheetCell_(value) {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return value;
}

function mergedRowValues_(headers, row, baseValues) {
  const values = Array.isArray(baseValues)
    ? baseValues.slice(0, headers.length)
    : headers.map(function emptyValue() { return ''; });
  while (values.length < headers.length) {
    values.push('');
  }
  headers.forEach(function eachHeader(header, index) {
    if (Object.prototype.hasOwnProperty.call(row || {}, header)) {
      values[index] = valueForSheetCell_(row[header]);
    }
  });
  return values;
}

function rowValuesWithFormulas_(values, formulas) {
  return values.map(function eachValue(value, index) {
    return formulas && formulas[index] ? formulas[index] : value;
  });
}

function writeRowsByNumber_(sheet, rowsByNumber, width) {
  const rowNumbers = Object.keys(rowsByNumber || {}).map(function eachKey(key) {
    return Number(key);
  }).filter(function validRow(rowNumber) {
    return rowNumber >= 2;
  }).sort(function sortRows(a, b) {
    return a - b;
  });
  if (rowNumbers.length === 0) {
    return;
  }

  let groupStart = rowNumbers[0];
  let previous = rowNumbers[0];
  let groupValues = [rowsByNumber[groupStart]];
  for (let i = 1; i < rowNumbers.length; i += 1) {
    const current = rowNumbers[i];
    if (current === previous + 1) {
      groupValues.push(rowsByNumber[current]);
    } else {
      sheet.getRange(groupStart, 1, groupValues.length, width).setValues(groupValues);
      groupStart = current;
      groupValues = [rowsByNumber[current]];
    }
    previous = current;
  }
  sheet.getRange(groupStart, 1, groupValues.length, width).setValues(groupValues);
  invalidateRowsCache_(sheet);
}

function appendRowsByHeader_(sheet, headers, rows) {
  if (!rows || rows.length === 0) {
    return 0;
  }

  const sheetHeaders = getSheetHeaders_(sheet, headers);
  const values = rows.map(function mapRow(row) {
    return mergedRowValues_(sheetHeaders, row, null);
  });

  const startRow = sheet.getLastRow() + 1;
  sheet.getRange(startRow, 1, values.length, sheetHeaders.length).setValues(values);
  invalidateRowsCache_(sheet);
  return values.length;
}

function rowValuesByHeader_(headers, row) {
  return mergedRowValues_(headers, row, null);
}

function upsertRowsByKeys_(sheet, headers, rows, keyHeaders) {
  if (!rows || rows.length === 0) {
    return 0;
  }
  const sheetHeaders = getSheetHeaders_(sheet, headers);
  const lastRow = sheet.getLastRow();
  const width = sheetHeaders.length;
  const values = lastRow > 1
    ? sheet.getRange(2, 1, lastRow - 1, width).getValues()
    : [];
  const formulas = lastRow > 1
    ? sheet.getRange(2, 1, lastRow - 1, width).getFormulas()
    : [];
  const indexes = {};
  keyHeaders.forEach(function eachKey(header) {
    indexes[header] = sheetHeaders.indexOf(header);
  });
  const rowIndexByKey = {};
  for (let i = 0; i < values.length; i += 1) {
    const key = keyHeaders.map(function eachKey(header) {
      return asString_(values[i][indexes[header]]).trim();
    }).join('::');
    if (key) {
      rowIndexByKey[key] = i + 2;
    }
  }

  const rowsByNumber = {};
  const appendValues = [];
  rows.forEach(function eachRow(row) {
    const key = keyHeaders.map(function eachKey(header) {
      return asString_(row[header]).trim();
    }).join('::');
    if (rowIndexByKey[key] !== undefined) {
      const rowNumber = rowIndexByKey[key];
      const sourceIndex = rowNumber - 2;
      const baseValues = rowValuesWithFormulas_(values[sourceIndex], formulas[sourceIndex]);
      rowsByNumber[rowNumber] = mergedRowValues_(sheetHeaders, row, baseValues);
    } else {
      appendValues.push(mergedRowValues_(sheetHeaders, row, null));
    }
  });

  writeRowsByNumber_(sheet, rowsByNumber, width);
  if (appendValues.length > 0) {
    sheet.getRange(sheet.getLastRow() + 1, 1, appendValues.length, width).setValues(appendValues);
    invalidateRowsCache_(sheet);
  }
  return rows.length;
}

function replaceBatchRows_(sheet, headers, batchId, rows) {
  const sheetHeaders = getSheetHeaders_(sheet, headers);
  const width = sheetHeaders.length;
  const previousLastRow = sheet.getLastRow();
  const existing = previousLastRow > 1
    ? sheet.getRange(2, 1, previousLastRow - 1, width).getValues()
    : [];
  const formulas = previousLastRow > 1
    ? sheet.getRange(2, 1, previousLastRow - 1, width).getFormulas()
    : [];
  const batchIndex = sheetHeaders.indexOf('batch_id');
  const recordIndex = sheetHeaders.indexOf('record_id');
  const existingByRecordId = {};
  const availableRows = [];
  const incomingRecordIds = {};
  (rows || []).forEach(function eachIncoming(row) {
    const recordId = asString_(row.record_id).trim();
    if (recordId) {
      incomingRecordIds[recordId] = true;
    }
  });
  existing.forEach(function eachExisting(row, index) {
    if (asString_(row[batchIndex]).trim() !== batchId) {
      return;
    }
    const rowNumber = index + 2;
    const recordId = recordIndex >= 0 ? asString_(row[recordIndex]).trim() : '';
    if (recordId) {
      existingByRecordId[recordId] = rowNumber;
    }
    if (!recordId || !incomingRecordIds[recordId]) {
      availableRows.push(rowNumber);
    }
  });

  const usedRows = {};
  const rowsByNumber = {};
  const appendValues = [];
  (rows || []).forEach(function eachReplacement(row) {
    const recordId = asString_(row.record_id).trim();
    let rowNumber = recordId ? existingByRecordId[recordId] : null;
    if (!rowNumber) {
      while (availableRows.length > 0 && usedRows[availableRows[0]]) {
        availableRows.shift();
      }
      rowNumber = availableRows.length > 0 ? availableRows.shift() : null;
    }
    if (rowNumber) {
      usedRows[rowNumber] = true;
      const sourceIndex = rowNumber - 2;
      const baseValues = rowValuesWithFormulas_(existing[sourceIndex], formulas[sourceIndex]);
      rowsByNumber[rowNumber] = mergedRowValues_(sheetHeaders, row, baseValues);
    } else {
      appendValues.push(mergedRowValues_(sheetHeaders, row, null));
    }
  });

  existing.forEach(function clearStaleBatchRow(row, index) {
    const rowNumber = index + 2;
    if (asString_(row[batchIndex]).trim() !== batchId || usedRows[rowNumber]) {
      return;
    }
    const baseValues = rowValuesWithFormulas_(row, formulas[index]);
    headers.forEach(function eachManagedHeader(header) {
      const columnIndex = sheetHeaders.indexOf(header);
      if (columnIndex >= 0) {
        baseValues[columnIndex] = '';
      }
    });
    rowsByNumber[rowNumber] = baseValues;
  });

  writeRowsByNumber_(sheet, rowsByNumber, width);
  if (appendValues.length > 0) {
    sheet.getRange(sheet.getLastRow() + 1, 1, appendValues.length, width).setValues(appendValues);
    invalidateRowsCache_(sheet);
  }
  return (rows || []).length;
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

function requiredTabsStateKey_(spreadsheet) {
  return spreadsheet.getId() + ':' + sha256Base64_(JSON.stringify(REQUIRED_TABS));
}

function invalidateRequiredTabsState_(spreadsheet) {
  const spreadsheetId = spreadsheet ? spreadsheet.getId() : '';
  Object.keys(REQUIRED_TABS_READY_THIS_EXECUTION_).forEach(function eachKey(key) {
    if (!spreadsheetId || key.indexOf(spreadsheetId + ':') === 0) {
      delete REQUIRED_TABS_READY_THIS_EXECUTION_[key];
    }
  });
  getScriptProperties_().deleteProperty('APPS_SCRIPT_REQUIRED_TABS_STATE');
}

function markRequiredTabsReady_(spreadsheet) {
  const stateKey = requiredTabsStateKey_(spreadsheet);
  REQUIRED_TABS_READY_THIS_EXECUTION_[stateKey] = true;
  getScriptProperties_().setProperty('APPS_SCRIPT_REQUIRED_TABS_STATE', JSON.stringify({
    key: stateKey,
    checked_at_ms: Date.now()
  }));
}

function ensureRequiredTabs_(spreadsheet, force) {
  const stateKey = requiredTabsStateKey_(spreadsheet);
  if (!force && REQUIRED_TABS_READY_THIS_EXECUTION_[stateKey]) {
    return;
  }

  if (!force) {
    try {
      const stored = JSON.parse(
        getScriptProperties_().getProperty('APPS_SCRIPT_REQUIRED_TABS_STATE') || '{}'
      );
      const ageMs = Date.now() - Number(stored.checked_at_ms || 0);
      if (stored.key === stateKey && ageMs >= 0 && ageMs < 10 * 60 * 1000) {
        REQUIRED_TABS_READY_THIS_EXECUTION_[stateKey] = true;
        return;
      }
    } catch (error) {
    }
  }

  const existingByName = {};
  spreadsheet.getSheets().forEach(function eachSheet(sheet) {
    existingByName[sheet.getName()] = sheet;
  });
  REQUIRED_TABS.forEach(function eachTab(tabDef) {
    let sheet = existingByName[tabDef.name];
    if (!sheet) {
      sheet = spreadsheet.insertSheet(tabDef.name);
      existingByName[tabDef.name] = sheet;
    }
    ensureHeader_(sheet, tabDef.headers);
  });
  markRequiredTabsReady_(spreadsheet);
}

function indexByHeaders_(headers) {
  const idx = {};
  headers.forEach(function eachHeader(header, i) {
    idx[asString_(header)] = i;
  });
  return idx;
}

function rowsFromSheet_(sheet) {
  const cacheKey = sheet.getParent().getId() + ':' + sheet.getSheetId();
  if (Object.prototype.hasOwnProperty.call(SHEET_ROWS_CACHE_, cacheKey)) {
    return SHEET_ROWS_CACHE_[cacheKey];
  }
  const values = sheet.getDataRange().getValues();
  if (!values || values.length <= 1) {
    SHEET_ROWS_CACHE_[cacheKey] = [];
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
  SHEET_ROWS_CACHE_[cacheKey] = rows;
  return SHEET_ROWS_CACHE_[cacheKey];
}

function invalidateRowsCache_(sheet) {
  if (!sheet) {
    SHEET_ROWS_CACHE_ = {};
    return;
  }
  const cacheKey = sheet.getParent().getId() + ':' + sheet.getSheetId();
  delete SHEET_ROWS_CACHE_[cacheKey];
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

