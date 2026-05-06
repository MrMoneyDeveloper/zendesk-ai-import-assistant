# AI Zendesk Import Assistant â€” Repository Context

## 1. Purpose of This Document

This file gives the AI agent and future developers the full project context for the **AI Zendesk Import Assistant** repository.

The goal is to build a proof-of-concept tool that allows a user to describe a business process in natural language and receive a structured, reviewable Zendesk configuration package. The system should generate Zendesk-ready objects such as ticket fields, forms, macros, triggers, views, and supporting tag dictionaries, stage them in Google Sheets, validate them, preview them to the user, and only deploy selected/approved records into a Zendesk sandbox.

The system must behave like a **controlled AI configuration copilot**, not an uncontrolled autonomous Zendesk admin.

Core objective:

```text
Natural business request
â†’ AI planning
â†’ schema-controlled generation
â†’ Google Sheets staging
â†’ validation
â†’ preview and record-level approval
â†’ Zendesk sandbox deployment
â†’ execution logging
```

---

## 2. What We Are Trying to Achieve

Zendesk implementation work is often repetitive and template-driven. A consultant usually has to:

1. Understand the business requirement.
2. Decide what Zendesk objects are needed.
3. Draft ticket fields, forms, views, macros, triggers, tags, and routing logic.
4. Put those objects into spreadsheets or import-ready formats.
5. Validate the data manually.
6. Import or recreate the configuration inside Zendesk.
7. Test and correct errors.

This project reduces that manual translation layer.

The user should be able to say something like:

```text
I need a Zendesk setup for an insurance business. They handle quotes, policy changes, claims, broker assignment, follow-ups, escalations, and complaints. Design the fields, forms, macros, triggers, views, tags, and anything else needed for the setup.
```

The assistant should then:

1. Understand the business scenario.
2. Identify the Zendesk objects required.
3. Select only the supported internal templates/schemas.
4. Generate structured JSON.
5. Stage generated records into Google Sheets.
6. Validate every staged record.
7. Show the user a preview with warnings and blockers.
8. Allow the user to select exactly what should be imported.
9. Push only approved records into Zendesk sandbox.
10. Log the final result.

The AI should **not** directly write into Zendesk without staging, validation, preview, and approval.

---

## 3. High-Level Architecture

```text
React Front End
  â†“
FastAPI Gateway hosted on Cloudflare
  â†“
Grok Planner
  â†“
FastAPI Schema Registry / Template Selector
  â†“
Grok Generator
  â†“
FastAPI Generated JSON Validation
  â†“
Google Apps Script
  â†“
Google Sheets Staging + Validation Logs
  â†“
React Preview + Record-Level Approval
  â†“
FastAPI Zendesk Deployment Handler
  â†“
Zendesk Sandbox API
  â†“
Google Apps Script Execution Log
  â†“
React Final Result
```

---

## 4. Technology Stack

### Front End

| Component | Technology |
|---|---|
| Application UI | React |
| Build tool | Vite or equivalent |
| Styling | Tailwind CSS or plain CSS |
| Hosting | Cloudflare Pages or equivalent static hosting |
| Responsibility | Natural prompt capture, preview display, record selection, approval, deployment result display |

### Gateway / Backend

| Component | Technology |
|---|---|
| API gateway | FastAPI |
| Hosting target | Cloudflare-hosted API layer or equivalent serverless/container host |
| Responsibility | Request validation, AI orchestration, schema selection, generated JSON validation, secure API routing, Zendesk deployment |

> Note: If Cloudflare cannot host the exact FastAPI runtime cleanly in the selected deployment model, the gateway can be implemented as a Cloudflare Worker equivalent. The responsibility remains the same: secure orchestration between React, Grok, Apps Script, and Zendesk.

### AI Layer

| Component | Technology |
|---|---|
| AI model | xAI API / Grok |
| First AI pass | Planner |
| Second AI pass | Generator |
| Output style | Strict structured JSON |
| Responsibility | Convert natural business context into a plan, then generate schema-controlled Zendesk configuration data |

### Staging and Validation Layer

| Component | Technology |
|---|---|
| Staging | Google Sheets |
| Automation | Google Apps Script |
| Responsibility | Write generated data into tabs, validate staged rows, store warnings/blockers, store approval states, store execution results |

### Execution Layer

| Component | Technology |
|---|---|
| Zendesk execution | Zendesk REST API |
| Environment | Sandbox-first |
| Responsibility | Create or update only approved Zendesk records |

---

