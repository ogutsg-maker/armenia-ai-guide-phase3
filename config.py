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
AI_MODEL="openai/gpt-oss-20b"
OPENAI_MODEL=os.getenv("OPENAI_MODEL","gpt-4o-mini")
OPENROUTER_MODEL=os.getenv("OPENROUTER_MODEL","openai/gpt-4o-mini")