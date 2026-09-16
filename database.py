"""DatabaseManager — единый слой работы с PostgreSQL (Supabase / любой PostgreSQL)."""
import os
import json
import logging
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier
from config import DATABASE_URL, CITY_SYNONYMS

logger = logging.getLogger(__name__)

def _connection_kwargs() -> dict:
    """Возвращает параметры подключения с автоподстройкой под Supabase."""
    url = DATABASE_URL
    # prepare_threshold=None отключает автоматические prepared statements psycopg3.
    # Это обязательно для Supabase transaction-pooler (порт 6543 / pgbouncer),
    # иначе всплывают ошибки "prepared statement already exists".
    kwargs = {"row_factory": dict_row, "prepare_threshold": None}
    if "supabase" in url or "supabase.co" in url:
        kwargs["sslmode"] = "require"
    elif "sslmode" not in url:
        kwargs["sslmode"] = "prefer"
    return kwargs

def _connect(url: str = None, row_factory=None):
    """Удобная обёртка: автоматически подставляет sslmode и фабрику строк."""
    url = url or DATABASE_URL
    kwargs = {"row_factory": row_factory or dict_row, "prepare_threshold": None}
    if "supabase" in url or "supabase.co" in url:
        kwargs["sslmode"] = "require"
    elif "sslmode" not in url:
        kwargs["sslmode"] = "prefer"
    return psycopg.connect(url, **kwargs)
