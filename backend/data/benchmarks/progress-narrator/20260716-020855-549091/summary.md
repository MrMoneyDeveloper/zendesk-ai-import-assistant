# Progress Narrator Benchmark

Synthetic orchestration events only. No Zendesk deployment was attempted.

| Provider | Valid | Model responses | Fallbacks | Avg ms | Tokens | Descriptive |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| deterministic | 3/3 | 0 | 0 | 0.01 | 0 | 1.0 |
| gemini | 3/3 | 3 | 0 | 2047.15 | 970 | 0.733 |
| groq | 3/3 | 0 | 3 | 736.62 | 1164 | 1.0 |

Recommendation: **gemini**

Use Gemini narration because it produced the strongest valid summaries in this run; deterministic events remain the fallback.
