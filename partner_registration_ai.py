import json
from ai_service import AIService
from database import get_supabase_client

class PartnerRegistrationAI:
    def __init__(self):
        # Подключаем наше универсальное ИИ-сердце и базу данных Supabase
        self.ai_service = AIService()
        self.db = get_supabase_client()

    def _is_document_required(self, category: str) -> bool:
        """
        Проверяет по базе данных или правилам, требуется ли официальный документ/лицензия 
        для выбранной мастером категории услуг (например, для ремонта сложной электроники или юр. услуг)
        """
        if not category:
            return False
        # Для базового ремонта ПК и установки Windows документы обычно не обязательны,
        # но если это сложный ремонт плат/пайка или B2B услуги, мы можем включить требование документа.
        strict_categories = ["ремонт плат", "пайка", "серверное оборудование", "b2b сети"]
        return any(sc in category.lower() for sc in strict_categories)

    def generate_interview_prompt(self, current_profile: dict) -> str:
        """Формирует для ИИ жесткую инструкцию по проведению интервью с мастером"""
        # Превращаем текущую анкету в понятную для ИИ сводку
        services = ", ".join(current_profile.get("services", [])) or "Не указаны"
        
        prompt = (
            "Ты — умный и приветливый b2b-менеджер маркетплейса услуг в Армении. "
            "Твоя задача — помочь потенциальному партнеру (мастеру) пройти регистрацию на сайте через живой диалог.\n"
            "Общайся вежливо, используй армяно-русский контекст. Твоя цель — собрать полноценный бизнес-профиль.\n\n"
            f"ТЕКУЩИЙ ПРОФИЛЬ МАСТЕРА В БАЗЕ:\n"
            f"- Имя: {current_profile.get('name', 'Не указано')}\n"
            f"- Город/Район: {current_profile.get('district', 'Не указано')} (работает в Ереване)\n"
            f"- Услуги: {services}\n"
            f"- Цены: {current_profile.get('min_price', 'Не указаны')}\n"
            f"- График и выходные: {current_profile.get('working_hours', 'Не указаны')}\n"
            f"- Выезд к клиенту: {current_profile.get('on_site', 'Не указано')}\n"
            f"- Документ загружен: {current_profile.get('document_uploaded', 'Нет')}\n\n"
            "ЧТО ТЕБЕ НУЖНО СДЕЛАТЬ:\n"
            "1. Проанализируй текущий профиль и новое сообщение мастера.\n"
            "2. Найди, чего не хватает. Будь гибким, подсказывай мастеру, что он мог упустить! "
            "Например, если он написал 'ставлю Windows', спроси: 'А выезжаете ли вы к клиенту на дом?', "
            "'В каких районах Еревана вам удобнее работать?', 'Есть ли у вас выходные или вы работаете 24/7?', "
            "'Какая минимальная цена за выезд?'.\n"
            "3. Если направление сложное (например, пайка чипов) и поле document_uploaded равно False, "
            "напомни мастеру, что для активации профиля админу потребуется фото его сертификата или паспорта.\n"
            "4. Задавай за один раз не более 1-2 коротких вопросов, чтобы не перегружать человека.\n"
            "5. Если ВСЕ данные полностью собраны, профиль идеален и документ (если нужен) загружен, "
            "напиши финальное поздравление и в самом конце добавь скрытый маркер: [REGISTRATION_COMPLETE_JSON]"
        )
        return prompt
    def handle_partner_message(self, partner_id: int, user_message: str, current_profile: dict) -> dict:
        """
        Основной метод ведения диалога регистрации с партнером.
        Анализирует текст сообщения, извлекает новые данные, сохраняет их,
        генерирует уточняющие вопросы и сигнализирует админке о готовности профиля.
        """
        # Шаг 1: Извлекаем новые данные из сообщения пользователя с помощью ИИ
        # Делаем быстрый скрытый запрос, чтобы понять, добавил ли мастер новые параметры
        data_extraction_prompt = (
            "Проанализируй сообщение мастера и извлеки новые данные для профиля. "
            "Верни СТРОГО JSON с полями (если данных в сообщении нет, пиши null):\n"
            "- name (имя мастера)\n"
            "- district (район Еревана, например: Арабкир, Кентрон)\n"
            "- min_price (минимальная цена числом в AMD)\n"
            "- working_hours (график, например: 10:00-20:00, без выходных)\n"
            "- on_site (выезд к клиенту: true/false)\n"
            "- services (массив строк с новыми услугами, если упомянуты)\n"
            "Не пиши никаких объяснений, только чистый JSON."
        )

        extracted_data_raw = self.ai_service.process_text_request(
            user_text=user_message,
            role="partner",
            system_prompt=data_extraction_prompt
        )

        try:
            clean_json = extracted_data_raw.replace("```json", "").replace("```", "").strip()
            new_data = json.loads(clean_json)
            
            # Обновляем наш текущий профиль новыми данными, если они были найдены
            for key, val in new_data.items():
                if val is not None:
                    if key == "services" and isinstance(val, list):
                        # Смерживаем существующие услуги с новыми
                        current_profile["services"] = list(set(current_profile.get("services", []) + val))
                    else:
                        current_profile[key] = val
        except Exception:
            pass # Если ИИ выдал неидеальный формат, просто двигаемся дальше по тексту

        # Проверяем, нужна ли лицензия/документ для услуг мастера
        categories_str = " ".join(current_profile.get("services", []))
        if self._is_document_required(categories_str):
            current_profile["document_required"] = True
        else:
            current_profile["document_required"] = False

        # Шаг 2: Генерируем живой ответ мастера с наводящими вопросами
        interview_prompt = self.generate_interview_prompt(current_profile)
        
        ai_reply = self.ai_service.process_text_request(
            user_text=user_message,
            role="partner",
            system_prompt=interview_prompt
        )

        registration_complete = False
        # Шаг 3: Проверяем, завершен ли сбор данных
        if "[REGISTRATION_COMPLETE_JSON]" in ai_reply:
            ai_reply = ai_reply.replace("[REGISTRATION_COMPLETE_JSON]", "").strip()
            registration_complete = True
            
            # Переводим статус партнера в Supabase в состояние "ready_for_review" (на рассмотрении админа)
            try:
                self.db.table("partners").update({
                    "status": "ready_for_review",
                    "profile_data": current_profile
                }).eq("id", partner_id).execute()
                
                # Также обновляем лог в таблице потенциальных партнеров, если он там был
                self.db.table("potential_partners").update({
                    "status": "ready_for_review"
                }).eq("id", partner_id).execute()
            except Exception:
                pass

        # Возвращаем боту текст ответа, обновленный профиль и флаг готовности для админки
        return {
            "reply_text": ai_reply,
            "updated_profile": current_profile,
            "registration_complete": registration_complete
        }
