# Operating Model Benchmark

Zendesk deployment and external Sheet staging were disabled for every variant.

| variant | status | wall_time_seconds | record_count | generator_calls | supervisor_calls | total_tokens | gemini_tokens | groq_tokens | fallback_chunks | blocked | avg_quality_score | avg_article_body_chars | avg_macro_body_chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| template | preview_ready | 409.0 | 33 | 32 | 16 | 162755 | 142424 | 20331 | 4 | 5 | 0.61 | 378.0 | 191.5 |
| hybrid | preview_ready | 304.08 | 42 | 12 | 17 | 193605 | 191320 | 2285 | 1 | 9 | 0.68 | 663.2 | 191.5 |
| hybrid | preview_ready | 135.96 | 39 | 4 | 11 | 116033 | 113790 | 2243 | 0 | 0 | 1.0 | 709.2 | 232.0 |
