# Progress Narrator Benchmark

Synthetic orchestration events only. No Zendesk deployment was attempted.

| Provider | Valid | Model responses | Fallbacks | Avg ms | Tokens | Descriptive |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| deterministic | 3/3 | 0 | 0 | 0.03 | 0 | 1.0 |
| gemini | 3/3 | 3 | 0 | 2033.21 | 975 | 0.8 |
| groq | 3/3 | 3 | 0 | 772.87 | 830 | 1.0 |

Recommendation: **groq**

Use Groq for low-volume wave narration while Gemini remains dedicated to planning, generation, and supervision; verified deterministic events remain the fallback.