## 5. Non-Negotiable Design Principles

1. **The AI does not deploy directly to Zendesk.**
2. **The front end must never hold Zendesk, Grok, or privileged Apps Script secrets.**
3. **FastAPI is the secure orchestration layer.**
4. **Grok must return structured JSON, not free-form text.**
5. **Google Sheets is the staging and review layer.**
6. **Apps Script performs row-level validation against staged data.**
7. **The user must preview generated records before deployment.**
8. **The user must be able to approve or exclude individual records.**
9. **Only approved records may be deployed.**
10. **Initial deployment must target Zendesk sandbox only.**
11. **Execution results must be logged.**
12. **Unsupported object types must be marked as recommendations or blocked, not silently generated or deployed.**

---

## 6. Full End-to-End Flow

The current agreed flow is:

```text
1. React front end
   â†“
2. FastAPI gateway hosted on Cloudflare
   â†“
3. FastAPI validates the incoming user request
   â†“
4. FastAPI sends user request to Grok Planner with a planning JSON schema
   â†“
5. Grok Planner returns what the user needs
   â†“
6. FastAPI validates the planner response
   â†“
7. FastAPI selects the correct Zendesk object templates/schemas
   â†“
8. FastAPI sends second request to Grok Generator with selected schemas
   â†“
9. Grok Generator returns structured Zendesk configuration JSON
   â†“
10. FastAPI validates generated JSON structure
   â†“
11. FastAPI sends generated data to Google Apps Script
   â†“
12. Apps Script writes generated data to Google Sheets staging tabs
   â†“
13. Apps Script validates staged rows
   â†“
14. Apps Script returns validated records, warnings, blockers, and sample preview data
   â†“
15. React shows preview of generated objects, sample rows, warnings, and blockers
   â†“
16. User selects what should be imported and what should be excluded
   â†“
17. React sends selected/approved records back to FastAPI
   â†“
18. FastAPI marks selected records as approved for deployment
   â†“
19. FastAPI pushes only approved records to Zendesk sandbox
   â†“
20. Apps Script logs execution result
   â†“
21. React shows final import result, including created, skipped, failed, and blocked items
```

---

## 7. Core Concept: Two-Pass AI Flow

The user experience should feel natural. The user does not need to choose â€œCreate macroâ€ or â€œCreate triggerâ€ manually. Instead, the AI must first decide what configuration objects are needed.

This requires two AI passes.

---

### 7.1 Pass 1 â€” Grok Planner

The planner answers:

```text
What does the user need?
```

Input:

```text
Original natural-language business request
+ planning response schema
+ supported/unsupported category rules
```

Example user request:

```text
Design a Zendesk setup for an insurance company handling claims, quotes, broker assignment, policy changes, escalations, and follow-ups.
```

Example planner output:

```json
{
  "business_type": "insurance",
  "detected_processes": [
    "quote requests",
    "claims support",
    "policy changes",
    "broker assignment",
    "customer follow-ups",
    "complaint escalation"
  ],
  "recommended_objects": [
    "ticket_fields",
    "ticket_forms",
    "macros",
    "triggers",
    "views",
    "tag_dictionary"
  ],
  "unsupported_or_later": [
    "sla_policies",
    "custom_objects",
    "webhooks"
  ],
  "planning_summary": "The business needs structured intake, routing, agent replies, operational queues, and reporting tags."
}
```

The planner must not generate final Zendesk payloads. It only identifies what should be built.

---

### 7.2 Template Selection After Planner

FastAPI receives the planner output and checks it against the internal schema registry.

Example:

```text
Planner recommended:
- ticket_fields
- ticket_forms
- macros
- triggers
- views
- tag_dictionary
- sla_policies

FastAPI registry check:
- ticket_fields: supported
- ticket_forms: supported
- macros: supported
- triggers: supported
- views: supported
- tag_dictionary: supported
- sla_policies: not supported for MVP

Result:
- Include supported schemas in second Grok call.
- Move unsupported items to recommendations only.
```

The LLM recommends.  
The FastAPI schema registry decides what is allowed.

---

### 7.3 Pass 2 â€” Grok Generator

The generator answers:

```text
Generate the actual Zendesk configuration data.
```

Input:

```text
Original user prompt
+ planner output
+ selected supported schemas/templates
+ strict output JSON contract
```

Example generator instruction:

```text
Using the approved implementation plan and only the selected schemas, generate structured Zendesk configuration JSON.

Return JSON only.
Do not include prose.
Do not generate unsupported object types.
Do not invent fields outside the schema.
Do not deploy anything.
```

