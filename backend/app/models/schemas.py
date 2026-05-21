from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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
    prompt: str = Field(..., min_length=5, max_length=4000)

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
    ]
    id: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=1200)

    @field_validator("id", "name")
    @classmethod
    def trim_string_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("description")
    @classmethod
    def trim_optional_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ImportAssistantGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=4000)
    target_environment: Literal["sandbox"] = "sandbox"
    mode: str = "generate_validate_preview"
    requester: str = "local-user"
    dependency_mode: DependencyMode = "match_existing_or_create_new"
    related_objects: list[ContextReference] = Field(default_factory=list)
    reference_catalog: dict[str, list[ContextReference]] = Field(default_factory=dict)
    recent_batch_context: list[str] = Field(default_factory=list)
    focus_object_types: list[FocusObjectType] = Field(default_factory=list)
    context_notes: str | None = None

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


class AppScriptActionRequest(BaseModel):
    action: Literal[
        "setup_once",
        "sync_schema",
        "generate_api_key",
        "write_batch_to_sheets",
        "validate_batch",
        "get_batch_preview",
        "stage_validate_preview",
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


class ZendeskDeployRequest(BaseModel):
    batch_id: str
    subdomain: str = Field(..., min_length=2, max_length=200)
    email: str = Field(..., min_length=3, max_length=254)
    api_token: str = Field(..., min_length=6, max_length=512)
    dry_run: bool = False
    on_existing: OnExistingMode = "create_new"

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
