from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

BatchStatus = Literal[
    "received",
    "request_validated",
    "planning",
    "planned",
    "business_blueprinting",
    "backlog_building",
    "wave_execution",
    "clarification_required",
    "schemas_selected",
    "generating",
    "supervisor_review",
    "generated",
    "staging",
    "staged",
    "validating",
    "validated_passed",
    "validated_warning",
    "validated_failed",
    "preview_ready",
    "partially_approved",
    "approved",
    "deploying",
    "deployed",
    "deployed_partial",
    "deploy_failed",
    "failed",
]

ImportDecision = Literal["pending_review", "approved", "skipped", "edit_later", "blocked"]
DependencyMode = Literal[
    "match_existing_or_create_new",
    "force_create_new",
    "force_existing_only",
]
OnExistingMode = Literal["create_new", "overwrite_existing", "skip_existing"]
OperationMode = Literal["create", "update"]
ZendeskDeploymentScope = Literal["support", "help_center", "all"]
ZendeskArticleMode = Literal["draft", "publish"]
FocusObjectType = Literal[
    "brands",
    "categories",
    "sections",
    "triggers",
    "automations",
    "macros",
    "views",
    "groups",
    "ticket_fields",
    "ticket_forms",
    "articles",
]

FOCUS_OBJECT_TYPES = {
    "brand": "brands",
    "brands": "brands",
    "category": "categories",
    "categories": "categories",
    "section": "sections",
    "sections": "sections",
    "trigger": "triggers",
    "triggers": "triggers",
    "automation": "automations",
    "automations": "automations",
    "macro": "macros",
    "macros": "macros",
    "view": "views",
    "views": "views",
    "group": "groups",
    "groups": "groups",
    "ticket_field": "ticket_fields",
    "ticket_fields": "ticket_fields",
    "ticket_form": "ticket_forms",
    "ticket_forms": "ticket_forms",
    "article": "articles",
    "articles": "articles",
}


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=12000)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 5:
            raise ValueError("Prompt must be at least 5 characters long.")
        return cleaned


