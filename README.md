# IT Learn Backend

Welcome to the backend of IT Learn!
This is the server-side of our platform, built with Flask and Supabase, powering the API, user authentication, progress tracking, and more.

If you want to help improve the backend, please only create PRs to the beta branch. Any PR for the main branch will be rejected.

🚀 Features

🔐 User authentication with signup/login and session management

📚 Progress tracking for lessons, XP, streaks, and missions

✅ Cloudflare Turnstile verification to prevent bots

🌐 CORS configured to allow only trusted frontend domains

💾 Supabase integration for database and auth management

⚡ REST API endpoints for user session, progress, and user count

# 🧠 AI Proxy Endpoint

## POST `/api/ai?p=<systemPromptKey>`
A proxy endpoint that forwards chat requests to **Hack Club AI** (`https://ai.hackclub.com/proxy/v1/chat/completions`).

### Allowed frontend pages (Referer allowlist)
This endpoint is restricted:
- **Origin** must be one of:
  - `https://itlearn.be`
  - `https://it-learn.pages.dev`
- **Referer path** must start with one of the configured prefixes in:
  - `AI_ALLOWED_PATH_PREFIXES` (env var, comma-separated)

Example:
```env
AI_ALLOWED_PATH_PREFIXES=/practice,/lesson,/ai
```

If `Referer` is missing or the path doesn’t match, you’ll get:
- `403 Forbidden`

### System prompt selection (`?p=`)
`p` selects a server-side system prompt key.

Current keys in code:
- `p=projects-assist` (projects assistant)

If you use an unknown key, you’ll get:
- `400 Unknown system prompt key`

### Request JSON body
You can send either:

**Option A: OpenAI-compatible messages**
```json
{
  "messages": [
    { "role": "user", "content": "Hello" }
  ]
}
```

**Option B: simple content**
```json
{
  "content": "Hello"
}
```
(Backend converts it to a single `user` message.)

**Optional fields**
- `model`: string (proxy model name)
- `temperature`: number (default `0.2`)

### Response
Success (`200`):
```json
{
  "ok": true,
  "content": "<assistant text>",
  "model": "<model name>"
}
```

### Error codes
- `500 Server misconfigured: missing OPENAI_API_KEY`
- `400` invalid request body / unknown prompt key
- `403 Forbidden` referer/origin restriction failed
- `413` request too large (prompt size limit)
- `429` rate limit exceeded
- Upstream proxy errors (returned as `{"error": ..., "status": ...}`)