Example generator output shape:

```json
{
  "batch_id": "BATCH-001",
  "objects": {
    "ticket_fields": [],
    "ticket_forms": [],
    "macros": [],
    "triggers": [],
    "views": [],
    "tag_dictionary": [],
    "recommendations": []
  }
}
```

---

## 8. Responsibility Breakdown

### 8.1 React Front End

React is responsible for:

- Capturing the userâ€™s natural business request.
- Displaying generation status.
- Displaying the AI planning summary.
- Displaying generated object previews.
- Showing validation warnings and blockers.
- Allowing the user to approve or exclude individual records.
- Sending the selected approved records back to FastAPI.
- Showing final deployment results.

React must not:

- Store Zendesk credentials.
- Store xAI API credentials.
- Call Zendesk directly.
- Call Grok directly.
- Make production deployment decisions without backend enforcement.
- Bypass validation or approval gates.

---

### 8.2 FastAPI Gateway

FastAPI is the secure orchestration layer.

FastAPI is responsible for:

- Receiving requests from React.
- Validating the incoming request.
- Checking origin/session/auth where applicable.
- Creating or tracking a batch ID.
- Calling Grok Planner.
- Validating planner output.
- Selecting schemas from the schema registry.
- Calling Grok Generator.
- Validating generated JSON structure.
- Sending generated records to Apps Script.
- Receiving validation summaries from Apps Script.
- Receiving user-approved records from React.
- Marking records as approved for deployment.
- Pushing only approved records to Zendesk sandbox.
- Returning final status to React.

FastAPI must not:

- Trust the userâ€™s front-end input blindly.
- Trust Grok output blindly.
- Deploy unsupported object types.
- Deploy blocked records.
- Deploy skipped records.
- Deploy to production in the MVP.
- Store secrets in front-end-accessible files.

---

### 8.3 xAI API / Grok

Grok is responsible for:

- Understanding natural-language business intent.
- Planning the required Zendesk configuration objects.
- Generating structured JSON based on the selected schemas.

Grok must not:

- Deploy anything.
- Generate unsupported object types as deployable items.
- Return free-form narrative for the generator step.
- Make final approval decisions.
- Override the schema registry.

---

### 8.4 Google Apps Script

Apps Script is the Google Sheets worker.

Apps Script is responsible for:

- Receiving generated JSON from FastAPI.
- Writing records into the correct Google Sheets tabs.
- Writing the original prompt and batch metadata.
- Running row-level validation.
- Writing validation results.
- Returning preview data, warnings, and blockers to FastAPI.
- Logging Zendesk execution results after deployment.

Apps Script should be organized into separate functions:

```text
writeBatchToSheets()
validateBatch()
getBatchPreview()
updateApprovalStatus()
writeExecutionLog()
getExecutionSummary()
```

Apps Script must not:

- Be the primary AI brain.
- Trust generated data without validation.
- Expose privileged sheet actions publicly without protection.
- Deploy blocked records.
- Treat validation warnings as automatic approval.

---

### 8.5 Google Sheets

Google Sheets is the staging, review, and audit layer.

Recommended tabs:

| Tab | Purpose |
|---|---|
| Requests | Stores prompt, requester, batch ID, status, environment, timestamp |
| Planning Output | Stores detected business processes and recommended objects |
| Ticket Fields | Stages generated ticket field records |
| Ticket Forms | Stages generated ticket form records |
| Macros | Stages generated macros |
| Triggers | Stages generated trigger rules |
| Views | Stages generated view definitions |
| Tag Dictionary | Stages recommended tags and naming standards |
| Recommendations | Stores unsupported/later-phase suggestions |
| Validation Log | Stores row-level validation errors/warnings |
| Approval Log | Stores user approval/exclusion decisions |
| Execution Log | Stores Zendesk API results |

---

### 8.6 Zendesk API

Zendesk API is the final execution layer.

Zendesk deployment should:

- Target sandbox only for MVP.
- Only process approved records.
- Only process deployable object types.
- Log each API response.
- Continue safely on partial failure where possible.
- Return created IDs where available.
- Mark failed records with error details.

Zendesk deployment should not:

- Push the entire batch blindly.
- Push blocked items.
- Push skipped items.
- Push recommendations.
- Push production changes in MVP.
- Hide partial failures.

---

## 9. Schema Registry

FastAPI needs a schema registry that maps object types to:

- JSON schema file
- Sheet tab
- Apps Script validator
- Deployment handler
- Deployability flag
- Risk level

