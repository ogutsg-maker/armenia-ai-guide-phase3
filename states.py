"""FSM-состояния aiogram для регистрации и торгов."""
from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    choosing_role = State()
    choosing_city = State()
    choosing_categories = State()


class BiddingStates(StatesGroup):
    awaiting_master_price = State()       # мастер вводит цену
    awaiting_client_counter = State()     # клиент поторговаться
    awaiting_master_counter = State()     # мастер отвечает на контр-оффер
    awaiting_deal_confirm = State()       # обе стороны подтвердили


class CodeEntryStates(StatesGroup):
    entering_deal_id = State()            # мастер вводит ID сделки
    entering_secure_code = State()        # мастер вводит 6-значный код

class PartnerAIStates(StatesGroup):
    onboarding = State()
