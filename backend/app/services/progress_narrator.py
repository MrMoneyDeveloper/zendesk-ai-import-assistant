from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.api.gemini.client import GeminiInteractionClient
from app.api.grok.client import GrokClient
from app.core.settings import get_settings


_WAVE_PURPOSES = {
    1: "laying out Help Center categories and sections so article destinations exist",
    2: "creating shared fields and department groups used by later routing",
    3: "assembling department forms and queue views against verified dependencies",
    4: "building routing triggers, time-based automations, and agent macros",
    5: "drafting Help Center articles into the generated topic structure",
}


def _clean_names(values: list[object], *, limit: int = 4) -> list[str]:
    output: list[str] = []
    for value in values:
        text = " ".join(str(value or "").strip().split())
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _label_from_item(item: dict[str, Any]) -> str:
    return str(
        item.get("department_name")
        or item.get("topic")
        or item.get("title_hint")
        or item.get("object_type")
        or "Shared operating model"
    ).strip()


def build_wave_start_message(
    *,
    wave: int,
    wave_position: int,
    total_waves: int,
    items: list[dict[str, Any]],
) -> str:
    object_types = _clean_names([item.get("object_type") for item in items])
    labels = _clean_names([_label_from_item(item) for item in items], limit=3)
    type_text = ", ".join(value.replace("_", " ") for value in object_types) or "records"
    label_text = ", ".join(labels)
    if len(items) > len(labels):
        label_text = f"{label_text}, and {len(items) - len(labels)} more" if label_text else f"{len(items)} bundles"
    purpose = _WAVE_PURPOSES.get(wave, "building the next dependency layer")
    return (
        f"Wave {wave_position}/{total_waves} started: {purpose}. "
        f"The queue contains {len(items)} {type_text} work item(s)"
        f"{f' across {label_text}' if label_text else ''}; each result is checked before the next dependency wave opens."
    )


def build_chunk_message(
    *,
    wave_position: int,
    total_waves: int,
    item: dict[str, Any],
    object_type: str,
    chunk_index: int,
    chunk_total: int,
    target_count: int,
    mode: str,
) -> str:
    label = _label_from_item(item)
    object_label = object_type.replace("_", " ")
    mode_text = {
        "department_template": "using deploy-safe templates",
        "gemini": "using Gemini as the primary drafting model",
        "groq_fallback": "using a Groq fallback lane after Gemini recovery was exhausted",
    }.get(mode, "using the active generation route")
    return (
        f"Wave {wave_position}/{total_waves}, {label}: building {target_count} {object_label} "
        f"record(s) in chunk {chunk_index}/{chunk_total} {mode_text}. "
        "Names and dependencies stay explicit so Zendesk IDs can be resolved only during deployment."
    )


def build_wave_complete_message(
    *,
    wave: int,
    wave_position: int,
    total_waves: int,
    wave_meta: dict[str, Any],
    cumulative_counts: dict[str, int],
    supervisor_summary: str = "",
) -> str:
    generated = int(wave_meta.get("generated_records", 0) or 0)
    chunks = int(wave_meta.get("chunks", 0) or 0)
    blocked = int(wave_meta.get("blocked", 0) or 0)
    cumulative_total = sum(max(int(value or 0), 0) for value in cumulative_counts.values())
    next_purpose = (
        "final validation and staging"
        if wave_position >= total_waves
        else _WAVE_PURPOSES.get(wave + 1, "the next dependency layer")
    )
    safe_supervisor_summary = str(supervisor_summary or "").strip()
    for premature_state, replacement in (
        (r"\bstaged\b", "reviewed"),
        (r"\bvalidated\b", "checked"),
        (r"\bdeployed\b", "prepared"),
        (r"\bpublished\b", "drafted"),
        (r"\bapproved\b", "accepted by the quality gate"),
    ):
        safe_supervisor_summary = re.sub(
            premature_state,
            replacement,
            safe_supervisor_summary,
            flags=re.IGNORECASE,
        )
    supervisor_text = (
        f" Supervisor result: {safe_supervisor_summary}"
        if safe_supervisor_summary
        else " Supervisor and deterministic gates have finished for this checkpoint."
    )
    return (
        f"Wave {wave_position}/{total_waves} completed with {generated} record(s) from {chunks} chunk(s) "
        f"and {blocked} blocked item(s); the cumulative operating model now contains {cumulative_total} record(s)."
        f"{supervisor_text} Next: {next_purpose}."
    )


