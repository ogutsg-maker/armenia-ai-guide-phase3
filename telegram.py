from __future__ import annotations
from aiogram import Bot,Dispatcher,Router,types
from aiogram.filters import CommandStart,Command
from aiogram.types import WebAppInfo,InlineKeyboardButton,InlineKeyboardMarkup
from config import BOT_TOKEN,ADMIN_ID,WEBAPP_BASE_URL
router=Router()
def url(p):return f"{WEBAPP_BASE_URL}/{p}"
@router.message(CommandStart())
async def start(m:types.Message):
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✦ Armenia AI Guide",web_app=WebAppInfo(url=url("welcome.html")))]])
    await m.answer("✦ Armenia AI Guide\nARMENIA · AI CONCIERGE\n\nԳտեք, ընտրեք, բանակցեք և ամրագրեք ծառայություններ։",reply_markup=kb)
@router.message(Command("admin"))
async def admin(m:types.Message):
    if m.from_user.id!=ADMIN_ID:return
    await m.answer("👑 Admin",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open Admin",web_app=WebAppInfo(url=url("admin.html")))] ]))
def setup(dp:Dispatcher):dp.include_router(router)
