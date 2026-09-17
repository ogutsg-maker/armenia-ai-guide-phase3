import json
from ai_service import AIService
from database import get_supabase_client
# Подтягиваем готовую схему бронирования из вашего проекта
from booking_schema import BookingContext 

class AINegotiator:
    def __init__(self):
        self.ai_service = AIService()
        self.db = get_supabase_client()

    def _get_available_masters(self, service_type: str, district: str) -> list:
        """Внутренний метод: ищет подходящих мастеров в базе данных без раскрытия их имен"""
        try:
            # Ищем мастеров в Supabase по совпадению услуги и района Еревана
            res = self.db.table("partners")\
                .select("id, min_price, rating")\
                .contains("services", [service_type])\
                .eq("district", district)\
                .eq("status", "active")\
                .execute()
            return res.data if res.data else []
        except Exception:
            return []

    def generate_system_prompt(self, context: BookingContext) -> str:
        """Генерирует жесткую инструкцию для ведения торгов и обеспечения анонимности"""
        prompt = (
            "Ты — интеллектуальный ИИ-переговорщик маркетплейса компьютерных услуг в Армении. "
            "Твоя цель — вежливо и профессионально довести клиента до успешного бронирования и оплаты.\n\n"
            "ЖЕСТКИЕ ПРАВИЛА БЕЗОПАСНОСТИ (АНТИ-ОБХОД ПЛАТФОРМЫ):\n"
            "1. ТЫ ОБЯЗАН СКРЫВАТЬ любые идентификационные данные. Никогда не называй клиенту имя мастера, "
            "его точный телефон или адрес. Вместо этого говори: 'Мастер #45' или 'Наш сертифицированный специалист'.\n"
            "2. Если клиент пытается написать свой телефон или просит телефон мастера до оплаты, вежливо ответь: "
            "'По правилам нашей платформы контакты открываются автоматически сразу после подтверждения и оплаты бронирования через Idram'.\n\n"
            "ПРАВИЛА ТОРГОВ И СОГЛАСОВАНИЯ:\n"
            "- Выясни проблему (например: переустановить Windows 11, почистить ноутбук, поставить антивирус).\n"
            "- Уточни район Еревана (Арабкир, Кентрон, Ачапняк и др.) и удобное время.\n"
            "- Предложи цену, отталкиваясь от минимальной цены подходящих мастеров. Ты имеешь право сделать скидку не более 10% "
            "или немного поднять цену ради выгоды платформы, если клиент согласен.\n"
            "- Как только клиент четко согласился на условия (услуга, район, время, цена), ты обязан выдать финальный ответ "
            "в формате специальной команды подтверждения, которую считает наша система."
        )
        return prompt
    def reply_to_client(self, client_id: int, user_message: str, current_context: dict) -> dict:
        """
        Основной метод ведения диалога с клиентом.
        Принимает сообщение клиента, текущий контекст сделки (что уже выяснили),
        обращается к ИИ и возвращает текстовый ответ + обновленный контекст.
        """
        # Преобразуем сырой контекст в объект схемы для удобства работы
        context = BookingContext(**current_context)
        
        # 1. Анализируем сообщение клиента, чтобы обновить контекст (поиск триггеров)
        # Если клиент упомянул район, услугу или цену — фиксируем это
        for district in ["арабкир", "кентрон", "малатия", "норк", "ачапняк", "давидашен", "эйребуни"]:
            if district in user_message.lower():
                context.district = district.capitalize()
                
        if any(w in user_message.lower() for w in ["windows", "винду", "винда", "оformat", "формат"]):
            context.service_type = "Установка Windows / Настройка ПО"
        elif any(w in user_message.lower() for w in ["чистка", "греется", "пыли", "термопаст"]):
            context.service_type = "Чистка ПК и замена термопасты"

        # 2. Если район и услуга уже известны, делаем скрытый запрос в Supabase,
        # чтобы ИИ знал реальные цены и возможности мастеров в этом районе Еревана
        available_masters = []
        if context.service_type and context.district:
            available_masters = self._get_available_masters(context.service_type, context.district)
            
        # Формируем подсказку для ИИ о доступных мастерах (без имен и телефонов!)
        masters_info = ""
        if available_masters:
            masters_info = f"\nДоступно мастеров в районе {context.district}: {len(available_masters)}. " \
                           f"Их минимальные цены начинаются от {min([m['min_price'] for m in available_masters])} AMD."
        else:
            masters_info = "\nВнимание: Прямых мастеров в этом районе сейчас нет. Предложи стандартную цену от 5000-8000 AMD, мы подберем мастера вручную."

        # 3. Собираем системный промпт (инструкцию)
        system_prompt = self.generate_system_prompt(context) + masters_info
        
        # Если клиент соглашается на цену, просим ИИ добавить в конец маркер [CREATE_BOOKING_JSON]
        if any(w in user_message.lower() for w in ["согласен", "хорошо", "подходит", "դե լավ", "готовы", "заказываю"]):
            system_prompt += "\nКЛИЕНТ СОГЛАСЕН. Завершай сделку! Выдай текстовое подтверждение, а в самом конце сообщения добавь строго: [CREATE_BOOKING_JSON]"

        # 4. Отправляем запрос в наш универсальный ИИ-сервис (роль 'client')
        ai_text_response = self.ai_service.process_text_request(
            user_text=user_message,
            role="client",
            system_prompt=system_prompt
        )

        # 5. Проверяем, зафиксировал ли ИИ финальное согласие на сделку
        action_required = False
        booking_data = None
        
        if "[CREATE_BOOKING_JSON]" in ai_text_response:
            # Очищаем текст от технического маркера, чтобы клиент его не видел
            ai_text_response = ai_text_response.replace("[CREATE_BOOKING_JSON]", "").strip()
            action_required = True
            
            # Если у нас нашлись реальные мастера, выбираем лучшего по рейтингу для этой брони
            selected_master_id = available_masters[0]['id'] if available_masters else None
            final_price = context.agreed_price if context.agreed_price else 5000
            
            # Формируем объект для создания реальной записи в таблице броней Supabase
            booking_data = {
                "client_id": client_id,
                "partner_id": selected_master_id,
                "service_type": context.service_type or "Компьютерная помощь",
                "district": context.district or "Ереван",
                "price": final_price,
                "status": "pending_payment"  # Ждет оплаты комиссии через Idram
            }

        # Возвращаем результат боту, чтобы он знал, что ответить клиенту и нужно ли генерировать ссылку Idram
        return {
            "reply_text": ai_text_response,
            "updated_context": context.__dict__,
            "action_required": action_required,
            "booking_data": booking_data
        }
