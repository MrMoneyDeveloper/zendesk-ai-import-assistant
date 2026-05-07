const APP_CONFIG = Object.freeze({
  APP_NAME: 'AI Zendesk Import Assistant',
  DEFAULT_SPREADSHEET_NAME: 'AI Zendesk Import Assistant Staging',
  API_KEY_PROPERTY: 'APPS_SCRIPT_API_KEY',
  SHEET_ID_PROPERTY: 'GOOGLE_SHEET_ID',
  ROOT_FOLDER_ID_PROPERTY: 'ROOT_FOLDER_ID',
  SCHEMA_BUNDLE_PROPERTY: 'FASTAPI_SCHEMA_BUNDLE_JSON',
  LAST_SCHEMA_SYNC_AT_PROPERTY: 'LAST_SCHEMA_SYNC_AT',
  LAST_SCHEMA_HASH_PROPERTY: 'LAST_SCHEMA_HASH',
  LAST_SETUP_AT_PROPERTY: 'LAST_SETUP_AT',
  LAST_VALIDATION_AT_PROPERTY: 'LAST_VALIDATION_AT'
});

const OBJECT_RECORD_HEADERS = Object.freeze([
  'batch_id',
  'record_id',
  'title',
  'object_type',
  'preview_summary',
  'conditions_json',
  'actions_json',
  'validation_status',
  'warning_message',
  'blocked_reason',
  'import_decision',
  'approved_by',
  'approved_at',
  'deployment_status',
  'zendesk_object_id'
]);

const REQUIRED_TABS = Object.freeze([
  {
    name: 'Requests',
    headers: ['batch_id', 'prompt', 'requester', 'status', 'target_environment', 'created_at']
  },
  {
    name: 'Planning Output',
    headers: ['batch_id', 'object_type', 'intent', 'confidence']
  },
  {
    name: 'Ticket Fields',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Ticket Forms',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Macros',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Triggers',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Views',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Tag Dictionary',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Recommendations',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Validation Log',
    headers: [
      'batch_id',
      'record_id',
      'object_type',
      'title',
      'validation_status',
      'warnings',
      'blocked_reason',
      'checked_at'
    ]
  },
  {
    name: 'Approval Log',
    headers: ['batch_id', 'record_id', 'import_decision', 'approved_at', 'approved_by']
  },
  {
    name: 'Execution Log',
    headers: [
      'batch_id',
      'record_id',
      'object_type',
      'title',
      'deployment_status',
      'zendesk_object_id',
      'execution_message',
      'executed_at'
    ]
  },
  {
    name: 'Schema Registry',
    headers: ['schema_name', 'schema_version', 'hash', 'updated_at', 'schema_json']
  }
]);

const OBJECT_SHEET_NAMES = Object.freeze([
  'Ticket Fields',
  'Ticket Forms',
  'Macros',
  'Triggers',
  'Views',
  'Tag Dictionary',
  'Recommendations'
]);

const OBJECT_TAB_BY_TYPE = Object.freeze({
  ticket_field: 'Ticket Fields',
  ticket_fields: 'Ticket Fields',
  ticket_form: 'Ticket Forms',
  ticket_forms: 'Ticket Forms',
  macro: 'Macros',
  macros: 'Macros',
  trigger: 'Triggers',
  triggers: 'Triggers',
  view: 'Views',
  views: 'Views',
  tag_dictionary: 'Tag Dictionary',
  recommendation: 'Recommendations',
  recommendations: 'Recommendations'
});


