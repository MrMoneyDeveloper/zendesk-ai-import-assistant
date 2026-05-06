import json
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from app.core.settings import get_settings
from app.loggers.logger import get_logger

logger = get_logger(__name__)

TAB_NAMES = [
    "Requests",
    "Planning Output",
    "Ticket Fields",
    "Ticket Forms",
    "Macros",
    "Triggers",
    "Views",
    "Tag Dictionary",
    "Recommendations",
    "Validation Log",
    "Approval Log",
]

OBJECT_TYPE_TO_TAB = {
    "ticket_fields": "Ticket Fields",
    "ticket_forms": "Ticket Forms",
    "macros": "Macros",
    "triggers": "Triggers",
    "views": "Views",
    "tag_dictionary": "Tag Dictionary",
    "recommendations": "Recommendations",
}


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True)
    except Exception:
        return "{}"


class SheetsService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._service = None

    @property
    def enabled(self) -> bool:
        if not self.settings.google_sheet_id:
            return False
        if not self.settings.google_service_account_file:
            return False
        return self._service_account_path().exists()

    def _service_account_path(self) -> Path:
        path = Path(self.settings.google_service_account_file)
        if path.is_absolute():
            return path
        return (Path(__file__).resolve().parents[2] / path).resolve()

    def _client(self):
        if self._service is not None:
            return self._service

        credentials = Credentials.from_service_account_file(
            str(self._service_account_path()),
            scopes=[self.settings.google_sheets_scope],
        )
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def ensure_tabs(self, tab_names: list[str] | None = None) -> list[str]:
        if not self.enabled:
            return []

        tabs = tab_names or TAB_NAMES
        service = self._client()
        sheet_meta = (
            service.spreadsheets()
            .get(spreadsheetId=self.settings.google_sheet_id)
            .execute()
        )
        existing = {
            item["properties"]["title"]
            for item in sheet_meta.get("sheets", [])
            if "properties" in item
        }

        missing = [tab for tab in tabs if tab not in existing]
        if not missing:
            return []

        requests = [{"addSheet": {"properties": {"title": tab}}} for tab in missing]
        (
            service.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={"requests": requests},
            )
            .execute()
        )
        logger.info("Created missing Google Sheets tabs: %s", ", ".join(missing))
        return missing

    def _ensure_header(self, tab_name: str, headers: list[str]) -> None:
        service = self._client()
        existing = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{tab_name}!1:1",
            )
            .execute()
            .get("values", [])
        )
        if existing:
            return
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{tab_name}!1:1",
                valueInputOption="RAW",
                body={"values": [headers]},
            )
            .execute()
        )

    def append_rows(self, tab_name: str, headers: list[str], row_dicts: list[dict[str, Any]]) -> int:
        if not self.enabled or not row_dicts:
            return 0

        self._ensure_header(tab_name, headers)
        values = [[row.get(header, "") for header in headers] for row in row_dicts]
        service = self._client()
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"{tab_name}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
        return int(result.get("updates", {}).get("updatedRows", 0))

    def stage_batch(
        self,
        batch: dict[str, Any],
        planning: dict[str, Any],
        preview_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "sheet_enabled": False,
                "sheet_id": "",
                "message": "Google Sheets is not configured; staging skipped.",
            }

        self.ensure_tabs()
        batch_id = batch["batch_id"]
        self.append_rows(
            "Requests",
            ["batch_id", "prompt", "requester", "status", "target_environment", "created_at"],
            [
                {
                    "batch_id": batch_id,
                    "prompt": batch.get("prompt", ""),
                    "requester": batch.get("requester", ""),
                    "status": batch.get("status", ""),
                    "target_environment": batch.get("target_environment", "sandbox"),
                    "created_at": batch.get("created_at", ""),
                }
            ],
        )
        self.append_rows(
            "Planning Output",
            ["batch_id", "object_type", "intent", "confidence"],
            [
                {
                    "batch_id": batch_id,
                    "object_type": planning.get("object_type", ""),
                    "intent": planning.get("intent", ""),
                    "confidence": planning.get("confidence", ""),
                }
            ],
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in preview_records:
            object_type = str(row.get("object_type", "recommendations")).lower().strip()
            tab = OBJECT_TYPE_TO_TAB.get(object_type, "Recommendations")
            grouped.setdefault(tab, []).append(
                {
                    "batch_id": batch_id,
                    "record_id": row.get("record_id", ""),
                    "title": row.get("title", ""),
                    "object_type": row.get("object_type", ""),
                    "conditions_json": _safe_json(row.get("conditions", [])),
                    "actions_json": _safe_json(row.get("actions", [])),
                    "validation_status": row.get("validation_status", ""),
                }
            )

        for tab, rows in grouped.items():
            self.append_rows(
                tab,
                [
                    "batch_id",
                    "record_id",
                    "title",
                    "object_type",
                    "conditions_json",
                    "actions_json",
                    "validation_status",
                ],
                rows,
            )

        return {
            "sheet_enabled": True,
            "sheet_id": self.settings.google_sheet_id,
            "message": "Batch staged to Google Sheets.",
        }

    def write_validation_log(self, batch_id: str, records: list[dict[str, Any]]) -> int:
        rows = []
        for row in records:
            rows.append(
                {
                    "batch_id": batch_id,
                    "record_id": row.get("record_id", ""),
                    "title": row.get("title", ""),
                    "validation_status": row.get("validation_status", ""),
                    "warnings": _safe_json(row.get("warnings", [])),
                    "blocked_reason": row.get("blocked_reason", ""),
                }
            )
        return self.append_rows(
            "Validation Log",
            [
                "batch_id",
                "record_id",
                "title",
                "validation_status",
                "warnings",
                "blocked_reason",
            ],
            rows,
        )

    def write_approval_log(self, batch_id: str, decisions: list[dict[str, Any]]) -> int:
        return self.append_rows(
            "Approval Log",
            ["batch_id", "record_id", "import_decision"],
            [
                {
                    "batch_id": batch_id,
                    "record_id": item.get("record_id", ""),
                    "import_decision": item.get("import_decision", "pending_review"),
                }
                for item in decisions
            ],
        )
