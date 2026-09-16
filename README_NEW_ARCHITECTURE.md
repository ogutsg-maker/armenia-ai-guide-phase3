# Armenia AI Guide — новая архитектура

Это новая ветка архитектуры на базе текущего `marketplace-bot` и последних исправлений направления/документов.

## Что сохранено
- aiogram + aiohttp;
- PostgreSQL / Supabase;
- Groq + OpenAI Whisper;
- Idram/legacy payment flow;
- старые `users`, `orders`, `bids`, `deals`, `reviews`, `disputes` для обратной совместимости;
- существующий каталог `master_categories -> categories`;
- независимые `partner_directions` и документы по направлениям.

## Что изменено
### Партнёр
Партнёр больше не обязан проходить длинную форму. После выбора роли запускается `PartnerAI`:

`разговор → структурирование → подтверждение → направление → документ → admin review`.

AI хранит состояние в PostgreSQL (`ai_sessions`, `ai_messages`), поэтому диалог не зависит только от памяти Telegram FSM.

### AI-каталог
Если подходящего элемента каталога нет, AI создаёт `ai_catalog_proposals`.
Админ имеет три основных действия:

- `✓ Активировать`
- `✎ Изменить`
- `↩ Вернуть на уточнение`

При возврате комментарий отправляется партнёру через Telegram. Ответ партнёра возвращается в ту же AI-сессию.

### Потенциальные партнёры
Отдельный контур админки:

`AI Research → реальный источник → структурирование → потенциальный партнёр → контакт → приглашение → регистрация → обычный Partner AI onboarding`.

Фейковые партнёры не создаются. Без `SERPER_API_KEY` кнопка реального веб-поиска сообщает, что поисковый провайдер не настроен.

### Клиент
Уже заложено постоянное клиентское AI-состояние:

`Client AI → service_requests → request_candidates → negotiations`.

Добавлен базовый `client.html` и `/api/client/ai/chat`. Полный клиентский кабинет, переговоры, Premium Contact и финальный booking flow будут расширяться поверх этой модели, а не менять её.

## Новые таблицы
- `partners`
- `partner_locations`
- `services`
- `service_packages`
- `service_options`
- `service_schedule`
- `ai_sessions`
- `ai_messages`
- `ai_catalog_proposals`
- `catalog_subcategories`
- `potential_partners`
- `potential_partner_sources`
- `admin_clarifications`
- `client_profiles`
- `service_requests`
- `request_candidates`
- `negotiations`
- `negotiation_messages`

Миграция не удаляет старые данные.

## Важное правило
AI никогда сам не публикует новую структуру каталога и не утверждает партнёра. AI только готовит структурированные данные/предложения. Решение принимает администратор.

## Локальный запуск
1. Создать `.env` из `.env.example`.
2. Заполнить Telegram, PostgreSQL/Supabase и Groq.
3. Для реального поиска потенциальных партнёров дополнительно заполнить:
   - `SEARCH_PROVIDER=serper`
   - `SERPER_API_KEY=...`
4. Установить зависимости:
   `python -m pip install -r requirements.txt`
5. Запустить:
   `python main.py`

Сервер использует `PORT` из окружения, по умолчанию `8000`.