class DatabaseManager:
    def __init__(self):
        self.db_url = DATABASE_URL
        self.init_db()

    def init_db(self):
        """Создание таблиц маркетплейса при первом запуске."""
        try:
            with _connect() as conn:
                with conn.cursor() as cur:
                    # 1. Таблица пользователей
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS users (
                            telegram_id  BIGINT PRIMARY KEY,
                            username     TEXT,
                            full_name    TEXT,
                            role         TEXT DEFAULT NULL CHECK (role IN ('client','master',NULL)),
                            lang         TEXT DEFAULT 'hy',
                            city         TEXT DEFAULT NULL,
                            phone        TEXT DEFAULT NULL,
                            passport_photo TEXT DEFAULT NULL,
                            is_verified BOOLEAN DEFAULT FALSE,
                            is_frozen    BOOLEAN DEFAULT FALSE,
                            balance      NUMERIC DEFAULT 0,
                            rating_avg   NUMERIC DEFAULT NULL,
                            rating_count INT DEFAULT 0,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 2. Главные родительские категории (сферы)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS master_categories (
                            id           SERIAL PRIMARY KEY,
                            name_am      TEXT NOT NULL,
                            name_ru      TEXT NOT NULL,
                            slug         TEXT NOT NULL UNIQUE,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # 3. Подкатегории услуг (конкретные услуги)
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS categories (
                            id           SERIAL PRIMARY KEY,
                            master_category_id INT REFERENCES master_categories(id) ON DELETE CASCADE,
                            name_am      TEXT NOT NULL,
                            name_ru      TEXT NOT NULL,
                            slug         TEXT NOT NULL UNIQUE,
                            is_active    BOOLEAN DEFAULT TRUE,
                            commission_type  TEXT NOT NULL DEFAULT 'on_top'
                                CHECK (commission_type IN ('inside','on_top','fixed')),
                            commission_value NUMERIC NOT NULL DEFAULT 10,
                            created_at   TIMESTAMPTZ DEFAULT NOW()
                        )
                    ''')
                    # Partner specialisations (M:N user <-> categories). Kept
                    # because partner onboarding stores AI-detected categories
                    # here via set_master_categories().
                    cur.execute('''
                        CREATE TABLE IF NOT EXISTS master_skills (
                            id          SERIAL PRIMARY KEY,
                            user_id     BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                            category_id INT REFERENCES categories(id) ON DELETE CASCADE,
                            description TEXT DEFAULT '',
                            is_active   BOOLEAN DEFAULT TRUE,
                            created_at  TIMESTAMPTZ DEFAULT NOW(),
                            UNIQUE(user_id, category_id)
                        )
                    ''')
                    # Legacy contour tables (orders, deals, chat_messages,
                    # bids, reviews, disputes) were removed as part of the clean
                    # Armenia AI Guide architecture. The new catalogue / booking
                    # / notification tables are created by
                    # platform_schema.ensure_platform_schema().
                    conn.commit()
            logger.info("✅ База данных успешно инициализирована.")
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            raise
    # ------------------------------------------------------------------
    # ПОЛЬЗОВАТЕЛИ
    # ------------------------------------------------------------------
    def register_user(self, telegram_id: int, username: str, full_name: str = None):
        """Регистрация нового пользователя или обновление юзернейма."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO users (telegram_id, username, full_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        full_name = COALESCE(EXCLUDED.full_name, users.full_name)
                ''', (telegram_id, username, full_name))
                conn.commit()

    def get_user(self, telegram_id: int) -> dict | None:
        """Получение профиля пользователя по его Telegram ID."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
                return cur.fetchone()

    def update_user_field(self, telegram_id: int, field: str, value):
        """Обновление любого выбранного поля пользователя в базе (безопасный динамический SQL)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    SQL("UPDATE users SET {} = %s WHERE telegram_id = %s").format(Identifier(field)),
                    (value, telegram_id)
                )
                conn.commit()

    # ------------------------------------------------------------------
    # КАТЕГОРИИ УСЛУГ (ДВУХУРОВНЕВАЯ СТРУКТУРА)
    # ------------------------------------------------------------------
    def get_all_master_categories(self) -> list[dict]:
        """Возвращает список всех главных родительских категорий (сфер)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name_am, name_ru, slug FROM master_categories ORDER BY id")
                return cur.fetchall()

    def get_subcategories_by_master(self, master_category_id: int) -> list[dict]:
        """Возвращает список активных подкатегорий (услуг) для конкретной сферы."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, master_category_id, name_am, name_ru, slug, commission_type, commission_value 
                    FROM categories 
                    WHERE master_category_id = %s AND is_active = TRUE 
                    ORDER BY id
                ''', (master_category_id,))
                return cur.fetchall()

    def get_all_categories(self) -> list[dict]:
        """Возвращает все подкатегории вместе с именами их родительских сфер для админки."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT 
                        c.id, 
                        c.master_category_id, 
                        c.name_am as name_hy, 
                        c.name_ru, 
                        c.slug, 
                        c.commission_type, 
                        c.commission_value,
                        c.is_active,
                        m.name_ru as master_name_ru,
                        m.name_am as master_name_am
                    FROM categories c
                    JOIN master_categories m ON c.master_category_id = m.id
                    ORDER BY m.id, c.id
                ''')
                return cur.fetchall()

    def get_active_categories(self) -> list[dict]:
        """Возвращает только активные подкатегории (алиас для обратной совместимости с ai_dispatcher)."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT 
                        c.id, 
                        c.master_category_id, 
                        c.name_am as name_hy, 
                        c.name_ru, 
                        c.slug, 
                        c.commission_type, 
                        c.commission_value,
                        c.is_active,
                        m.name_ru as master_name_ru,
                        m.name_am as master_name_am
                    FROM categories c
                    JOIN master_categories m ON c.master_category_id = m.id
                    WHERE c.is_active = TRUE 
                    ORDER BY m.id, c.id
                ''')
                return cur.fetchall()

    def get_category_by_name(self, name_to_find: str) -> dict | None:
        """Ищет подкатегорию по названию или по slug."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id, master_category_id, name_am as name_hy, name_ru, slug, commission_type, commission_value 
                    FROM categories 
                    WHERE name_am = %s OR name_ru = %s OR slug = %s
                ''', (name_to_find, name_to_find, name_to_find))
                return cur.fetchone()

    def update_category(self, cat_id: int, **kwargs):
        """Обновление полей подкатегории (комиссия, активность и т.д.)."""
        if not kwargs:
            return
        sets = []
        vals = []
        for k, v in kwargs.items():
            sets.append(f"{k} = %s")
            vals.append(v)
        vals.append(cat_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE categories SET {', '.join(sets)} WHERE id = %s",
                    vals,
                )
                conn.commit()

    def update_category_settings(self, cat_id: int, **kwargs):
        """Обновляет настройки подкатегории в categories + category_settings."""
        allowed = {
            "is_active", "commission_type", "commission_value",
            "bank_commission_type", "bank_commission_value",
            "cancellation_policy", "premium_contact_enabled",
            "premium_contact_fee", "premium_disclosure_scope",
            "contact_reveal_after_booking",
        }
        data = {k: v for k, v in kwargs.items() if k in allowed}
        if not data:
            return
        with _connect() as conn:
            with conn.cursor() as cur:
                # Основные параметры подкатегории.
                cat_fields = {k: data[k] for k in ("is_active", "commission_type", "commission_value") if k in data}
                if cat_fields:
                    sets = [f"{k} = %s" for k in cat_fields]
                    vals = list(cat_fields.values()) + [cat_id]
                    cur.execute(f"UPDATE categories SET {', '.join(sets)} WHERE id = %s", vals)

                # Дополнительные коммерческие настройки.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS category_settings (
                        category_id INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
                        bank_commission_type TEXT DEFAULT 'none',
                        bank_commission_value NUMERIC DEFAULT 0,
                        cancellation_policy TEXT DEFAULT 'no_refund',
                        premium_contact_enabled BOOLEAN DEFAULT FALSE,
                        premium_contact_fee NUMERIC DEFAULT 0,
                        premium_disclosure_scope TEXT DEFAULT 'none',
                        contact_reveal_after_booking BOOLEAN DEFAULT TRUE
                    )
                """)
                settings_fields = [
                    "bank_commission_type", "bank_commission_value",
                    "cancellation_policy", "premium_contact_enabled",
                    "premium_contact_fee", "premium_disclosure_scope",
                    "contact_reveal_after_booking",
                ]
                cur.execute("""
                    INSERT INTO category_settings (category_id) VALUES (%s)
                    ON CONFLICT (category_id) DO NOTHING
                """, (cat_id,))
                for field in settings_fields:
                    if field in data:
                        cur.execute(f"UPDATE category_settings SET {field} = %s WHERE category_id = %s", (data[field], cat_id))
                conn.commit()

    # ------------------------------------------------------------------
    # АДМИНИСТРАТИВНЫЙ CRUD УПРАВЛЕНИЯ КАТАЛОГОМ
    # ------------------------------------------------------------------
    def create_master_category(self, name_ru: str, name_am: str, slug: str) -> int:
        """Создать новую главную родительскую сферу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO master_categories (name_ru, name_am, slug)
                    VALUES (%s, %s, %s) RETURNING id
                ''', (name_ru, name_am, slug))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def update_master_category(self, mcat_id: int, **kwargs):
        """Редактировать поля существующей главной сферы."""
        if not kwargs:
            return
        sets, vals = [], []
        for k, v in kwargs.items():
            sets.append(f"{k} = %s")
            vals.append(v)
        vals.append(mcat_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE master_categories SET {', '.join(sets)} WHERE id = %s", vals)
                conn.commit()

    def delete_master_category(self, mcat_id: int):
        """Удалить главную сферу. Связанные услуги удалятся автоматически благодаря ON DELETE CASCADE."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM master_categories WHERE id = %s", (mcat_id,))
                conn.commit()

    def create_subcategory(self, master_category_id: int, name_ru: str, name_am: str, slug: str, 
                           commission_type: str = 'on_top', commission_value: float = 10.0) -> int:
        """Создать новую дочернюю услугу внутри выбранной сферы."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO categories (master_category_id, name_ru, name_am, slug, commission_type, commission_value, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE) RETURNING id
                ''', (master_category_id, name_ru, name_am, slug, commission_type, commission_value))
                row = cur.fetchone()
                conn.commit()
                return row["id"] if row else None

    def delete_subcategory(self, cat_id: int):
        """Полное удаление конкретной услуги из каталога."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM categories WHERE id = %s", (cat_id,))
                conn.commit()
    # ------------------------------------------------------------------
    # НАСТРОЙКИ ПРОФИЛЯ ПАРТНЕРА (СВЯЗЬ МАСТЕР ↔ ПОДКАТЕГОРИИ)
    # ------------------------------------------------------------------
    def set_master_categories(self, user_id: int, category_ids: list[int]):
        """Устанавливает специализации мастера в таблице связей master_skills."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM master_skills WHERE user_id = %s", (user_id,))
                for cat_id in category_ids:
                    cur.execute('''
                        INSERT INTO master_skills (user_id, category_id, is_active) 
                        VALUES (%s, %s, TRUE) 
                        ON CONFLICT DO NOTHING
                    ''', (user_id, cat_id))
                conn.commit()

    def get_master_categories(self, user_id: int) -> list[dict]:
        """Возвращает список всех подкатегорий, на которые подписан мастер."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT ms.*, c.name_am as name_hy, c.name_ru, c.slug, c.master_category_id
                    FROM master_skills ms
                    JOIN categories c ON ms.category_id = c.id
                    WHERE ms.user_id = %s AND ms.is_active = TRUE
                    ORDER BY c.id
                ''', (user_id,))
                return cur.fetchall()

    def toggle_master_category(self, user_id: int, category_id: int, is_active: bool = True):
        """Включает или выключает получение уведомлений по конкретному направлению."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    UPDATE master_skills 
                    SET is_active = %s 
                    WHERE user_id = %s AND category_id = %s
                ''', (is_active, user_id, category_id))
                conn.commit()

    # ------------------------------------------------------------------
    # ЗАКАЗЫ И ПОИСК ИСПОЛНИТЕЛЕЙ
    # ------------------------------------------------------------------




    def find_matching_masters(self, category_name_or_slug: str, city: str) -> list[dict]:
        """Ищет верифицированных мастеров по категории и городу."""
        with _connect() as conn:
            with conn.cursor() as cur:
                city_lower = city.strip().lower()
                allowed = CITY_SYNONYMS.get(city_lower, [city_lower])
                cur.execute('''
                    SELECT u.telegram_id, u.username, u.city, u.rating_avg
                    FROM users u
                    JOIN master_skills ms ON u.telegram_id = ms.user_id
                    JOIN categories c ON ms.category_id = c.id
                    WHERE (c.name_am = %s OR c.name_ru = %s OR c.slug = %s)
                      AND u.role = 'master'
                      AND u.is_verified = TRUE
                      AND u.is_frozen = FALSE
                      AND ms.is_active = TRUE
                      AND TRIM(LOWER(u.city)) = ANY(%s)
                ''', (category_name_or_slug, category_name_or_slug, category_name_or_slug, allowed))
                return cur.fetchall()
    # ------------------------------------------------------------------
    # СТАВКИ / ТОРГИ (БИДЫ МАСТЕРОВ)
    # ------------------------------------------------------------------




    # ------------------------------------------------------------------
    # СДЕЛКИ И БЕЗОПАСНАЯ ОПЛАТА (ИНТЕГРАЦИЯ IDRAM)
    # ------------------------------------------------------------------




    # ------------------------------------------------------------------
    # АНОНИМНЫЙ ЧАТ И СИСТЕМА ОТЗЫВОВ
    # ------------------------------------------------------------------



    # ------------------------------------------------------------------
    # АРБИТРАЖ (СПОРЫ) И СТАТИСТИКА ЛИЧНОГО КАБИНЕТА МАСТЕРА
    # ------------------------------------------------------------------




    # ------------------------------------------------------------------
    # ЛЕНТА ЗАЯВОК МАСТЕРА И ФУНКЦИОНАЛ АДМИН-ПАНЕЛИ (ADMIN.HTML)
    # ------------------------------------------------------------------

    def get_all_users(self, limit: int = 100) -> list[dict]:
        """Возвращает список всех пользователей маркетплейса для админ-панели."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT %s", (limit,))
                return cur.fetchall()



    def delete_user(self, telegram_id: int):
        """Полное удаление пользователя из системы администратором."""
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE telegram_id = %s", (telegram_id,))
                conn.commit()


    # ------------------------------------------------------------------
    # NEW AI-FIRST PARTNER CORE (kept here for legacy main.py compatibility)
    # ------------------------------------------------------------------
    def get_partner_by_user(self, user_id: int):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM partners WHERE user_id=%s LIMIT 1", (user_id,))
                return cur.fetchone()

    def create_partner(self, user_id: int) -> int:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO partners(user_id) VALUES(%s) ON CONFLICT(user_id) DO UPDATE SET updated_at=NOW() RETURNING id", (user_id,))
                row=cur.fetchone(); conn.commit(); return row["id"] if row else None

    def update_partner(self, partner_id: int, **kwargs):
        allowed={"business_name","business_description","status","verification_status","rejection_reason","contact_share_policy","profile_json"}
        data={k:v for k,v in kwargs.items() if k in allowed}
        if not data: return self.get_partner_by_id(partner_id)
        sets=[]; vals=[]
        for k,v in data.items():
            if k == "profile_json" and not isinstance(v,str):
                v=json.dumps(v,ensure_ascii=False)
                sets.append(f"{k}=%s::jsonb")
            else:
                sets.append(f"{k}=%s")
            vals.append(v)
        sets.append("updated_at=NOW()")
        vals.append(partner_id)
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE partners SET {', '.join(sets)} WHERE id=%s RETURNING *", vals)
                row=cur.fetchone(); conn.commit(); return row

    def get_partner_by_id(self, partner_id: int):
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM partners WHERE id=%s", (partner_id,))
                return cur.fetchone()

    def get_admin_stats(self) -> dict:
        """Общая операционная статистика для админ-панели.

        Переписано под чистую архитектуру: вместо legacy-таблиц
        (orders / deals / disputes) используются новые partners / bookings.
        Каждый подзапрос обёрнут в COALESCE, а отсутствующие таблицы
        деградируют до нулей на уровне вызывающего API.
        """
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT
                        (SELECT COUNT(*) FROM users) as total_users,
                        (SELECT COUNT(*) FROM partners) as total_masters,
                        (SELECT COUNT(*) FROM partners WHERE status='approved') as verified_masters,
                        (SELECT COUNT(*) FROM bookings) as total_orders,
                        (SELECT COUNT(*) FROM bookings WHERE status='completed') as completed_orders,
                        (SELECT COALESCE(SUM(agreed_price), 0) FROM bookings WHERE status='completed') as total_commission,
                        0 as open_disputes
                ''')
                return cur.fetchone()
