# Operating Model Benchmark

Zendesk deployment and external Sheet staging were disabled for every variant.

| variant | status | wall_time_seconds | record_count | generator_calls | supervisor_calls | total_tokens | gemini_tokens | groq_tokens | fallback_chunks | blocked | avg_quality_score | avg_article_body_chars | avg_macro_body_chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| template | preview_ready | 163.97 | 119 | 0 | 35 | 606222 | 606222 | 0 | 0 | 0 | 0.98 | 838.93 | 398.38 |
| hybrid | preview_ready | 212.4 | 119 | 14 | 35 | 632900 | 619869 | 13031 | 0 | 0 | 0.97 | 1022.29 | 582.38 |