Example:

```json
{
  "ticket_fields": {
    "schema": "schemas/ticket_fields.schema.json",
    "sheet_tab": "Ticket Fields",
    "validator": "validateTicketFields",
    "deploy_handler": "deployTicketFields",
    "deployable": true,
    "risk_level": "medium"
  },
  "ticket_forms": {
    "schema": "schemas/ticket_forms.schema.json",
    "sheet_tab": "Ticket Forms",
    "validator": "validateTicketForms",
    "deploy_handler": "deployTicketForms",
    "deployable": true,
    "risk_level": "medium"
  },
  "macros": {
    "schema": "schemas/macros.schema.json",
    "sheet_tab": "Macros",
    "validator": "validateMacros",
    "deploy_handler": "deployMacros",
    "deployable": true,
    "risk_level": "low"
  },
  "triggers": {
    "schema": "schemas/triggers.schema.json",
    "sheet_tab": "Triggers",
    "validator": "validateTriggers",
    "deploy_handler": "deployTriggers",
    "deployable": true,
    "risk_level": "high"
  },
  "views": {
    "schema": "schemas/views.schema.json",
    "sheet_tab": "Views",
    "validator": "validateViews",
    "deploy_handler": "deployViews",
    "deployable": true,
    "risk_level": "low"
  },
  "tag_dictionary": {
    "schema": "schemas/tag_dictionary.schema.json",
    "sheet_tab": "Tag Dictionary",
    "validator": "validateTagDictionary",
    "deploy_handler": null,
    "deployable": false,
    "risk_level": "low"
  },
  "sla_policies": {
    "schema": null,
    "sheet_tab": "Recommendations",
    "validator": null,
    "deploy_handler": null,
    "deployable": false,
    "risk_level": "high"
  }
}
```

The schema registry is a guardrail.

The planner may recommend an object.  
FastAPI may only generate and deploy it if the registry supports it.

---

## 10. Validation Layers

There are multiple validation points. Each has a different purpose.

---

### 10.1 Incoming Request Validation â€” FastAPI

Before AI calls, FastAPI validates:

- Request came from an allowed front-end origin.
- Prompt is present.
- Prompt is not too short or empty.
- Target environment is sandbox.
- User is allowed to start generation.
- Request is not attempting direct deployment.
- Payload follows the expected API contract.

Example failure:

```json
{
  "status": "rejected",
  "code": "EMPTY_PROMPT",
  "message": "A business request is required before generation can start."
}
```

---

### 10.2 Planner Response Validation â€” FastAPI

After Grok Planner returns, FastAPI validates:

- Response is valid JSON.
- Required planner fields are present.
- `recommended_objects` is an array.
- Object names match known registry keys or are moved to unsupported/later.
- Planner did not return final deployable payloads by mistake.

Example failure:

```json
{
  "status": "failed",
  "code": "INVALID_PLANNER_RESPONSE",
  "message": "Planner response did not include recommended_objects."
}
```

---

### 10.3 Generated JSON Structure Validation â€” FastAPI

After Grok Generator returns, FastAPI validates:

- JSON is valid.
- `batch_id` exists.
- `objects` exists.
- Each object array matches the selected schemas.
- Unsupported deployable objects are not present.
- No production execution instruction is included.
- High-level required fields exist before sending to Apps Script.

This is not the final row-level validation. It is a structural check.

---

### 10.4 Staged Row Validation â€” Apps Script

Apps Script validates the actual staged rows in Sheets.

Validation examples:

#### Ticket Fields

- Title is required.
- Type is required.
- Dropdown options must include name and tag.
- Duplicate option tags are blocked.
- Field type must be supported.
- Field key/tag must follow naming rules.

#### Ticket Forms

- Form name is required.
- Form must include valid ticket fields.
- Duplicate form names are flagged.
- Field ordering must be valid.

#### Macros

- Macro title is required.
- Macro comment/body is required.
- Macro actions must be valid.
- Tags must follow naming rules.

#### Triggers

- Trigger title is required.
- At least one condition is required.
- At least one action is required.
- Conditions must use supported fields/operators.
- Actions must use supported Zendesk actions.
- Missing group IDs are blocked.
- Ambiguous routing logic is blocked.

#### Views

- View title is required.
- Conditions are required.
- Output columns must be supported.
- Sorting rules must be valid.
- Duplicate view titles are warnings or blockers depending on policy.

#### Tag Dictionary

