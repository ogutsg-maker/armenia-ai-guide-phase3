import json
from ai_service import AIService
from database import get_supabase_client

class PotentialPartnerAI:
    def __init__(self):
        # Подключаем наше универсальное ИИ-сердце и базу данных
        self.ai_service = AIService()
        self.db = get_supabase_client()

    def analyze_and_structure_lead(self, raw_internet_text: str) -> dict:
        """
        ИИ берет сырой текст с сайтов/карт/форумов, понимает армяно-русский контекст,
        вытаскивает ключевые параметры мастера и упаковывает в чистый JSON для админки.
        """
        system_prompt = (
            "Ты — аналитик b2b-лидов на рынке услуг Армении. Твоя задача — извлечь из сырого текста "
            "информацию о потенциальном партнере по ремонту и настройке ПК.\n\n"
            "Выведи результат СТРОГО в формате JSON со следующими полями (если поле не найдено, пиши null):\n"
            "- name (имя мастера или название фирмы, если есть)\n"
            "- services (массив строк: конкретные услуги, например: установка Windows 10/11, замена термопасты, антивирусы)\n"
            "- city (город оказания услуг, обычно Ереван, Гюмри, Ванадзор и т.д.)\n"
            "- district (конкретный район Еревана, если упоминается, например: Арабкир, Кентрон, Ачапняк)\n"
            "- min_price (минимальная цена числом в драмах AMD, если указана цифра)\n"
            "- working_hours (строка формата '10:00-20:00', если указан график)\n\n"
            "ПРАВИЛО БЕЗОПАСНОСТИ: Не пиши никаких вступлений, пояснений или markdown-разметки. Только чистый JSON."
        )

        # Отправляем запрос в наш универсальный движок. Роль 'admin', так как это парсинг админ-панели.
        ai_response = self.ai_service.process_text_request(
            user_text=raw_internet_text, 
            role="admin", 
            system_prompt=system_prompt
        )

        try:
            # На всякий случай очищаем ответ от возможных markdown-оберток (```json ... ```)
            clean_json = ai_response.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_json)
        except Exception:
            # Fallback: Если ИИ вернул сбойный формат, создаем безопасный черновик для ручного редактирования админом
            return {
                "name": "Не распознано автоматически",
                "services": ["Требуется ручной разбор текста"],
                "city": "Ереван",
                "district": None,
                "min_price": None,
                "working_hours": None
            }
    def generate_personalized_invite(self, structured_partner_data: dict) -> dict:
        """
        ИИ генерирует уникальный текст приглашения в проект на основе услуг мастера.
        Автоматически проверяет рубильники активности каналов (TG/WA/SMS) в админке.
        Если выбранный канал отключен, переходит в безопасный режим ручной отправки.
        """
        # 1. Считываем из Supabase настройки каналов связи
        active_channel = self._get_setting("active_notification_channel", "telegram")
        tg_enabled = self._get_setting("enable_telegram_channel", "true") == "true"
        wa_enabled = self._get_setting("enable_whatsapp_channel", "true") == "true"
        sms_enabled = self._get_setting("enable_sms_channel", "false") == "true"

        # Логика защиты: Проверяем, доступен ли выбранный админом канал
        final_channel = "manual"  # По умолчанию ручной режим, если всё выключено
        
        if active_channel == "telegram" and tg_enabled:
            final_channel = "telegram"
        elif active_channel == "whatsapp" and wa_enabled:
            final_channel = "whatsapp"
        elif active_channel == "sms" and sms_enabled:
            final_channel = "sms"
        else:
            # Если выбранный канал выключен рубильником, ищем любой другой рабочий
            if tg_enabled: final_channel = "telegram"
            elif wa_enabled: final_channel = "whatsapp"
            elif sms_enabled: final_channel = "sms"

        # 2. Подготавливаем контекст мастера для искусственного интеллекта
        services_list = structured_partner_data.get("services", [])
        services_str = ", ".join(services_list) if services_list else "компьютерные услуги"
        district = structured_partner_data.get("district")
        district_info = f" в районе {district}" if district else ""
        min_price = structured_partner_data.get("min_price")
        price_info = f" с ценами от {min_price} AMD" if min_price else ""

        user_context = (
            f"Мастер оказывает услуги: {services_str}{district_info}{price_info}. "
            f"Формат под который нужно написать текст: {final_channel}."
        )

        # 3. Формируем индивидуальную инструкцию для ИИ под выбранный мессенджер
        system_prompt = (
            "Ты — профессиональный B2B-копирайтер маркетплейса услуг в Армении. "
            "Напиши привлекательное, живое и не спамное приглашение для мастера компьютерной помощи. "
            "Тон дружелюбный, уважительный. Учитывай жесткие ограничения формата:\n\n"
            "ПОДСТРОЙКА ПОД КАНАЛ СВЯЗИ:\n"
            "- Если формат 'telegram' или 'whatsapp': используй аккуратные эмодзи в тему, разбей текст на короткие абзацы. "
            "Напиши, что мы автоматизировали заказы в Ереване и у нас есть клиенты на его услуги. "
            "В самом конце обязательно вставь ссылку-заглушку: [ССЫЛКА_НА_КАБИНЕТ].\n"
            "- Если формат 'sms': напиши ультра-короткий текст (строго до 140 символов!), без воды, только суть и короткая ссылка [ССЫЛКА].\n"
            "- Если формат 'manual': напиши универсальное теплое сообщение для ручной отправки в любые соцсети/мессенджеры со ссылкой [ИНВАЙТ].\n\n"
            "ЯЗЫКОВАЯ СПЕЦИФИКА: Понимай, что это Армения, но пиши текст на красивом, чистом русском языке, так как парсинг "
            "идет по русскоязычным или смешанным базам кандидатов."
        )

        # 4. Запускаем генерацию через наш универсальный ИИ-сервис
        invite_text = self.ai_service.process_text_request(
            user_text=user_context,
            role="admin",
            system_prompt=system_prompt
        )

        # Возвращаем результат админке: текст сообщения и статус, как его отправлять
        return {
            "invite_text": invite_text,
            "delivery_method": final_channel,  # 'telegram', 'whatsapp', 'sms' или 'manual'
            "is_auto_send": final_channel != "manual"  # Если ручной режим — автоотправка ложится в холдинг
        }
