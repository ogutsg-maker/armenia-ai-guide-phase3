"""Telegram WebApp initData validation for API authentication."""
from __future__ import annotations
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

class TelegramWebAppAuthError(Exception):
    pass

def validate_telegram_webapp_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data or not bot_token:
        raise TelegramWebAppAuthError('telegram_init_data_required')
    pairs=dict(parse_qsl(init_data, keep_blank_values=True))
    received=pairs.pop('hash',None)
    if not received:
        raise TelegramWebAppAuthError('telegram_hash_missing')
    data_check_string='\n'.join(f'{k}={v}' for k,v in sorted(pairs.items()))
    secret=hmac.new(b'WebAppData',bot_token.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,data_check_string.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received):
        raise TelegramWebAppAuthError('telegram_hash_invalid')
    try:
        user=json.loads(pairs.get('user','{}'))
    except Exception as exc:
        raise TelegramWebAppAuthError('telegram_user_invalid') from exc
    if not user.get('id'):
        raise TelegramWebAppAuthError('telegram_user_missing')
    return user
