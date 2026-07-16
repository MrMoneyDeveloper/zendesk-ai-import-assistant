# Progress Narrator Benchmark

Synthetic orchestration events only. No Zendesk deployment was attempted.

| Provider | Valid | Model responses | Fallbacks | Avg ms | Tokens | Descriptive |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| deterministic | 3/3 | 0 | 0 | 0.01 | 0 | 1.0 |
| gemini | 3/3 | 3 | 0 | 1470.27 | 974 | 0.667 |
| groq | 3/3 | 3 | 0 | 721.44 | 1006 | 0.867 |

Recommendation: **groq**

Use Groq for low-volume wave narration while Gemini remains dedicated to planning, generation, and supervision; verified deterministic events remain the fallback.
