# Фаза 3 — детальный план реализации

**Проект:** Armenia AI Guide (aiogram + aiohttp + PostgreSQL + Groq + Idram)
**Тон бота:** русский, неформальный
**Модули фазы:** Премиум-контакт · Отмены · Отзывы · Поддержка · Админ-статистика

---

## 0. Что уже есть в проекте (переиспользуем, не пишем заново)

| Инфраструктура | Где живёт | Статус для Фазы 3 |
|---|---|---|
| `contact_disclosures`, `partner_payouts`, `booking_cancellations`, `partner_financial_ledger` | `booking_schema.py` | таблицы созданы, логики поверх них нет |
| `partners.premium_contact_enabled / premium_contact_fee / premium_disclosure_scope / cancellation_policy` | `database.py` | колонки есть, не задействованы |
| `partners.contact_share_policy` (`after_booking`/`premium`/`never`) | `platform_schema.py` | читается в поиске, платной выдачи нет |
| `IdramProvider` (`create_invoice`, `verify_callback`, test-режим) | `idram.py` | готов, переиспользуем |
| `notify()` (Telegram + запись в `notifications`) | `notify.py` | готов |
| `AIRouter` / `AINegotiator` / `GroqAI` | Фаза 2 | готов, расширяем интентами |
| `bookings`, `payments`, `booking_checkins` | `booking_schema.py` | готовы |

**Нужно создать с нуля:** таблицы `reviews`, `support_tickets`, `support_ticket_messages`; view/запросы для админ-статистики.

---

## 1. Премиум-контакт (платная выдача контактов)

**Цель:** клиент может получить контакты партнёра до брони, если у партнёра `contact_share_policy='premium'`, оплатив `premium_contact_fee` через Idram.

**БД:** используем существующую `contact_disclosures` (добавить при необходимости колонки `payment_id`, `fee_amount`, `scope`, `status` через безопасную миграцию `ADD COLUMN IF NOT EXISTS`).

**Логика (новый файл `premium_contact_api.py`):**
1. `POST /api/market/client/service/{service_id}/contact/quote` → вернуть политику и сумму (`premium`/`after_booking`/`never`, `fee`, валюта).
2. `POST /api/market/client/service/{service_id}/contact/unlock` →
   - если `never` → 403; если `after_booking` → проверить наличие оплаченной брони, отдать контакт бесплатно;
   - если `premium` → создать invoice на `premium_contact_fee` через `IdramProvider`, записать `payments(payment_type='premium_contact')` + `contact_disclosures`, начислить в `partner_financial_ledger`, выдать контакт по `premium_disclosure_scope` (phone/website/telegram);
   - идемпотентность: если контакт уже раскрыт этому клиенту — вернуть его без повторной оплаты.
3. `notify()` партнёру: «клиент разблокировал контакт».

**Тесты:** три ветки политики; повторный unlock не берёт деньги дважды; test-Idram отдаёт `TEST-IDRAM-*`.

---

## 2. Отмены (клиент/партнёр) + политика возврата

**Цель:** отмена брони/сделки с учётом `cancellation_policy` партнёра и корректным возвратом/списанием комиссии.

**БД:** существующая `booking_cancellations` (кто, причина, сумма возврата, штраф). Статусы уже есть: `service_requests.status='cancelled'`, `negotiations.status='cancelled'`, `bookings.status` (добавить `'cancelled'`/`'refunded'` через миграцию значения).

**Логика (расширяем `marketplace_flow_api.py`, отдельные хендлеры):**
1. `POST /api/market/client/booking/{booking_id}/cancel` и `.../partner/booking/{booking_id}/cancel`.
2. Движок политики `cancellation_policy` (напр. `flexible`/`moderate`/`strict`): считает % возврата по времени до `scheduled_at`.
3. Проводки: запись в `booking_cancellations`, сторно/частичный возврат в `payments` (`status='refunded'`/`partial_refund`), корректировка `partner_financial_ledger`.
4. Обновить `bookings.status`, `service_requests.status`, при активной сделке — `negotiations.status='cancelled'`.
5. `notify()` второй стороне.

**Тесты:** flexible→100%, strict→0%, промежуточный %, отмена уже отменённой → 409, права (чужую бронь не отменить).

---

## 3. Отзывы и рейтинги

**Цель:** после завершённой брони клиент оставляет оценку 1–5 + текст; у партнёра/услуги считается агрегированный рейтинг.

**БД (новая таблица в `platform_schema.py`):**
```sql
CREATE TABLE IF NOT EXISTS reviews (
    id BIGSERIAL PRIMARY KEY,
    booking_id BIGINT REFERENCES bookings(id) ON DELETE SET NULL,
    client_id  BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    service_id BIGINT REFERENCES services(id) ON DELETE SET NULL,
    rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment TEXT NOT NULL DEFAULT '',
    partner_reply TEXT,
    status TEXT NOT NULL DEFAULT 'published'
        CHECK (status IN ('published','hidden','flagged')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(booking_id, client_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_partner ON reviews(partner_id, status);
```