class PlannerResult(BaseModel):
    object_type: str = Field(default="trigger")
    intent: str
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class GeneratedRecord(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    object_type: str = Field(default="triggers")


class ValidationErrorItem(BaseModel):
    row: int
    error: str


class ValidationResult(BaseModel):
    status: Literal["passed", "failed"]
    errors: list[ValidationErrorItem] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    plan: PlannerResult
    generated_data: list[GeneratedRecord]
    validation: ValidationResult
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApiTestResponse(BaseModel):
    provider: str
    base_url: str
    model: str
    status: Literal["ok", "error", "skipped"]
    latency_ms: float | None = None
    output_preview: str | None = None
    detail: str | None = None


class ContextReference(BaseModel):
    object_type: Literal[
        "brand",
        "group",
        "ticket_form",
        "help_center",
        "category",
        "section",
        "trigger",
        "automation",
        "macro",
        "view",
        "ticket_field",
        "article",
        "sla_policy",
        "schedule",
        "user_field",
        "organization_field",
        "custom_object",
    ]
    id: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=1200)
    catalog_key: str | None = Field(default=None, max_length=120)
    updated_at: str | None = Field(default=None, max_length=120)
    snapshot_hash: str | None = Field(default=None, max_length=128)
    snapshot: dict[str, Any] | None = None
    editable: bool = True

    @field_validator("id", "name")
    @classmethod
    def trim_string_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("description", "catalog_key", "updated_at", "snapshot_hash")
    @classmethod
    def trim_optional_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ImportAssistantGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=12000)
    target_environment: Literal["sandbox"] = "sandbox"
    mode: str = "generate_validate_preview"
    requester: str = "local-user"
    dependency_mode: DependencyMode = "match_existing_or_create_new"
    related_objects: list[ContextReference] = Field(default_factory=list)
    reference_catalog: dict[str, list[ContextReference]] = Field(default_factory=dict)
    recent_batch_context: list[str] = Field(default_factory=list)
    focus_object_types: list[FocusObjectType] = Field(default_factory=list)
    context_notes: str | None = None
    operation_mode: OperationMode = "create"
    update_target: ContextReference | None = None
    instance_sync_id: str | None = Field(default=None, max_length=160)

    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("focus_object_types", mode="before")
    @classmethod
    def normalize_focus_object_types(cls, value: object) -> list[str]:
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            source_items = [value]
        elif isinstance(value, list):
            source_items = value
        else:
            raise ValueError("focus_object_types must be a list of strings.")

        normalized: list[str] = []
        for item in source_items:
            text = str(item).strip().lower()
            if not text:
                continue
            if text == "auto":
                return []
            canonical = FOCUS_OBJECT_TYPES.get(text)
            if not canonical:
                raise ValueError(f"Unsupported focus object type: {text}")
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("instance_sync_id")
    @classmethod
    def trim_instance_sync_id(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_update_target(self) -> "ImportAssistantGenerateRequest":
        if self.operation_mode != "update":
            return self
        if self.update_target is None:
            raise ValueError("update_target is required when operation_mode is update.")
        if not self.instance_sync_id:
            raise ValueError("instance_sync_id is required when operation_mode is update.")
        if not self.update_target.editable:
            raise ValueError("The selected Zendesk object is read-only in this version.")
        if not isinstance(self.update_target.snapshot, dict) or not self.update_target.snapshot:
            raise ValueError("The selected update target must include its synchronized snapshot.")
        supported = {
            "brand",
            "category",
            "section",
            "trigger",
            "automation",
            "macro",
            "view",
            "group",
            "ticket_form",
            "ticket_field",
            "article",
        }
        if self.update_target.object_type not in supported:
            raise ValueError(
                f"Update is not supported for object type '{self.update_target.object_type}'."
            )
        return self


class ValidationSummary(BaseModel):
    passed: int = 0
    warnings: int = 0
    blocked: int = 0


class ClarificationQuestion(BaseModel):
    id: str
    question: str
    reason: str
    examples: list[str] = Field(default_factory=list)


class ImportAssistantGenerateResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    planning_summary: dict[str, Any]
    generated_counts: dict[str, int]
    validation_summary: ValidationSummary
    preview_url: str
    needs_clarification: bool = False
    clarification_questions: list[ClarificationQuestion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationSafetySummary(BaseModel):
    blocked: bool
    reasons: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    min_confidence: float = 0.0
    fallback_detected: bool = False
    focus_violations: list[str] = Field(default_factory=list)
    explicit_constraint_violations: list[str] = Field(default_factory=list)
    blocked_record_ids: list[str] = Field(default_factory=list)


class StatusHistoryItem(BaseModel):
    status: BatchStatus
    message: str = ""
    at: str
    event_id: str | None = None
    source: str | None = None
    provider: str | None = None
    model: str | None = None
    wave: int | None = None
    department: str | None = None
    object_type: str | None = None


class JobStatusResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    created_at: str
    updated_at: str
    requester: str
    target_environment: str
    mode: str
    status_history: list[StatusHistoryItem] = Field(default_factory=list)
    generated_counts: dict[str, int] = Field(default_factory=dict)
    validation_summary: ValidationSummary = Field(default_factory=ValidationSummary)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobListItem(BaseModel):
    batch_id: str
    status: BatchStatus
    created_at: str
    updated_at: str
    requester: str
    prompt_preview: str


class JobListResponse(BaseModel):
    jobs: list[JobListItem] = Field(default_factory=list)


class PreviewRecord(BaseModel):
    record_id: str
    object_type: str
    title: str
    preview_summary: str
    validation_status: Literal["passed", "warning", "failed"]
    warnings: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    import_decision: ImportDecision = "pending_review"
    deployable: bool = True
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    deployment_status: Literal["pending", "deployed", "failed", "skipped"] = "pending"
    zendesk_object_id: str | None = None
    execution_message: str = ""
    operation_mode: OperationMode = "create"
    target_object_id: str | None = None
    target_object_type: str | None = None
    target_updated_at: str | None = None
    target_snapshot_hash: str | None = None
    before_configuration: dict[str, Any] | None = None
    after_configuration: dict[str, Any] | None = None
    change_summary: list[dict[str, Any]] = Field(default_factory=list)


class PreviewResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    records: list[PreviewRecord]
    planning_summary: dict[str, Any] = Field(default_factory=dict)
    generated_counts: dict[str, int] = Field(default_factory=dict)
    validation_summary: ValidationSummary = Field(default_factory=ValidationSummary)


class ApprovalRecordDecision(BaseModel):
    record_id: str
    import_decision: Literal["approved", "skipped", "edit_later"]


class ApprovalRequest(BaseModel):
    batch_id: str
    approved_by: str = "local-user"
    records: list[ApprovalRecordDecision]


class ApprovalSummary(BaseModel):
    approved: int = 0
    skipped: int = 0
    edit_later: int = 0


class ApprovalResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    summary: ApprovalSummary
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


RunControlAction = Literal[
    "pause",
    "resume",
    "cancel",
    "pause_at_next_wave",
    "clear_pause_after_wave",
]


class RunControlRequest(BaseModel):
    action: RunControlAction
    requested_by: str = "local-user"


class RunControlResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    run_control: dict[str, Any] = Field(default_factory=dict)


CheckpointStatus = Literal["pending", "accepted", "rejected", "superseded"]
CheckpointDecision = Literal["accept", "reject"]


class CheckpointItem(BaseModel):
    checkpoint_id: str
    wave: int
    created_at: str
    status: CheckpointStatus = "pending"
    summary: dict[str, Any] = Field(default_factory=dict)
    preview_snapshot_ref: str = ""
    decision_at: str | None = None
    decision_by: str | None = None
    decision_note: str | None = None


class CheckpointListResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    checkpoints: list[CheckpointItem] = Field(default_factory=list)


class CheckpointDecisionRequest(BaseModel):
    decision: CheckpointDecision
    requested_by: str = "local-user"
    note: str = ""


class CheckpointDecisionResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    checkpoint: CheckpointItem
    rollback: dict[str, Any] = Field(default_factory=dict)
    run_control: dict[str, Any] = Field(default_factory=dict)


class AppScriptActionRequest(BaseModel):
    action: Literal[
        "setup_once",
        "sync_schema",
        "generate_api_key",
        "write_batch_to_sheets",
        "validate_batch",
        "get_batch_preview",
        "stage_validate_preview",
        "rollback_batch",
        "update_approval_status",
        "write_execution_log",
        "get_execution_summary",
        "schema_info",
        "health",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class AppScriptActionResponse(BaseModel):
    action: str
    status: Literal["ok", "error", "skipped"]
    detail: str | None = None
    http_status: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AttachmentExtractResponse(BaseModel):
    filename: str
    mime_type: str
    char_count: int
    extracted_text: str
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


class IntegrationStatusResponse(BaseModel):
    appscript: dict[str, Any] = Field(default_factory=dict)
    sheets_backend_mode: dict[str, Any] = Field(default_factory=dict)
    zendesk: dict[str, Any] = Field(default_factory=dict)


class ZendeskCredentialValidationRequest(BaseModel):
    subdomain: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=3, max_length=254)
    api_token: str = Field(..., min_length=6, max_length=512)

    @field_validator("subdomain")
    @classmethod
    def normalize_subdomain(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned.startswith("https://"):
            cleaned = cleaned.removeprefix("https://")
        if cleaned.startswith("http://"):
            cleaned = cleaned.removeprefix("http://")
        if cleaned.endswith(".zendesk.com"):
            cleaned = cleaned.removesuffix(".zendesk.com")
        cleaned = cleaned.strip("/")
        if "." in cleaned:
            raise ValueError("Provide Zendesk subdomain only (for example: acme), not a full domain.")
        return cleaned

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        return value.strip()


class ZendeskCredentialValidationResponse(BaseModel):
    ok: bool
    detail: str
    subdomain: str
    base_url: str
    account_name: str | None = None
    authenticated_user: str | None = None
    authenticated_user_role: str | None = None
    http_status: int | None = None


class ZendeskContextRequest(BaseModel):
    subdomain: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=3, max_length=254)
    api_token: str = Field(..., min_length=6, max_length=512)

    @field_validator("subdomain")
    @classmethod
    def normalize_subdomain(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned.startswith("https://"):
            cleaned = cleaned.removeprefix("https://")
        if cleaned.startswith("http://"):
            cleaned = cleaned.removeprefix("http://")
        if cleaned.endswith(".zendesk.com"):
            cleaned = cleaned.removesuffix(".zendesk.com")
        cleaned = cleaned.strip("/")
        if "." in cleaned:
            raise ValueError("Provide Zendesk subdomain only (for example: acme), not a full domain.")
        return cleaned

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        return value.strip()


class ZendeskContextResponse(BaseModel):
    ok: bool
    detail: str
    base_url: str
    catalogs: dict[str, list[ContextReference]] = Field(default_factory=dict)
    fetched_at: str
    warnings: list[str] = Field(default_factory=list)
    sync_id: str = ""
    complete: bool = True
    catalog_counts: dict[str, int] = Field(default_factory=dict)
    page_counts: dict[str, int] = Field(default_factory=dict)


class ZendeskHelpCenterReadinessRequest(ZendeskCredentialValidationRequest):
    help_center_url: str | None = Field(default=None, max_length=2048)
    brand_id: str | None = Field(default=None, max_length=100)
    locale: str | None = Field(default=None, max_length=35)

    @field_validator("help_center_url", "brand_id", "locale")
    @classmethod
    def trim_optional_values(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None


class ZendeskHelpCenterReadinessResponse(BaseModel):
    ready: bool
    state: Literal[
        "ready",
        "manual_enablement_required",
        "brand_selection_required",
        "permission_denied",
        "invalid_credentials",
        "invalid_url",
        "unavailable",
    ]
    detail: str
    base_url: str
    help_center_api_base_url: str
    help_center_url: str
    locale: str
    brand: dict[str, Any] | None = None
    available_brands: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    documentation_url: str = "https://support.zendesk.com/hc/en-us/articles/5702269234330"
    can_create_structure: bool = False
    can_create_articles: bool = False


class ZendeskDeployRequest(BaseModel):
    batch_id: str
    subdomain: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=3, max_length=254)
    api_token: str = Field(..., min_length=6, max_length=512)
    dry_run: bool = False
    on_existing: OnExistingMode = "create_new"
    deployment_scope: ZendeskDeploymentScope = "support"
    help_center_url: str | None = Field(default=None, max_length=2048)
    brand_id: str | None = Field(default=None, max_length=100)
    locale: str | None = Field(default=None, max_length=35)
    article_mode: ZendeskArticleMode = "draft"
    confirm_help_center_deploy: bool = False
    confirm_article_publish: bool = False

    @field_validator("subdomain")
    @classmethod
    def normalize_subdomain(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned.startswith("https://"):
            cleaned = cleaned.removeprefix("https://")
        if cleaned.startswith("http://"):
            cleaned = cleaned.removeprefix("http://")
        if cleaned.endswith(".zendesk.com"):
            cleaned = cleaned.removesuffix(".zendesk.com")
        cleaned = cleaned.strip("/")
        if "." in cleaned:
            raise ValueError("Provide Zendesk subdomain only (for example: acme), not a full domain.")
        return cleaned

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        return value.strip()

    @field_validator("help_center_url", "brand_id", "locale")
    @classmethod
    def trim_optional_deploy_values(cls, value: str | None) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None


class ZendeskDeployRecordResult(BaseModel):
    record_id: str
    object_type: str
    title: str
    deployment_status: Literal["deployed", "failed", "skipped", "pending"]
    zendesk_object_id: str | None = None
    execution_message: str = ""
    executed_at: str


class ZendeskDeploySummary(BaseModel):
    attempted: int = 0
    deployed: int = 0
    failed: int = 0
    skipped: int = 0


class ZendeskDeployResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    summary: ZendeskDeploySummary
    message: str
    results: list[ZendeskDeployRecordResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
