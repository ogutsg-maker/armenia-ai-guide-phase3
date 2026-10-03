# Armenia AI Guide — clean architecture
This repository is rebuilt from a clean tree around the agreed lifecycle.

Layers: Telegram/UI → AI Core → AI Context → Data Core → PostgreSQL.

Partner registration is minimal: business name + phone. Services are separate: AI parses free text, backend creates a pending service only after partner confirmation, admin approves it, then it becomes ACTIVE.

AI never decides database IDs or lifecycle truth. Booking, payment confirmation, QR expiry, check-in, completion and review are deterministic backend actions.

Runtime: Python 3.12, aiohttp, aiogram, PostgreSQL/Supabase, OpenAI-compatible AI provider chain Groq → OpenAI → OpenRouter.
