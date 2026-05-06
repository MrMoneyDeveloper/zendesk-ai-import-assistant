# Grok API Configuration

This folder contains the Grok API integration used by the backend.

- `client.py`: HTTP client for `https://api.x.ai/v1/chat/completions`
- `models.py`: model metadata and rate-limit tier notes

Current defaults:

- Model: `grok-4.3`
- Base URL: `https://api.x.ai/v1`
- API key env var: `XAI_API_KEY`

Official docs used:

- Models: https://docs.x.ai/developers/models?cluster=us-east-1
- Rate limits: https://docs.x.ai/developers/rate-limits
- Quickstart: https://docs.x.ai/developers/quickstart
