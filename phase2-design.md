# Фаза 2 — AI-ядро (AI Core)

Цель фазы: превратить наивную переписку в маркетплейсе в единое AI-ядро,
которое (1) маршрутизирует сообщение в нужный модуль, (2) ведёт торг через
структурированное понимание намерения и цены (а не по ключевым словам) и
(3) делит общую память между модулями через `ai_sessions` / `ai_messages`.

## Компоненты

### 1. `ai_service.py` — расширение GroqAI
Добавлены два структурированных метода поверх существующего `_call_groq`
(полностью совместимы со старым интерфейсом, `negotiate_reply` сохранён):

- `route_message(text, role, context) -> dict`
  Классифицирует свободный текст в один из модулей ядра:
  `client_search | negotiation | partner_onboarding | smalltalk`.
  Возвращает `{module, confidence, reason, language}`.
  Mock/фолбэк: детерминированная эвристика по контексту и языку.

- `negotiate(payload) -> dict`
  Понимает реплику торга. Вход: услуга, текущая цена, валюта, история,
  роль отправителя, новое сообщение. Выход:
  `{intent, proposed_price, agreed, reply, summary}`
  где `intent ∈ {agree, counter, reject, question, chitchat}`.
  Mock/фолбэк: извлечение цены регуляркой + намерения по маркерам на
  трёх языках (hy/ru/en). Это фолбэк ТОЛЬКО когда нет живого ключа Groq;
  при живом ключе намерение и цену определяет модель.

### 2. `ai_negotiator.py` — модуль торга (новый)
`AINegotiator.handle(nid, sender_role, sender_id, text, db_hooks)`:
- пишет сообщение в `negotiation_messages`;
- зеркалит его в общую память `ai_sessions(session_type='negotiation')`;
- вызывает `ai.negotiate(...)` со всем контекстом торга;
- обновляет `state_json` (client_agreed/partner_agreed/last_price/
  proposed_price/history_summary) на основе структурированного намерения;
- при обоюдном согласии переводит negotiation → `agreed`,
  service_request → `confirmed`;
- возвращает нейтральную реплику-медиатора и обновлённое состояние.

Ключевое отличие от Фазы 1: раньше `negotiation_client_message`
сравнивал текст со списком слов («согласен/беру/agree…»), а `partner_reply`
просто пересылал текст. Теперь оба пути идут через один AI-медиатор и общую
логику согласия/цены.

### 3. `ai_router.py` — оркестратор ядра (новый)
`AIRouter.dispatch(user_id, role, text, lang)`:
- ведёт общий «оркестраторный» слой памяти в `ai_sessions`
  (`role='client'|'partner'`, `session_type='orchestrator'`);
- через `ai.route_message` решает модуль и делегирует:
  `client_search → ClientAI`, `partner_onboarding → PartnerAI`;
- логирует решение маршрутизации в общую память, чтобы модули видели
  историю намерений пользователя;
- возвращает `{module, reply, ...}`.

Торг маршрутизируется не через свободный чат, а по явному id переговоров
(эндпоинты `/api/market/...`), поэтому оркестратор для торга выдаёт
подсказку-ссылку, а фактическую реплику формирует `AINegotiator`.

### 4. Общая память
`ai_sessions` + `ai_messages` уже существуют. Используемые срезы:
- `orchestrator` — намерения/маршрутизация пользователя;
- `sales` — клиентский поиск (ClientAI);
- `onboarding` — партнёрский онбординг (PartnerAI);
- `negotiation` — переписка торга, зеркалируется из negotiation_messages.
Все модули читают/пишут через хелперы `platform_db`
(`create_session/active_session/add_ai_message/recent_ai_messages/update_session`).

### 5. Правки существующих файлов
- `marketplace_flow_api.py`: `negotiation_client_message` и `partner_reply`
  переведены на `AINegotiator` (структурированное намерение + цена).
- `client_ai.py`: усилён clarify-цикл — отслеживаются заданные вопросы,
  не повторяются, стадия двигается discovery → clarifying → options_found.
- `client_api.py`: `/api/client/ai/chat` теперь может идти через оркестратор
  (обратная совместимость сохранена, ClientAI по-прежнему точка входа).

## Проверка (честно об ограничениях)
- `py_compile` всех модулей.
- Harness сборки app (mock psycopg + Telegram) — отсутствие дублей маршрутов.
- Смоук эндпоинтов — отсутствие 500.
- Юнит торга/маршрутизатора на mock-GroqAy (keyless) — намерение и цена
  извлекаются детерминированно.
- Ограничения песочницы: БД замокана (запросы возвращают пустые строки),
  живых ключей Telegram/Groq нет — сквозной торг с реальными строками БД и
  реальной моделью здесь не воспроизводится; проверяется логика/контракты.