- Tag is required.
- Tag format must be valid.
- Duplicate tags are warnings/blockers.
- Tag purpose should be populated.

---

## 11. Preview and Record-Level Approval Gate

This is a mandatory step.

After staging and validation, the user must see a preview.

The preview should include:

- Planning summary.
- Generated object counts.
- Sample generated records.
- Validation results.
- Warnings.
- Blockers.
- Records eligible for import.
- Records excluded from import.
- Unsupported recommendations.

The user must be able to select:

```text
Import this
Do not import this
Edit this
Regenerate this category
Regenerate all
```

Approval should be record-level, not just batch-level.

---

### 11.1 Record Statuses

Each generated record should have an `import_decision` and `deployment_status`.

Recommended values:

```text
pending_review
approved
skipped
blocked
deploying
deployed
failed
not_deployable
```

Example staged row control fields:

| Field | Purpose |
|---|---|
| batch_id | Groups generated records |
| record_id | Unique generated record ID |
| object_type | ticket_field, macro, trigger, view, etc. |
| title | Human-readable object name |
| validation_status | passed, warning, failed |
| warning_message | Non-blocking issue |
| blocked_reason | Blocking issue |
| preview_summary | Human-readable summary |
| import_decision | pending_review, approved, skipped |
| approved_by | User who approved |
| approved_at | Approval timestamp |
| deployment_status | pending, deployed, failed, skipped |
| zendesk_object_id | Returned Zendesk ID |
| execution_message | API result or error |

---

## 12. Deployment Rules

Deployment must follow strict rules:

```text
Generate automatically: yes
Stage automatically: yes
Validate automatically: yes
Preview automatically: yes
Deploy automatically: no
```

Deployment requires explicit user approval.

Only records where all of the following are true may be deployed:

```text
validation_status = passed
import_decision = approved
deployable = true
environment = sandbox
blocked_reason is empty
```

Records must be skipped if:

```text
import_decision = skipped
validation_status = failed
deployable = false
object_type is unsupported
environment is production
```

---

## 13. Status Model

Use consistent batch statuses:

| Status | Meaning |
|---|---|
| received | React request received by FastAPI |
| request_validated | Incoming request passed initial validation |
| planning | Grok Planner is running |
| planned | Planner returned a valid implementation plan |
| plan_failed | Planner failed or returned invalid output |
| schemas_selected | FastAPI selected supported schemas |
| generating | Grok Generator is running |
| generated | Structured JSON was generated |
| generation_failed | Generator failed or returned invalid JSON |
| staging | Data is being sent to Apps Script |
| staged | Data was written to Sheets |
| validating | Apps Script validation is running |
| validated_passed | All deployable records passed validation |
| validated_warning | Records passed with warnings |
| validated_failed | Blocking validation errors exist |
| preview_ready | React can display preview and selection |
| partially_approved | Some records were approved |
| approved | Records approved for deployment |
| deploying | Zendesk API deployment is running |
| deployed | All approved records deployed successfully |
| partial_success | Some records deployed, some failed |
| failed | Batch failed |
| cancelled | User cancelled or skipped deployment |

---

## 14. API Contract Guidance

FastAPI should expose clear endpoints.

Recommended routes:

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Confirms gateway is online |
| POST | `/api/import-assistant/generate` | Starts planning, generation, staging, and validation |
| GET | `/api/import-assistant/jobs/{batch_id}` | Returns batch/job state |
| GET | `/api/import-assistant/preview/{batch_id}` | Returns preview data |
| POST | `/api/import-assistant/approve` | Receives selected records for import |
| POST | `/api/import-assistant/deploy` | Deploys approved records to Zendesk sandbox |
| GET | `/api/import-assistant/templates` | Returns supported object types |
| POST | `/api/import-assistant/regenerate` | Regenerates selected category or batch |
| POST | `/api/import-assistant/cancel/{batch_id}` | Cancels or skips a batch |

---

### 14.1 Generate Request Example

```json
{
  "prompt": "Design a Zendesk setup for an insurance business handling claims, quotes, policy changes, broker assignment, follow-ups, and escalations.",
  "target_environment": "sandbox",
  "mode": "generate_validate_preview",
  "requester": "demo-user"
}
```

---

### 14.2 Generate Response Example

