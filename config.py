from __future__ import annotations
import os
from dotenv import load_dotenv
load_dotenv()
BOT_TOKEN=os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID=int(os.environ["ADMIN_TELEGRAM_ID"])
DATABASE_URL=os.environ["DATABASE_URL"]
WEBAPP_BASE_URL=os.getenv("WEBAPP_BASE_URL","").rstrip("/")
GROQ_API_KEY=os.getenv("GROQ_API_KEY","")
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","")
OPENROUTER_API_KEY=os.getenv("OPENROUTER_API_KEY","")
AI_MODEL=os.getenv("GROQ_MODEL","openai/gpt-oss-20b")
AI_TIMEOUT=float(os.getenv("AI_TIMEOUT","30"))
IDRAM_PAYMENT_URL=os.getenv("IDRAM_PAYMENT_URL","")
COMMISSION_MODE=os.getenv("COMMISSION_MODE","on_top")
COMMISSION_RATE=float(os.getenv("COMMISSION_RATE","10"))
PAYMENT_WEBHOOK_SECRET=os.getenv("PAYMENT_WEBHOOK_SECRET","")