@dataclass(frozen=True)
class NarrationResult:
    message: str
    source: str
    provider: str | None
    model: str | None
    fallback_used: bool
    telemetry: dict[str, Any]


def _validate_narration(candidate: str, deterministic_message: str) -> str | None:
    cleaned = " ".join(str(candidate or "").strip().strip('"').split())
    if len(cleaned) < 25 or len(cleaned) > 520:
        return None
    lowered = cleaned.lower()
    if any(
        phrase in lowered
        for phrase in (
            "chain-of-thought",
            "hidden reasoning",
            "i think",
            "my reasoning",
            "i cannot",
        )
    ):
        return None
    allowed_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", deterministic_message))
    candidate_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", cleaned))
    if not candidate_numbers.issubset(allowed_numbers):
        return None
    deterministic_lowered = deterministic_message.lower()
    for premature_claim in (
        "approved",
        "deployed",
        "staged",
        "validated",
        "preview ready",
        "published",
    ):
        if premature_claim in lowered and premature_claim not in deterministic_lowered:
            return None
    return cleaned


class ProgressNarrator:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.progress_narrator_enabled
            and self.settings.progress_narrator_max_calls_per_batch > 0
        )

    async def narrate(
        self,
        *,
        deterministic_message: str,
        event_context: dict[str, Any],
    ) -> NarrationResult:
        if not self.enabled:
            return NarrationResult(
                message=deterministic_message,
                source="deterministic",
                provider=None,
                model=None,
                fallback_used=False,
                telemetry={},
            )

        provider = str(self.settings.progress_narrator_provider or "groq").strip().lower()
        prompt_payload = {
            "task": "public_operational_progress_summary",
            "rules": [
                "Write one concise, descriptive status update for an end user.",
                "Use only facts supplied below; never invent counts, approval, deployment, or completion.",
                "Describe what was built, what was checked, and what happens next.",
                "Do not expose or claim hidden chain-of-thought.",
                "Return plain text only, under 70 words.",
            ],
            "deterministic_source_message": deterministic_message,
            "event_context": event_context,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You turn verified orchestration events into public progress copy. "
                    "Never add facts or hidden reasoning."
                ),
            },
            {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
        ]
        try:
            if provider == "gemini":
                client = GeminiInteractionClient()
                raw = await client.chat(
                    messages,
                    task="narrator",
                    model=self.settings.gemini_default_model,
                )
                telemetry = client.get_last_call_metrics("narrator")
                model = self.settings.gemini_default_model
                provider_label = "Gemini"
            else:
                client = GrokClient()
                raw = await client.chat(
                    messages,
                    temperature=0.1,
                    model=self.settings.progress_narrator_model,
                    max_output_tokens=self.settings.progress_narrator_max_output_tokens,
                    strict_schema=False,
                    task="narrator",
                    response_format_override="none",
                    api_key_override=(
                        self.settings.xai_api_key_tertiary
                        or self.settings.xai_api_key_secondary
                        or self.settings.xai_api_key
                    ),
                    prefer_provider="groq",
                    reasoning_effort=(
                        "low"
                        if "gpt-oss" in self.settings.progress_narrator_model.lower()
                        else None
                    ),
                    reasoning_format="hidden",
                )
                telemetry = client.get_last_call_metrics("narrator")
                model = self.settings.progress_narrator_model
                provider_label = "Groq"
            validated = _validate_narration(raw, deterministic_message)
            if validated:
                return NarrationResult(
                    message=validated,
                    source="model_narrator",
                    provider=provider_label,
                    model=model,
                    fallback_used=False,
                    telemetry=telemetry,
                )
        except Exception as exc:  # noqa: BLE001
            telemetry = {"error": type(exc).__name__}

        return NarrationResult(
            message=deterministic_message,
            source="deterministic_fallback",
            provider=provider.capitalize(),
            model=(
                self.settings.gemini_default_model
                if provider == "gemini"
                else self.settings.progress_narrator_model
            ),
            fallback_used=True,
            telemetry=telemetry,
        )