**Логика (новый файл `reviews_api.py`):**
1. `POST /api/market/client/booking/{booking_id}/review` — только для брони со `status='completed'`, один отзыв на бронь.
2. `POST /api/market/partner/review/{review_id}/reply` — ответ партнёра.
3. `GET /api/market/partner/{partner_id}/reviews` — список + агрегаты (`AVG(rating)`, кол-во).
4. AI-модерация комментария через `GroqAI` (флаг токсичности → `status='flagged'` на ревью админом), с fallback в mock-режиме.
5. Рейтинг подмешать в ранжирование поиска (бонус к `rank_score`).

**Тесты:** отзыв без завершённой брони → 400; дубликат → 409; агрегат считается; mock-модерация не падает.

---

## 4. Поддержка (тикеты) с AI-ассистентом

**Цель:** пользователь открывает тикет; AI отвечает первой линией, эскалация к админу при необходимости.

**БД (новые таблицы в `platform_schema.py`):**
```sql
CREATE TABLE IF NOT EXISTS support_tickets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    audience TEXT NOT NULL DEFAULT 'client' CHECK (audience IN ('client','partner')),
    subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','ai_answered','escalated','resolved','closed')),
    priority TEXT NOT NULL DEFAULT 'normal',
    data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS support_ticket_messages (
    id BIGSERIAL PRIMARY KEY,
    ticket_id BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender_role TEXT NOT NULL CHECK (sender_role IN ('user','ai','admin')),
    sender_id BIGINT,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status, updated_at DESC);
```

**Логика (новый файл `support_api.py`):**
1. `POST /api/support/ticket` — создать тикет + первое сообщение.
2. `POST /api/support/ticket/{id}/message` — сообщение пользователя; AI (через `GroqAI`) генерит ответ, статус → `ai_answered`; если AI неуверен/просят человека → `escalated` + `notify()` админу.
3. `GET /api/support/ticket/{id}` — история; `GET /api/support/tickets` — список пользователя.
4. `POST /api/admin/support/ticket/{id}/reply` и `.../resolve` — админ-ответ и закрытие.
5. Переиспользуем shared-memory (`ai_sessions`/`ai_messages`) для контекста тикета.

**Тесты:** создание тикета; AI-ответ в mock; эскалация ставит статус+уведомление; закрытие тикета.

---

## 5. Админ-статистика

**Цель:** сводные метрики платформы для админа.

**Метрики (SQL-агрегаты, без новых таблиц):**
- Заявки: по статусам (`service_requests`), конверсия discovery→booked.
- Сделки: активные/agreed/cancelled (`negotiations`).
- Брони и GMV: сумма `bookings.agreed_price`, комиссия (`payments` type=commission), выручка премиум-контактов.
- Партнёры: всего/approved/по статусам; топ по броням и рейтингу.
- Отзывы: средний рейтинг, кол-во flagged.
- Поддержка: открытые/эскалированные тикеты, среднее время ответа.
- Возвраты: сумма и кол-во по `booking_cancellations`.

**Логика (расширяем `admin_ai_api.py` или новый `admin_stats_api.py`):**
1. `GET /api/admin/stats/overview` — все ключевые числа одним ответом (защита через `admin_access`).
2. `GET /api/admin/stats/timeseries?metric=bookings&period=30d` — ряды для графиков.
3. `GET /api/admin/stats/top-partners`.
4. Кэш в памяти на 60 сек (метрики тяжёлые), фича-флаг.
5. Фронт: карточки в `web_apps/admin_v2.html` (по возможности переиспользовать существующий UI).

**Тесты:** эндпоинты требуют админ-доступ (403 без него); агрегаты не падают на пустых таблицах; числа сходятся на фикстурах.

---

## 6. Общие правила фазы

- **Миграции безопасные:** только `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, никакого DROP.
- **Idram:** всё через `IdramProvider`; test-режим (`gsk/idram test`) → `TEST-*`, продакшн-ключи включают live без правки кода.
- **AI:** новые интенты (`review`, `support`, `cancel`, `contact_unlock`) добавить в `AIRouter`; в mock-режиме — детерминированный fallback, как в Фазе 2.
- **Уведомления:** каждое значимое событие → `notify()` (best-effort, не валит основной поток).
- **Регистрация роутов:** `register_*_routes(app)`, проверка на отсутствие дублей в `harness.py`.
- **Тесты:** `test_phase3_core.py` (юнит-логика без БД) + `test_phase3_endpoints.py` (smoke, mock-DB, без 500).
- **Ограничения песочницы:** нет живого Postgres/Groq/Telegram/Idram — сквозные оплаты и live-AI проверяются моками; в отчёте честно помечаем непроверяемое.

---

## 7. Порядок работ (предлагаемый)

1. Миграции БД: `reviews`, `support_tickets`, `support_ticket_messages` + `ADD COLUMN` для отмен/премиума.
2. Премиум-контакт (переиспользует готовую платёжную обвязку — быстрый первый результат).
3. Отмены + политика возврата.
4. Отзывы + подмешивание рейтинга в поиск.
5. Поддержка (тикеты + AI).
6. Админ-статистика + карточки в admin UI.
7. Тесты `test_phase3_*`, прогон, сборка zip → `outputs`, короткий русский отчёт.

**Оценка объёма:** ~4 новых файла (`premium_contact_api`, `reviews_api`, `support_api`, `admin_stats_api`), правки в `platform_schema.py`, `marketplace_flow_api.py`, `ai_router.py`, `ai_service.py`, `harness.py` + 2 тест-файла.