```json
{
  "batch_id": "BATCH-2026-05-05-001",
  "status": "preview_ready",
  "planning_summary": {
    "business_type": "insurance",
    "recommended_objects": [
      "ticket_fields",
      "ticket_forms",
      "macros",
      "triggers",
      "views",
      "tag_dictionary"
    ],
    "unsupported_or_later": [
      "sla_policies",
      "custom_objects"
    ]
  },
  "generated_counts": {
    "ticket_fields": 8,
    "ticket_forms": 2,
    "macros": 6,
    "triggers": 5,
    "views": 4,
    "tag_dictionary": 20
  },
  "validation_summary": {
    "passed": 20,
    "warnings": 3,
    "blocked": 2
  },
  "preview_url": "/api/import-assistant/preview/BATCH-2026-05-05-001"
}
```

---

### 14.3 Preview Response Example

```json
{
  "batch_id": "BATCH-2026-05-05-001",
  "status": "preview_ready",
  "records": [
    {
      "record_id": "REC-001",
      "object_type": "ticket_field",
      "title": "Policy Type",
      "preview_summary": "Dropdown field for capturing insurance policy type.",
      "validation_status": "passed",
      "warnings": [],
      "blocked_reason": null,
      "import_decision": "pending_review",
      "deployable": true
    },
    {
      "record_id": "REC-002",
      "object_type": "trigger",
      "title": "Route Claims Tickets",
      "preview_summary": "Routes claims tickets to the Claims Support group.",
      "validation_status": "failed",
      "warnings": [],
      "blocked_reason": "Missing Zendesk group ID mapping for Claims Support.",
      "import_decision": "blocked",
      "deployable": false
    }
  ]
}
```

---

### 14.4 Approval Request Example

```json
{
  "batch_id": "BATCH-2026-05-05-001",
  "approved_by": "demo-user",
  "records": [
    {
      "record_id": "REC-001",
      "import_decision": "approved"
    },
    {
      "record_id": "REC-002",
      "import_decision": "skipped"
    }
  ]
}
```

---

### 14.5 Deploy Request Example

```json
{
  "batch_id": "BATCH-2026-05-05-001",
  "target_environment": "sandbox",
  "approved_by": "demo-user"
}
```

---

### 14.6 Deploy Response Example

```json
{
  "batch_id": "BATCH-2026-05-05-001",
  "status": "partial_success",
  "summary": {
    "created": 12,
    "skipped": 3,
    "failed": 1,
    "blocked": 2
  },
  "results": [
    {
      "record_id": "REC-001",
      "object_type": "ticket_field",
      "title": "Policy Type",
      "deployment_status": "deployed",
      "zendesk_object_id": "123456789"
    },
    {
      "record_id": "REC-010",
      "object_type": "macro",
      "title": "Claims::Missing Documents",
      "deployment_status": "failed",
      "execution_message": "Zendesk API rejected macro action payload."
    }
  ]
}
```

---

## 15. Suggested Repository Structure

