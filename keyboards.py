"""Генераторы клавиатур aiogram (Inline + Reply) с поддержкой двухуровневых категорий услуг."""
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ─── Регистрация ─────────────────────────────────────────────────

def get_role_keyboard() -> InlineKeyboardMarkup:
    """Выбор роли при первой регистрации."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Ես հաճախորդ եմ | Я клиент 👤", callback_data="role_client")
    builder.button(text="Ես վարպետ եմ | Я мастер 🛠️", callback_data="role_master")
    builder.adjust(1)
    return builder.as_markup()


def get_language_keyboard() -> InlineKeyboardMarkup:
    """Выбор языка интерфейса."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🇦🇲 Հայերեն", callback_data="lang_hy")
    builder.button(text="🇷🇺 Русский", callback_data="lang_ru")
    builder.button(text="🇬🇧 English", callback_data="lang_en")
    builder.adjust(3)
    return builder.as_markup()


def get_city_keyboard() -> InlineKeyboardMarkup:
    """Быстрый выбор популярного города."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Երևան | Ереван", callback_data="city_Ереван")
    builder.button(text="Գյումրի | Гюмри", callback_data="city_Гюмри")
    builder.button(text="Վանաձոր | Ванадзор", callback_data="city_Ванадзор")
    builder.button(text="Դիլիջան | Дилижан", callback_data="city_Дилижан")
    builder.button(text="Սևան | Севан", callback_data="city_Севан")
    builder.adjust(2)
    return builder.as_markup()


def get_master_categories_keyboard(master_categories: list[dict], lang: str = "ru") -> InlineKeyboardMarkup:
    """ЭТАП 1: Вывод 10 главных родительских сфер деятельности (двуязычный)."""
    builder = InlineKeyboardBuilder()
    for mcat in master_categories:
        # Динамически берем имя в зависимости от языка пользователя
        name = mcat.get("name_ru") if lang == "ru" else mcat.get("name_am")
        builder.button(
            text=str(name),
            callback_data=f"select_mcat_{mcat['id']}"
        )
    builder.adjust(1)
    return builder.as_markup()
def get_categories_keyboard(categories: list[dict], selected: list[int] = None, lang: str = "ru") -> InlineKeyboardMarkup:
    """ЭТАП 2: Мультивыбор конкретных подкатегорий (услуг) внутри сферы с галочками и кнопкой Назад."""
    selected = selected or []
    builder = InlineKeyboardBuilder()
    
    for cat in categories:
        prefix = "✅ " if cat["id"] in selected else ""
        name = cat.get("name_ru") if lang == "ru" else cat.get("name_hy") or cat.get("name_am")
        builder.button(
            text=f"{prefix}{name}",
            callback_data=f"mcat_{cat['id']}"
        )
        
    # Навигационные кнопки подтверждения и возврата назад
    confirm_text = "✅ Подтвердить" if lang == "ru" else "✅ Հաստատել"
    back_text = "🔙 Назад" if lang == "ru" else "🔙 Ուղղություններ"
    
    builder.button(text=confirm_text, callback_data="cats_done")
    builder.button(text=back_text, callback_data="back_to_mcat")
    builder.adjust(1)
    return builder.as_markup()


# ─── Торги / Сделки ──────────────────────────────────────────────

def get_bid_actions_keyboard(order_id: int, master_id: int, amount: float) -> InlineKeyboardMarkup:
    """Клавиатура для клиента при получении ставки от мастера."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Ընդունել / Принять ({amount} ֏)",
        callback_data=f"accept_bid_{order_id}_{master_id}"
    )
    builder.button(
        text="🔄 Փոխանակել / Поторговаться",
        callback_data=f"counter_bid_{order_id}_{master_id}_{amount}"
    )
    builder.button(
        text="❌ Մերժել / Отклонить",
        callback_data=f"reject_bid_{order_id}_{master_id}"
    )
    builder.adjust(1)
    return builder.as_markup()


def get_book_keyboard(order_id: int, webapp_url: str) -> InlineKeyboardMarkup:
    """Кнопка бронирования — открывает Web App с чеком."""
    builder = InlineKeyboardBuilder()
    from aiogram.types import WebAppInfo
    builder.button(
        text="💳 Забронировать (Idram)",
        web_app=WebAppInfo(url=f"{webapp_url}/booking.html?order_id={order_id}")
    )
    builder.adjust(1)
    return builder.as_markup()


def get_master_accept_keyboard(order_id: int, client_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для мастера — откликнуться на заявку."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📥 Ընդունել / Откликнуться",
        callback_data=f"master_accept_{order_id}_{client_id}"
    )
    builder.button(
        text="⏭️ Բաց թողնել / Пропустить",
        callback_data="master_skip"
    )
    builder.adjust(1)
    return builder.as_markup()
# ─── Закрытие сделки ─────────────────────────────────────────────

def get_close_deal_keyboard() -> InlineKeyboardMarkup:
    """Мастер: сканировать QR или ввести код."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📷 Сканировать QR", callback_data="scan_qr")
    builder.button(text="🔢 Ввести код вручную", callback_data="enter_code")
    builder.adjust(1)
    return builder.as_markup()


# ─── Оценка ──────────────────────────────────────────────────────

def get_rating_keyboard(deal_id: str, master_id: int) -> InlineKeyboardMarkup:
    """Оценка работы мастера от 1 до 5 звёзд."""
    builder = InlineKeyboardBuilder()
    for i in range(1, 6):
        builder.button(text="⭐" * i, callback_data=f"rate_{deal_id}_{master_id}_{i}")
    builder.adjust(5)
    return builder.as_markup()


# ─── Главное меню мастера ────────────────────────────────────────

def get_master_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню мастера (Reply клавиатура)."""
    kb = [
        [KeyboardButton(text="📋 Իմ հայտերը / Мои заявки")],
        [
            KeyboardButton(text="📂 Ուղղություններ / Направления"),
            KeyboardButton(text="📊 Պատմություն / История"),
        ],
        [KeyboardButton(text="📷 Փակել գործարքը / Закрыть сделку")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


# ─── Главное меню клиента ────────────────────────────────────────

def get_client_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню клиента (Reply клавиатура)."""
    kb = [
        [KeyboardButton(text="📝 Նոր հայտ / Новая заявка")],
        [KeyboardButton(text="📊 Պատմություն / История")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
