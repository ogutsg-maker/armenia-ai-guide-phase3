# Armenia AI Guide — Deploy Guide

## 1. Environment
Copy `.env.example` to `.env` and set:
- `TELEGRAM_BOT_TOKEN`
- `ADMIN_TELEGRAM_ID`
- `GROQ_API_KEY`
- `OPENAI_API_KEY` (optional, used for Whisper and image analysis)
- `DATABASE_URL` (Supabase/PostgreSQL)
- `WEBAPP_BASE_URL`
- Supabase Storage variables if partner documents should use private Storage
- `SEARCH_PROVIDER=serper` + `SERPER_API_KEY` for real AI research of potential partners

Do not commit `.env`.

## 2. Local
```powershell
python -m pip install -r requirements.txt
python main.py
```

Server: `http://localhost:8000`

## 3. Render / Docker
The project starts with `python main.py` and listens on `PORT` (default `8000`).

## 4. Database
The application performs non-destructive migrations on startup. Existing legacy tables/data are preserved.

## 5. Telegram Web App
Set the bot's Web App URL to the deployed `WEBAPP_BASE_URL`.
The root page is the Armenia AI Guide welcome screen. Partner onboarding itself continues through the Telegram AI conversation.

## 6. Search provider
The real potential-partner research module does not invent businesses. If no search provider is configured, it returns `SEARCH_PROVIDER_NOT_CONFIGURED` instead of generating fake candidates.