```text
ai-zendesk-import-assistant/
â”‚
â”œâ”€â”€ README.md
â”œâ”€â”€ CONTEXT.md
â”œâ”€â”€ .env
â”‚
â”œâ”€â”€ frontend/
â”‚   â”œâ”€â”€ package.json
â”‚   â”œâ”€â”€ vite.config.ts
â”‚   â””â”€â”€ src/
â”‚       â”œâ”€â”€ App.tsx
â”‚       â”œâ”€â”€ main.tsx
â”‚       â”œâ”€â”€ components/
â”‚       â”‚   â”œâ”€â”€ PromptInput.tsx
â”‚       â”‚   â”œâ”€â”€ PlanningSummary.tsx
â”‚       â”‚   â”œâ”€â”€ PreviewTable.tsx
â”‚       â”‚   â”œâ”€â”€ ValidationPanel.tsx
â”‚       â”‚   â”œâ”€â”€ ApprovalControls.tsx
â”‚       â”‚   â””â”€â”€ DeploymentResult.tsx
â”‚       â”œâ”€â”€ services/
â”‚       â”‚   â””â”€â”€ api.ts
â”‚       â””â”€â”€ types/
â”‚           â””â”€â”€ importAssistant.ts
â”‚
â”œâ”€â”€ api/
â”‚   â”œâ”€â”€ main.py
â”‚   â”œâ”€â”€ routes/
â”‚   â”‚   â”œâ”€â”€ generate.py
â”‚   â”‚   â”œâ”€â”€ preview.py
â”‚   â”‚   â”œâ”€â”€ approve.py
â”‚   â”‚   â”œâ”€â”€ deploy.py
â”‚   â”‚   â””â”€â”€ templates.py
â”‚   â”œâ”€â”€ services/
â”‚   â”‚   â”œâ”€â”€ Grok_planner_service.py
â”‚   â”‚   â”œâ”€â”€ Grok_generator_service.py
â”‚   â”‚   â”œâ”€â”€ schema_registry_service.py
â”‚   â”‚   â”œâ”€â”€ apps_script_service.py
â”‚   â”‚   â”œâ”€â”€ zendesk_service.py
â”‚   â”‚   â””â”€â”€ validation_service.py
â”‚   â”œâ”€â”€ schemas/
â”‚   â”‚   â”œâ”€â”€ planner_response.schema.json
â”‚   â”‚   â”œâ”€â”€ ticket_fields.schema.json
â”‚   â”‚   â”œâ”€â”€ ticket_forms.schema.json
â”‚   â”‚   â”œâ”€â”€ macros.schema.json
â”‚   â”‚   â”œâ”€â”€ triggers.schema.json
â”‚   â”‚   â”œâ”€â”€ views.schema.json
â”‚   â”‚   â””â”€â”€ tag_dictionary.schema.json
â”‚   â”œâ”€â”€ models/
â”‚   â”‚   â”œâ”€â”€ request_models.py
â”‚   â”‚   â”œâ”€â”€ response_models.py
â”‚   â”‚   â””â”€â”€ status_models.py
â”‚   â””â”€â”€ config/
â”‚       â””â”€â”€ schema_registry.json
â”‚
â”œâ”€â”€ apps-script/
â”‚   â”œâ”€â”€ Code.gs
â”‚   â”œâ”€â”€ SheetWriter.gs
â”‚   â”œâ”€â”€ Validators.gs
â”‚   â”œâ”€â”€ Preview.gs
â”‚   â”œâ”€â”€ Approval.gs
â”‚   â””â”€â”€ ExecutionLog.gs
â”‚
â””â”€â”€ docs/
    â”œâ”€â”€ architecture.md
    â”œâ”€â”€ api-contracts.md
    â”œâ”€â”€ schema-registry.md
    â”œâ”€â”€ validation-rules.md
    â””â”€â”€ deployment-notes.md
```

---

## 16. Front-End Experience

The React UI should follow this user journey:

### Step 1: Prompt Entry

User sees a text box:

```text
Describe the Zendesk setup you want to generate.
```

User enters natural language.

Button:

```text
Generate setup
```

---

### Step 2: Planning Status

Display:

```text
Analysing business requirement...
Identifying required Zendesk objects...
```

---

### Step 3: Generation Status

Display:

```text
Generating structured Zendesk configuration...
```

---

### Step 4: Validation Status

Display:

```text
Validating staged records...
```

---

### Step 5: Preview

Display sections:

```text
Planning Summary
Generated Objects
Validation Warnings
Blocked Items
Record Selection
```

Example:

```text
Generated:
- 8 ticket fields
- 2 forms
- 6 macros
- 5 triggers
- 4 views
- 20 tags

Validation:
- 20 passed
- 3 warnings
- 2 blocked
```

Each record should have:

```text
[Approve] [Skip] [Edit Later] [View Details]
```

---

### Step 6: Approval

User confirms:

```text
Deploy selected records to Zendesk sandbox
```

The UI should clearly state:

```text
Only approved records will be imported.
Skipped, blocked, and unsupported records will not be deployed.
```

---

### Step 7: Final Result

Display:

```text
Deployment complete

Created:
- 10 records

Skipped:
- 3 records

Failed:
- 1 record

Blocked:
- 2 records
```

Each failed record should show the reason.

---

## 17. Security and Governance

### Secrets

Secrets must be stored server-side only.

Do not expose:

- Zendesk API token
- xAI API credentials
- Apps Script secret token
- Service account keys
- Admin email/token pairs

Recommended environment variables:

```env
FRONTEND_ORIGIN=https://your-frontend-domain.com

XAI_API_KEY=
XAI_BASE_URL=https://api.x.ai/v1
XAI_MODEL=grok-4.3

GOOGLE_APPS_SCRIPT_URL=
GOOGLE_APPS_SCRIPT_SHARED_SECRET=

ZENDESK_SUBDOMAIN=
ZENDESK_EMAIL=
ZENDESK_API_TOKEN=
ZENDESK_TARGET_ENVIRONMENT=sandbox
```

### Environment Control

For MVP:

```text
Production deployment must be disabled.
Sandbox deployment only.
```

### Approval Control

Do not allow:

```text
Generated â†’ deployed
```

Always require:

