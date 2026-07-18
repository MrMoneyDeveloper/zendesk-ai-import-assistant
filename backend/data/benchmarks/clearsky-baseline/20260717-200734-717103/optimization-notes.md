# ClearSky Optimization Notes

Zendesk deployment and external Google Sheet staging were disabled during the baseline.

## Measured Baseline

- Status: `preview_ready`
- Wall time: `409.0 seconds`
- Records: `33`
- Model calls: `55` (`32` generator, `16` supervisor, `2` planner, `5` narrator)
- Tokens: `162,755`
- Retries: `12`
- Provider wait time: `159.44 seconds`
- Validation: `28` warnings and `5` blocked articles
- Missing or incorrect dependencies: only one group, one category, no sections, nine fields, and two forms

## Optimized Execution Budget

- Exact target: `39` records
- Exact dependencies: `5` named groups, `3` categories, `3` sections, `5` fields, and `3` forms
- Runtime chunks: `12`
- Model-drafted chunks: `7`
- Deterministic explicit-structure chunks: `5`
- Initial Gemini supervisor bundles: `9` instead of `16`
- Malformed Gemini generator output now hands off to Groq without repeating the same request
- Article fallback retains exact article titles and same-batch section names
- Backend auto-reload is disabled in stable demo mode

## Provider Health Sample

- Gemini structured JSON: first-attempt success, `61` tokens, `10.59 seconds`
- Gemini supervisor schema: first-attempt success, `976` tokens, `7.94 seconds`
- Groq fallback structured JSON: first-attempt success, `197` tokens, `0.86 seconds`

These are endpoint health samples, not an optimized end-to-end timing result.

## Verification

- Backend: `158 passed`
- Frontend lint: no errors, one existing TanStack Table compiler warning
- Frontend production build: passed
- Optimized live comparison: awaiting explicit approval to send the attached ClearSky prompt to configured external model APIs
