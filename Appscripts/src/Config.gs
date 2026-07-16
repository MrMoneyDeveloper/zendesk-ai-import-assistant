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
  LAST_VALIDATION_AT_PROPERTY: 'LAST_VALIDATION_AT',
  SECRETS_TAB_NAME: 'Integration Secrets'
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
  'zendesk_object_id',
  'chunk_id',
  'record_key',
  'department',
  'topic',
  'source_provider',
  'source_model'
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
    name: 'Brands',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Groups',
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
    name: 'Automations',
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
    name: 'Categories',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Sections',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Articles',
    headers: OBJECT_RECORD_HEADERS
  },
  {
    name: 'Batch Metadata',
    headers: [
      'batch_id',
      'status',
      'coverage_status',
      'coverage_manifest_json',
      'department_coverage_json',
      'quality_gates_json',
      'supervisor_json',
      'llm_routes_json',
      'usage_report_json',
      'orchestration_json',
      'progress_narration_json',
      'updated_at'
    ]
  },
  {
    name: 'Progress Log',
    headers: [
      'batch_id',
      'event_id',
      'status',
      'wave',
      'department',
      'object_type',
      'message',
      'source',
      'provider',
      'model',
      'at'
    ]
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
    name: 'Integration Secrets',
    headers: ['key_name', 'key_value', 'generated_at', 'generated_by', 'note', 'is_active']
  },
  {
    name: 'Schema Registry',
    headers: ['schema_name', 'schema_version', 'hash', 'updated_at', 'schema_json']
  }
]);

const OBJECT_SHEET_NAMES = Object.freeze([
  'Brands',
  'Groups',
  'Ticket Fields',
  'Ticket Forms',
  'Macros',
  'Triggers',
  'Automations',
  'Views',
  'Categories',
  'Sections',
  'Articles',
  'Tag Dictionary',
  'Recommendations'
]);

const OBJECT_TAB_BY_TYPE = Object.freeze({
  brand: 'Brands',
  brands: 'Brands',
  group: 'Groups',
  groups: 'Groups',
  ticket_field: 'Ticket Fields',
  ticket_fields: 'Ticket Fields',
  ticket_form: 'Ticket Forms',
  ticket_forms: 'Ticket Forms',
  macro: 'Macros',
  macros: 'Macros',
  trigger: 'Triggers',
  triggers: 'Triggers',
  automation: 'Automations',
  automations: 'Automations',
  view: 'Views',
  views: 'Views',
  category: 'Categories',
  categories: 'Categories',
  section: 'Sections',
  sections: 'Sections',
  article: 'Articles',
  articles: 'Articles',
  tag_dictionary: 'Tag Dictionary',
  recommendation: 'Recommendations',
  recommendations: 'Recommendations'
});

const CANONICAL_OBJECT_TYPE_BY_TYPE = Object.freeze({
  brand: 'brands',
  brands: 'brands',
  group: 'groups',
  groups: 'groups',
  ticket_field: 'ticket_fields',
  ticket_fields: 'ticket_fields',
  ticket_form: 'ticket_forms',
  ticket_forms: 'ticket_forms',
  macro: 'macros',
  macros: 'macros',
  trigger: 'triggers',
  triggers: 'triggers',
  automation: 'automations',
  automations: 'automations',
  view: 'views',
  views: 'views',
  category: 'categories',
  categories: 'categories',
  section: 'sections',
  sections: 'sections',
  article: 'articles',
  articles: 'articles',
  tag_dictionary: 'tag_dictionary',
  recommendation: 'recommendations',
  recommendations: 'recommendations'
});

const DEPLOYABLE_OBJECT_TYPES = Object.freeze({
  brand: true,
  brands: true,
  group: true,
  groups: true,
  ticket_field: true,
  ticket_fields: true,
  ticket_form: true,
  ticket_forms: true,
  macro: true,
  macros: true,
  trigger: true,
  triggers: true,
  automation: true,
  automations: true,
  view: true,
  views: true,
  category: true,
  categories: true,
  section: true,
  sections: true,
  article: true,
  articles: true,
  tag_dictionary: false,
  recommendation: false,
  recommendations: false
});