```text
Generated â†’ staged â†’ validated â†’ previewed â†’ approved â†’ deployed
```

---

## 18. Risk Levels

Different Zendesk objects have different risk levels.

| Object Type | Risk | Notes |
|---|---|---|
| Tag Dictionary | Low | Mostly planning/reference data |
| Views | Low | Visible queues; easy to review |
| Macros | Low to Medium | Agent-facing replies/actions |
| Ticket Fields | Medium | Affects forms and reporting |
| Ticket Forms | Medium | Affects ticket intake |
| Triggers | High | Can route, tag, notify, assign, and affect workflows |
| SLA Policies | High | Should be later phase |
| Webhooks | High | Should be later phase |
| Custom Objects | Medium to High | Requires more schema governance |

For MVP, prioritize:

```text
1. Views
2. Macros
3. Ticket Fields
4. Ticket Forms
5. Triggers
```

Be cautious with triggers. They should receive stricter validation and preferably require explicit record-level approval.

---

## 19. Phased Delivery Plan

### Phase 1 â€” MVP: Planning, Generation, Staging, Validation, Preview

Build:

- React prompt input.
- FastAPI generate endpoint.
- Grok Planner.
- Schema registry.
- Grok Generator.
- Apps Script sheet staging.
- Apps Script validation.
- React preview and selection UI.
- No automatic Zendesk deployment.

Outcome:

```text
The demo shows natural prompt â†’ plan â†’ generated records â†’ Sheets staging â†’ validation â†’ preview.
```

---

### Phase 2 â€” Controlled Sandbox Deployment

Add:

- Record-level approval.
- Zendesk sandbox API deployment.
- Execution logging.
- Partial success handling.
- Final result UI.

Outcome:

```text
The demo shows approved records being created in Zendesk sandbox.
```

---

### Phase 3 â€” Operational Refinement

Add:

- Regeneration of selected categories.
- AI-assisted repair of validation errors.
- Better duplicate detection against existing Zendesk objects.
- More object types.
- More detailed dependency mapping.
- Role-based approval.
- Production readiness review.

Outcome:

```text
The assistant becomes a safer configuration copilot rather than a one-off generator.
```

---

## 20. Agent Instructions for This Repository

When an AI agent works on this repository, it must follow these rules:

1. Preserve the staged workflow.
2. Do not bypass validation.
3. Do not implement direct React-to-Zendesk calls.
4. Do not place secrets in the front-end.
5. Do not auto-deploy generated records.
6. Use schema-controlled JSON for AI output.
7. Keep planner and generator responsibilities separate.
8. Add clear status handling for every stage.
9. Keep Apps Script focused on Sheets, validation, and logging.
10. Keep FastAPI focused on orchestration, security, schema selection, and Zendesk execution.
11. Treat unsupported object types as recommendations or blocked items.
12. Log all important actions against a batch ID.
13. Make partial failure visible.
14. Ensure the user can select exactly what gets imported.
15. Preserve sandbox-first deployment.

---

## 21. Example User Story

```text
As an implementation user,
I want to describe a business process in natural language,
so that the assistant can propose the Zendesk configuration objects required,
generate structured setup data,
stage it for review,
validate it,
allow me to choose what to import,
and deploy only approved records to Zendesk sandbox.
```

Acceptance criteria:

```text
Given I enter a natural-language business request,
when I submit it,
then the system creates a planning summary.

Given the planning summary is valid,
when generation runs,
then the system creates structured Zendesk configuration JSON.

Given generated data exists,
when it is staged,
then Google Sheets contains rows in the correct object tabs.

Given staged rows exist,
when validation runs,
then each row has a validation status.

Given validation results exist,
when the preview loads,
then I can approve or skip individual records.

Given I approve selected records,
when I deploy,
then only approved, valid, deployable records are pushed to Zendesk sandbox.

Given deployment completes,
then the system shows created, skipped, failed, and blocked records.
```

---

## 22. Final Implementation Summary

This project is not just:

```text
Prompt â†’ AI â†’ Zendesk
```

The correct design is:

```text
Prompt
â†’ AI Planner
â†’ Schema Registry
â†’ AI Generator
â†’ FastAPI JSON Validation
â†’ Google Sheets Staging
â†’ Apps Script Row Validation
â†’ React Preview
â†’ Record-Level Approval
â†’ Zendesk Sandbox Deployment
â†’ Execution Log
```

The end goal is a safe, explainable, auditable AI-assisted Zendesk configuration workflow where humans remain in control of what enters the instance.
