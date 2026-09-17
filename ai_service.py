import os
import re
from openai import OpenAI
from groq import Groq
from psycopg.rows import dict_row

from database import _connect


class AIService:
    def __init__(self):
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        self.openai_client = OpenAI(api_key=self.openai_key) if self.openai_key else None
        self.groq_client = Groq(api_key=self.groq_key) if self.groq_key else None

    def _get_setting(self, key: str, default: str) -> str:
        """Read an optional admin AI setting without making AI depend on legacy tables."""
        try:
            with _connect() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # Current architecture stores admin settings in admin_settings.
                    cur.execute("SELECT value_json FROM admin_settings WHERE key=%s", (key,))
                    row = cur.fetchone()
                    if row and row.get("value_json") is not None:
                        value = row["value_json"]
                        if isinstance(value, dict):
                            value = value.get("value") or value.get("model")
                        if value:
                            return str(value)
        except Exception:
            pass
        return default

    def clean_sensitive_data(self, text: str) -> str:
        if self._get_setting("hide_contacts_before_payment", "true").lower() == "false":
            return text
        phone_pattern = r'(\+?\d{1,3}\s?\(?\d{2,3}\)?\s?\d{3,4}\s?\d{2,3}\s?\d{2,3}|\b0\d{2}\s?\d{3}\s?\d{3}\b|\b\d{2,3}[-\s]?\d{2,3}[-\s]?\d{2,3}\b)'
        contact_pattern = r'(@[A-Za-z0-9_]{4,})|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(https?://[^\s]+)'
        text = re.sub(phone_pattern, "[🔒 ԿՈՆՏԱԿՏԸ ԹԱՔՑՎԱԾ Է ՄԻՆՉԵՎ ՎՃԱՐՈՒՄ]", text)
        text = re.sub(contact_pattern, "[🔒 ԿՈՆՏԱԿՏԸ ԹԱՔՑՎԱԾ Է]", text)
        return text

    def process_text_request(self, user_text: str, role: str, system_prompt: str) -> str:
        clean_user_text = self.clean_sensitive_data(user_text)
        setting_key = f"{role}_ai_model"
        chosen_model = self._get_setting(setting_key, self.groq_model)
        full_system_instruction = (
            f"{system_prompt}\n\n"
            "ВАЖНЫЙ КОНТЕКСТ ДЛЯ ТЕБЯ:\n"
            "Ты работаешь на платформе услуг в Армении. Пользователи могут смешивать армянский, русский и английский. "
            "Понимай такой смешанный язык. Все цены считай в армянских драмах (AMD). "
            "Учитывай города и районы Армении."
        )

        if "groq" in chosen_model.lower() or not self.openai_client:
            if not self.groq_client:
                raise RuntimeError("GROQ_API_KEY is not configured")
            model = self.groq_model
            # Do not use the retired llama3-70b-8192 model. Current Groq
            # production model is configurable through GROQ_MODEL.
            response = self.groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": full_system_instruction},
                    {"role": "user", "content": clean_user_text},
                ],
                temperature=0.2,
            )
            return response.choices[0].message.content or ""

        response = self.openai_client.chat.completions.create(
            model=chosen_model,
            messages=[
                {"role": "system", "content": full_system_instruction},
                {"role": "user", "content": clean_user_text},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or ""

    def process_voice(self, audio_file_path: str) -> str:
        if self._get_setting("allow_voice_input", "true").lower() == "false":
            return "🔒 Голосовой ввод временно отключен администратором в настройках платформы."
        if not self.openai_client:
            return "Голосовой ввод требует OPENAI_API_KEY."
        try:
            with open(audio_file_path, "rb") as audio:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                )
            return transcript.text
        except Exception as e:
            return f"Ошибка распознавания аудио: {str(e)}"

    def process_image_price(self, image_url: str) -> str:
        if self._get_setting("allow_image_input", "true").lower() == "false":
            return "🔒 Загрузка изображений для ИИ отключена администратором в настройках платформы."
        if not self.openai_client:
            return "Анализ изображений требует OPENAI_API_KEY."
        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Прочитай этот прайс-лист. Найди услуги и цены и верни аккуратный список услуг и цен в AMD.",
                            },
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
                max_tokens=1000,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            return f"Ошибка анализа изображения: {str(e)}"


def register_master_cabinet_routes(app, ai):
    from master_cabinet_api import update_profile_by_image, update_profile_by_voice
    app["ai"] = ai
    app.router.add_post("/api/partner/cabinet/update-by-image", update_profile_by_image)
    app.router.add_post("/api/partner/cabinet/update-by-voice", update_profile_by_voice)


GroqAI = AIService
