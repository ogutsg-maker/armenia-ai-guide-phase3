import os
import re
import psycopg
from openai import OpenAI
from groq import Groq
from psycopg.rows import dict_row

# Импортируем вашу родную функцию подключения напрямую из database.py
from database import _connect 

class AIService:
    def __init__(self):
        # Инициализируем клиентов ИИ. Ключи подтягиваются из вашего файла .env
        self.openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self.groq_client = Groq(api_key=os.getenv("GROQ_API_KEY", ""))

    def _get_setting(self, key: str, default: str) -> str:
        """
        Вытягивает значение переключателя напрямую из таблицы system_settings 
        через ваш родной асинхронно-безопасный драйвер psycopg3.
        """
        try:
            with _connect() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
                    row = cur.fetchone()
                    if row:
                        return str(row["value"])
        except Exception:
            pass
        return default

    def clean_sensitive_data(self, text: str) -> str:
        """
        🔒 ЗАЩИТА ПЛАТФОРМЫ ОТ ОБХОДА:
        Если в админке включен режим анонимности, метод жестко вырезает любые 
        телефонные номера, ссылки и логины мессенджеров из чатов до оплаты сделки.
        """
        if self._get_setting("hide_contacts_before_payment", "true") == "false":
            return text

        # Паттерн для телефонов (+374..., 093..., 098..., +7..., 89... и локальных форматов Армении)
        phone_pattern = r'(\+?\d{1,3}\s?\(?\d{2,3}\)?\s?\d{3,4}\s?\d{2,3}\s?\d{2,3}|\b0\d{2}\s?\d{3}\s?\d{3}\b|\b\d{2,3}[-\s]?\d{2,3}[-\s]?\d{2,3}\b)'
        
        # Паттерн для email-адресов, веб-ссылок и юзернеймов Telegram (@username)
        contact_pattern = r'(@[A-Za-z0-9_]{4,})|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})|(https?://[^\s]+)'
        
        text = re.sub(phone_pattern, "[🔒 КОНТАКТЫ СКРЫТЫ ДО ОПЛАТЫ КОМИССИИ]", text)
        text = re.sub(contact_pattern, "[🔒 ССЫЛКА СКРЫТА]", text)
        return text
    def process_text_request(self, user_text: str, role: str, system_prompt: str) -> str:
        """
        Универсальный обработчик текста. Сам заглядывает в базу настроек через psycopg,
        выбирает нужного провайдера (Groq или OpenAI) для конкретной роли (client, partner, admin)
        и предварительно очищает текст от скрытых контактных данных.
        """
        # Безопасность: автоматически вырезаем телефоны, если сделка еще не оплачена
        clean_user_text = self.clean_sensitive_data(user_text)

        # Вытаскиваем из Supabase, какую модель админ выбрал для этой роли
        setting_key = f"{role}_ai_model"
        chosen_model = self._get_setting(setting_key, "groq-llama3")

        # Базовая инструкция для ИИ с учетом специфики Армении и смешивания языков
        full_system_instruction = (
            f"{system_prompt}\n\n"
            "ВАЖНЫЙ КОНТЕКСТ ДЛЯ ТЕБЯ:\n"
            "Ты работаешь на платформе услуг в Армении (основной город — Ереван). "
            "Пользователи и мастера очень часто смешивают армянские слова, русский сленг и технический английский "
            "(примеры: винда, форматнуть, мастер, кабель, կարգավորել, էկրան). "
            "Ты обязан идеально понимать этот языковой суржик. "
            "Все валюты считай строго в армянских драмах (AMD). "
            "Локации сопоставляй с районами Еревана (Арабкир, Кентрон, Малатия-Себастия, Нор-Норк и др.)."
        )

        # Сценарий 1: Админ включил для этой роли Groq (Llama 3)
        if "groq" in chosen_model.lower():
            response = self.groq_client.chat.completions.create(
                model="llama3-70b-8192",  # Быстрая открытая модель на мощностях Groq
                messages=[
                    {"role": "system", "content": full_system_instruction},
                    {"role": "user", "content": clean_user_text}
                ],
                temperature=0.2
            )
            return response.choices.message.content

        # Сценарий 2: Админ включил для этой роли OpenAI (GPT-4o)
        else:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",  # Быстрая, точная и экономичная модель от OpenAI
                messages=[
                    {"role": "system", "content": full_system_instruction},
                    {"role": "user", "content": clean_user_text}
                ],
                temperature=0.2
            )
            return response.choices.message.content

    def process_voice(self, audio_file_path: str) -> str:
        """
        Превращение голоса (аудиосообщения) в текст с проверкой рубильника активности.
        Использует Whisper API от OpenAI.
        """
        if self._get_setting("allow_voice_input", "true") == "false":
            return "🔒 Голосовой ввод временно отключен администратором в настройках платформы."
        
        try:
            with open(audio_file_path, "rb") as audio:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1", 
                    file=audio
                )
            return transcript.text
        except Exception as e:
            return f"Ошибка распознавания аудио: {str(e)}"

    def process_image_price(self, image_url: str) -> str:
        """
        Сканирование бумажных прайсов и документов от мастеров с проверкой рубильника.
        Для анализа картинок всегда используется GPT-4o mini из-за поддержки зрения (Vision).
        """
        if self._get_setting("allow_image_input", "true") == "false":
            return "🔒 Загрузка изображений для ИИ отключена администратором в настройках платформы."

        try:
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text", 
                                "text": "Ты — модуль зрения. Внимательно прочитай текст с этой фотографии прайс-листа мастера. "
                                        "Найди все услуги и цены. Верни аккуратный текстовый список услуг и цен в AMD цифрами."
                            },
                            {
                                "type": "image_url", 
                                "image_url": {"url": image_url}
                            }
                        ],
                    }
                ],
                max_tokens=1000
            )
            return response.choices.message.content
        except Exception as e:
            return f"Ошибка анализа изображения: {str(e)}"


def register_master_cabinet_routes(app, ai):
    """Регистрация ИИ-маршрутов личного кабинета мастера в общем приложении"""
    # Ленивый импорт функций обработчиков, чтобы избежать круговых зависимостей
    from master_cabinet_api import update_profile_by_image, update_profile_by_voice
    app['ai'] = ai
    app.router.add_post('/api/partner/cabinet/update-by-image', update_profile_by_image)
    app.router.add_post('/api/partner/cabinet/update-by-voice', update_profile_by_voice)


# =====================================================================
# ОБРАТНАЯ СОВМЕСТИМОСТЬ ДЛЯ ГԼԱВНОГО ФАЙЛА main.py (ЗАЩИТА ОТ СБОЯ RENDER)
# =====================================================================
GroqAI = AIService
