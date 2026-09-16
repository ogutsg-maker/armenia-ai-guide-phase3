import os
import json
import logging
import asyncio
from groq import Groq
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class ModerationResult(BaseModel):
    is_safe: bool = Field(description="True, если сообщение безопасное. False, если обнаружены контакты.")
    reason: str = Field(description="Причина блокировки или 'Одобрено'.")

class AISecurityManager:
    def __init__(self):
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

    async def check_message(self, text: str) -> ModerationResult:
        system_prompt = (
            "Ты — строгий ИИ-модератор маркетплейса услуг в Армении.\n"
            "Заблокируй любую попытку передать контакты ДО оплаты.\n"
            "Если в тексте есть номер телефона (093..., +374..., словами или цифрами) или ссылки (list.am, t.me, инстаграм), "
            "верни is_safe = false. Если текст безопасный, верни is_safe = true.\n\n"
            "ОТВЕТЬ СТРОГО В ФОРМАТЕ JSON:\n"
            "{\n"
            "  \"is_safe\": true или false,\n"
            "  \"reason\": \"Причина блокировки на русском языке\"\n"
            "}"
        )

        try:
            loop = asyncio.get_event_loop()
            chat_completion = await loop.run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text}
                    ],
                    response_format={"type": "json_object"}
                )
            )

            # Исправленное корректное обращение по индексу [0]
            raw_json = chat_completion.choices[0].message.content
            logging.info(f"Модерация JSON: {raw_json}")
            
            parsed_data = json.loads(raw_json)
            return ModerationResult(**parsed_data)

        except Exception as e:
            logging.error(f"Ошибка в AISecurityManager: {e}")
            return ModerationResult(is_safe=True, reason="Ошибка проверки")
