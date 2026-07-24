# Free Deployment: Vercel and Render

This repository deploys as two services:

- `backend` -> Render free Python web service
- `frontend` -> Vercel Vite project

## 1. Render Backend

1. In Render, select **New > Blueprint**.
2. Connect this GitHub repository.
3. Render will read `render.yaml`.
4. Enter the requested secret environment values from `backend/.env`.
5. Do not include quotes around values unless the quote is part of the value.
6. Deploy and copy the resulting URL:

```text
https://YOUR-RENDER-SERVICE.onrender.com
```

The Blueprint already configures:

```text
Plan: free
Root directory: backend
Build: pip install -r requirements.txt
Start: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Health check: /
Python: 3.13.7
```

Add any extra Gemini, Groq, supervisor, narrator, or timeout settings from the
local `backend/.env` through Render's **Environment** page.

## 2. Vercel Frontend

1. In Vercel, select **Add New > Project**.
2. Import the same GitHub repository.
3. Set **Root Directory** to `frontend`.
4. Keep the detected Vite framework settings.
5. Add this Production environment variable:

```text
VITE_API_BASE_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

6. Deploy and copy the resulting Vercel URL.

## 3. Confirm CORS

The Blueprint pins this production origin:

```text
FRONTEND_ORIGIN=https://zendesk-ai-import-assistant.vercel.app
```

If the Vercel project URL changes, update both `render.yaml` and the Render
environment value to the exact new origin, including `https://` and without a
trailing slash. Saving a Render environment change restarts the service, so do
not change environment variables or redeploy while a generation run is active.

## 4. Verify

Open these URLs:

```text
https://YOUR-RENDER-SERVICE.onrender.com/
https://YOUR-VERCEL-PROJECT.vercel.app/
```

Then validate the Zendesk session in the Vercel application and run an
**Ask or verify** request before trying a generated preview.

The free Render filesystem is temporary. Batch and conversation history can
reset when the service sleeps, restarts, or redeploys. Google Sheets data is
not affected.
