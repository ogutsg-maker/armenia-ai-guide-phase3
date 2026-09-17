import json
from aiohttp import web
from ai_service import AIService
from database import get_supabase_client
from telegram_webapp_auth import validate_telegram_webapp_init_data, TelegramWebAppAuthError

def _auth_partner(request):
    """Внутренний метод: проверяет авторизацию мастера через Telegram WebApp"""
    raw = request.headers.get('X-Telegram-Init-Data', '').strip()
    token = request.app.get('stage3_bot_token', '')
    if not raw: 
        raise web.HTTPUnauthorized(text=json.dumps({'ok': False, 'error': 'auth_required'}), content_type='application/json')
    try: 
        user_data = validate_telegram_webapp_init_data(raw, token)
        uid = int(user_data['id'])
        return uid
    except Exception as e: 
        raise web.HTTPUnauthorized(text=json.dumps({'ok': False, 'error': str(e)}), content_type='application/json')

async def update_profile_by_image(request):
    """
    Эндпоинт: Мастер загружает фото нового бумажного прайса.
    ИИ сканирует изображение, извлекает услуги/цены и обновляет профиль в Supabase.
    """
    telegram_id = _auth_partner(request)
    data = await request.json()
    image_url = str(data.get('image_url') or '').strip()
    
    if not image_url:
        return web.json_response({'ok': False, 'error': 'image_url_required'}, status=400)
    
    ai_service = AIService()
    db = get_supabase_client()
    
    # 1. Запускаем компьютерное зрение ИИ для распознавания текста на картинке
    extracted_text = ai_service.process_image_price(image_url)
    
    if "🔒" in extracted_text:  # Если админ отключил рубильник картинок
        return web.json_response({'ok': False, 'error': 'image_processing_disabled', 'message': extracted_text}, status=403)
        
    # 2. Просим ИИ превратить распознанный текст в чистый структурированный JSON-массив услуг
    struct_prompt = (
        "Преврати этот текст прайс-листа в плоский массив строк JSON. "
        "Каждая строка должна содержать название услуги и цену в AMD, например: ['Установка Windows - 6000 AMD', 'Чистка - 5000 AMD']. "
        "Верни СТРОГО чистый JSON-массив без каких-либо вступлений и markdown разметки."
    )
    
    ai_json_response = ai_service.process_text_request(
        user_text=extracted_text,
        role="partner",
        system_prompt=struct_prompt
    )
    
    try:
        clean_json = ai_json_response.replace("```json", "").replace("```", "").strip()
        parsed_services = json.loads(clean_json)
        
        # 3. Обновляем поле услуг (services) у мастера в базе данных Supabase
        db.table("partners").update({"services": parsed_services}).eq("telegram_id", telegram_id).execute()
        return web.json_response({'ok': True, 'services': parsed_services, 'raw_analysis': extracted_text})
    except Exception as e:
        return web.json_response({'ok': False, 'error': 'parsing_failed', 'details': str(e), 'ai_raw': ai_json_response}, status=500)
async def update_profile_by_voice(request):
    """
    Эндпоинт: Мастер наговаривает голосовую команду (аудиофайл).
    ИИ переводит аудио в текст, понимает суть изменений и точечно правит профиль в Supabase.
    """
    telegram_id = _auth_partner(request)
    data = await request.json()
    audio_url = str(data.get('audio_url') or '').strip()
    
    # В реальной системе это может быть локальный путь к файлу, скачанному из телеграма
    audio_file_path = str(data.get('file_path') or '').strip() 
    
    if not audio_file_path and not audio_url:
        return web.json_response({'ok': False, 'error': 'audio_source_required'}, status=400)
        
    ai_service = AIService()
    db = get_supabase_client()
    
    # 1. Расшифровываем голос в текст через встроенный ИИ-сервис
    # Если на сервере сохранен файл, передаем его путь
    voice_text = ai_service.process_voice(audio_file_path)
    
    if "🔒" in voice_text:  # Если админ отключил рубильник голоса в админке
        return web.json_response({'ok': False, 'error': 'voice_processing_disabled', 'message': voice_text}, status=403)

    # 2. Просим ИИ понять, какие ИМЕННО параметры мастер хочет изменить в своей анкете
    interpretation_prompt = (
        "Ты — ИИ-ассистент личного кабинета мастера. Твоя задача — проанализировать расшифровку голоса мастера "
        "и понять, какие точечные изменения он хочет внести в свой рабочий профиль в Армении.\n\n"
        "Выведи результат СТРОГО в формате JSON со следующими полями. Если параметр в речи не упоминается, "
        "обязательно оставь для него значение null (чтобы не затереть старые данные в базе!):\n"
        "- min_price (новое число цены в AMD, если мастер говорит о повышении/понижении цен)\n"
        "- working_hours (новый график, например '09:00-19:00' или 'выходной в воскресенье', если мастер меняет время)\n"
        "- on_site (изменение статуса выезда к клиенту, true — если начал выезжать, false — если теперь работает только у себя)\n"
        "- status (если мастер говорит 'я уезжаю', 'снимите с публикации', 'не работаю' — выведи 'inactive', если 'я готов работать' — 'active')\n\n"
        "Не пиши никаких вступлений, пояснений и markdown-оберток. Только чистый JSON."
    )
    
    ai_analysis_json = ai_service.process_text_request(
        user_text=voice_text,
        role="partner",
        system_prompt=interpretation_prompt
    )
    
    try:
        clean_json = ai_analysis_json.replace("```json", "").replace("```", "").strip()
        changes = json.loads(clean_json)
        
        # Сборка только тех полей, которые мастер реально попросил изменить голосом
        update_payload = {k: v for k, v in changes.items() if v is not None}
        
        if not update_payload:
            return web.json_response({
                'ok': True, 
                'message': 'ИИ не обнаружил конкретных команд для изменения профиля', 
                'transcript': voice_text
            })
            
        # 3. Точечно обновляем анкету мастера в Supabase, сохраняя все остальные поля нетронутыми
        db.table("partners").update(update_payload).eq("telegram_id", telegram_id).execute()
        
        return web.json_response({
            'ok': True, 
            'applied_changes': update_payload, 
            'transcript': voice_text
        })
        
    except Exception as e:
        return web.json_response({
            'ok': False, 
            'error': 'voice_interpretation_failed', 
            'details': str(e), 
            'ai_raw': ai_analysis_json
        }, status=500)


def register_master_cabinet_routes(app, ai):
    """Регистрация ИИ-маршрутов личного кабинета мастера в общем приложении"""
    app['ai'] = ai
    app.router.add_post('/api/partner/cabinet/update-by-image', update_profile_by_image)
    app.router.add_post('/api/partner/cabinet/update-by-voice', update_profile_by_voice)
