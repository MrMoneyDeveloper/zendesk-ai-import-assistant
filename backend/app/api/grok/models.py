from dataclasses import dataclass


@dataclass(frozen=True)
class GrokModelInfo:
    model_id: str
    purpose: str
    docs_url: str


RECOMMENDED_MODEL = "grok-4.3"

SUPPORTED_MODELS: dict[str, GrokModelInfo] = {
    RECOMMENDED_MODEL: GrokModelInfo(
        model_id=RECOMMENDED_MODEL,
        purpose="General-purpose Grok model used in xAI quickstart examples.",
        docs_url="https://docs.x.ai/docs",
    ),
    "openai/gpt-oss-20b": GrokModelInfo(
        model_id="openai/gpt-oss-20b",
        purpose="Groq-hosted OpenAI-compatible OSS model.",
        docs_url="https://console.groq.com/docs/openai",
    ),
}

RATE_LIMIT_SUMMARY = {
    "source": "https://docs.x.ai/developers/rate-limits",
    "notes": [
        "Rate limits vary by model and by team tier.",
        "Per-model RPM/TPM values are shown in xAI Console.",
        "Text model tiers: Tier 0 ($0), Tier 1 ($50), Tier 2 ($250), Tier 3 ($1,000), Tier 4 ($5,000), Enterprise.",
    ],
}
