import asyncio
from datetime import datetime
import html
import io
import json
import logging
import os
import random
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from google.oauth2.service_account import Credentials
import gspread

# ---------------------------------------------------------
# 1. КОНФИГУРАЦИЯ И ИНИЦИАЛИЗАЦИЯ
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID", "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_KEY)

# Актуальные модели Google GenAI (без несуществующих версий)
AUDIO_MODELS_CASCADE = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
]

IMAGE_MODELS_CASCADE = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

# Подключение к Google Таблицам
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# Оперативная память состояний
pending_receipts: dict[int, dict] = {}
active_shifts: dict[int, dict] = {}
saved_receipt_file_ids: set[str] = set()

# ---------------------------------------------------------
# 2. FSM СОСТОЯНИЯ
# ---------------------------------------------------------
class VoiceProjectState(StatesGroup):
    waiting_for_voice = State()
    editing_field = State()

class RequisitesState(StatesGroup):
    waiting_for_input = State()

NAV_BUTTONS = [
    "📸 Как отправить чек",
    "🍔 Обеденные (2 500 ₸)",
    "⏱ Моя смена",
    "💰 Мои чеки",
    "💳 Мои реквизиты",
    "💼 Панель выплат (Admin)",
]

# ---------------------------------------------------------
# 3. СИНХРОННЫЕ ФУНКЦИИ GOOGLE SHEETS И API
# ---------------------------------------------------------
def _sync_get_available_models() -> list[str]:
    """Синхронно получает список доступных моделей Gemini из API"""
    try:
        available = []
        for model in ai_client.models.list():
            if "gemini" in model.name.lower():
                available.append(model.name)
        return available
    except Exception as e:
        logger.error("Ошибка при получении списка моделей: %s", e)
        return [f"Ошибка API: {e}"]


def _sync_get_or_register_employee(user_id_str: str, username_str: str, full_name: str) -> str:
    official_name = None
    try:
        ws_ref = spreadsheet.worksheet("Справочники")
        records = ws_ref.get_all_records()

        for r in records:
            chat_id = str(r.get("Chat ID", "")).strip()
            tg_nick = str(r.get("Telegram никнейм", "")).strip().lower()
            name = str(r.get("Имя", "")).strip()

            if user_id_str and chat_id == user_id_str:
                official_name = name
                break
            if username_str and tg_nick == username_str:
                official_name = name
                break

        if not official_name:
            official_name = full_name
            ws_ref.append_row([
                "", "", official_name, username_str if username_str else "-", user_id_str,
            ])
            logger.info("➕ Новый сотрудник '%s' добавлен в 'Справочники'", official_name)
    except Exception as e:
        logger.error("Ошибка чтения 'Справочников': %s", e)
        official_name = full_name

    try:
        ws_pay = spreadsheet.worksheet("Выплаты команде")
        col_names = ws_pay.col_values(1)

        if official_name not in col_names:
            next_row = len(col_names) + 1
            formula_debt = f'=SUMIFS(Операции!F:F; Операции!G:G; A{next_row}; Операции!H:H; "К возмещению")'
            formula_paid = f'=SUMIFS(Операции!F:F; Операции!G:G; A{next_row}; Операции!H:H; "Выплачено")'

            ws_pay.append_row(
                [official_name, "Команда", "-", formula_debt, formula_paid],
                value_input_option="USER_ENTERED",
            )
            logger.info("➕ Сотрудник '%s' добавлен в 'Выплаты команде'", official_name)
    except Exception as e:
        logger.error("Ошибка проверки 'Выплат': %s", e)

    return official_name


def _sync_get_employee_requisites(employee_name: str) -> str:
    try:
        ws_pay = spreadsheet.worksheet("Выплаты команде")
        rows = ws_pay.get_all_values()
        for row in rows[1:]:
            if len(row) >= 3 and row[0].strip():
                name_in_table = row[0].strip().lower()
                emp_name_low = employee_name.lower()
                if emp_name_low in name_in_table or name_in_table in emp_name_low:
                    reqs = row[2].strip()
                    if reqs and reqs not in ["-", "#ERROR!"]:
                        return reqs.lstrip("'")
                    return "Реквизиты еще не указаны"
    except Exception as e:
        logger.error("Ошибка чтения реквизитов: %s", e)
    return "Реквизиты еще не указаны"


def _sync_save_employee_requisites(employee_name: str, requisites_text: str):
    try:
        ws_pay = spreadsheet.worksheet("Выплаты команде")
        col_names = ws_pay.col_values(1)

        matched_idx = None
        emp_name_low = employee_name.lower()
        for idx, name in enumerate(col_names, start=1):
            name_low = name.strip().lower()
            if name_low and (emp_name_low in name_low or name_low in emp_name_low):
                matched_idx = idx
                break

        if matched_idx:
            clean_text = requisites_text.strip()
            if clean_text.startswith(("+", "=")):
                clean_text = "'" + clean_text
            ws_pay.update_cell(matched_idx, 3, clean_text)
            logger.info("✅ Реквизиты для %s сохранены в строку %d", employee_name, matched_idx)
    except Exception as e:
        logger.error("Ошибка сохранения реквизитов: %s", e)


def _sync_get_active_projects() -> list[str]:
    try:
        ws = spreadsheet.worksheet("Проекты")
        rows = ws.get_all_records()
        active = [
            str(r.get("Проект")).strip()
            for r in rows
            if str(r.get("Статус проекта")).strip().lower() in ["в работе", "активен"]
            and str(r.get("Проект")).strip()
        ]
        return active if active else ["Склад / Общее"]
    except Exception as e:
        logger.error("Ошибка получения активных проектов: %s", e)
        return ["Склад / Общее"]


def _sync_log_expense(
    project: str, category: str, amount: float, employee: str, comment: str, check_type: str = "Норматив (без чека)",
):
    ws = spreadsheet.worksheet("Операции")
    tx_id = f"TX-{random.randint(10000, 99999)}"
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    ws.append_row([
        tx_id, now_str, "Расход", project, category, amount, employee, "К возмещению", check_type, comment,
    ])


def _sync_check_duplicate_receipt(employee: str, amount: float, merchant: str) -> dict | None:
    try:
        ws = spreadsheet.worksheet("Операции")
        rows = ws.get_all_records()
        today_str = datetime.now().strftime("%d.%m.%Y")

        for r in reversed(rows[-50:]):
            dt = str(r.get("Дата и время", ""))
            who = str(r.get("Кто оплатил", "")).strip().lower()
            comm = str(r.get("Комментарий", "")).lower()

            try:
                amt = float(str(r.get("Сумма (₸)", 0)).replace(" ", "").replace(",", "."))
            except ValueError:
                amt = 0.0

            if (
                today_str in dt
                and (employee.lower() in who or who in employee.lower())
                and abs(amt - amount) < 0.01
            ):
                if merchant.lower() in comm or not merchant or merchant == "неизвестно":
                    return {
                        "time": dt.split(" ")[-1] if " " in dt else dt,
                        "project": r.get("Проект", "-"),
                        "amount": amt,
                        "comment": r.get("Комментарий", "-"),
                    }
    except Exception as e:
        logger.error("Ошибка проверки дубликатов: %s", e)
    return None


def _sync_get_debts_summary() -> dict[str, float]:
    ws = spreadsheet.worksheet("Операции")
    rows = ws.get_all_records()
    debts = {}

    for r in rows:
        status, who, amount_raw = "", "", 0

        for k, v in r.items():
            k_lower = str(k).lower()
            if "статус" in k_lower:
                status = str(v).strip().lower()
            elif "кто" in k_lower or "сотрудник" in k_lower:
                who = str(v).strip()
            elif "сумм" in k_lower or "₸" in k_lower:
                amount_raw = v

        if "к возмещению" in status and who:
            try:
                amount = float(str(amount_raw).replace(" ", "").replace(",", "."))
            except ValueError:
                amount = 0.0
            debts[who] = debts.get(who, 0.0) + amount
    return debts


def _sync_get_user_pending_receipts(employee_name: str) -> tuple[float, list[dict]]:
    ws = spreadsheet.worksheet("Операции")
    rows = ws.get_all_records()
    user_items = []
    total = 0.0

    for r in rows:
        status, who, amount_raw = "", "", 0
        proj, comm, dt = "-", "-", "-"

        for k, v in r.items():
            k_lower = str(k).lower()
            if "статус" in k_lower:
                status = str(v).strip().lower()
            elif "кто" in k_lower or "сотрудник" in k_lower:
                who = str(v).strip()
            elif "сумм" in k_lower or "₸" in k_lower:
                amount_raw = v
            elif "проект" in k_lower:
                proj = str(v)
            elif "коммент" in k_lower or "описание" in k_lower:
                comm = str(v)
            elif "дата" in k_lower:
                dt = str(v)

        if (
            employee_name.lower() in who.lower()
            or who.lower() in employee_name.lower()
        ) and "к возмещению" in status:
            try:
                amt = float(str(amount_raw).replace(" ", "").replace(",", "."))
            except ValueError:
                amt = 0.0
            total += amt
            user_items.append({"project": proj, "amount": amt, "comment": comm, "date": dt})
    return total, user_items


def _sync_batch_settle_debts(person_name: str) -> int:
    ws = spreadsheet.worksheet("Операции")
    all_values = ws.get_all_values()
    if not all_values or len(all_values) < 2:
        return 0

    headers = [str(h).strip().lower() for h in all_values[0]]
    status_col_idx = 8
    who_col_idx = 7

    for idx, h in enumerate(headers, start=1):
        if "статус" in h:
            status_col_idx = idx
        elif "кто" in h or "сотрудник" in h:
            who_col_idx = idx

    cells_to_update = []
    target = person_name.strip().lower()

    for row_idx, row in enumerate(all_values[1:], start=2):
        if len(row) >= max(status_col_idx, who_col_idx):
            who_val = row[who_col_idx - 1].strip().lower()
            status_val = row[status_col_idx - 1].strip().lower()

            if (target in who_val or who_val in target) and "к возмещению" in status_val:
                cells_to_update.append(
                    gspread.Cell(row=row_idx, col=status_col_idx, value="Выплачено")
                )

    if cells_to_update:
        ws.update_cells(cells_to_update)
        logger.info("✅ Пакетно обновлено %d записей для '%s'", len(cells_to_update), person_name)
    return len(cells_to_update)


def _sync_create_project(data: dict) -> tuple[str, float]:
    ws_proj = spreadsheet.worksheet("Проекты")
    next_row = len(ws_proj.col_values(1)) + 1

    event_date = str(data.get("event_date", "")).strip()
    raw_name = str(data.get("project_name", "")).strip()

    if event_date and not raw_name.startswith(event_date):
        full_proj_name = f"{event_date} {raw_name}"
    else:
        full_proj_name = raw_name

    total_price = float(data.get("price", 0))
    prepay_pct = int(data.get("prepay_percent", 0))
    paid_amount = total_price * (prepay_pct / 100.0)

    formula_expenses = f'=SUMIFS(Операции!F:F; Операции!D:D; A{next_row}; Операции!C:C; "Расход")'
    formula_profit = f"=E{next_row}-F{next_row}"
    formula_margin = f"=IF(E{next_row}>0; G{next_row}/E{next_row}; 0)"

    ws_proj.append_row(
        [
            full_proj_name, event_date, data.get("location", "Площадка"), "В работе",
            total_price, formula_expenses, formula_profit, formula_margin,
        ],
        value_input_option="USER_ENTERED",
    )

    if paid_amount > 0:
        ws_ops = spreadsheet.worksheet("Операции")
        tx_id = f"TX-{random.randint(10000, 99999)}"
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        ws_ops.append_row([
            tx_id, now_str, "Доход", full_proj_name, "Комплексный продакшн",
            paid_amount, "Клиент", "Не требуется", "-", f"Предоплата ({prepay_pct}%)",
        ])

    return full_proj_name, paid_amount


# ---------------------------------------------------------
# 4. АСИНХРОННЫЕ ОБЕРТКИ
# ---------------------------------------------------------
async def get_available_models() -> list[str]:
    return await asyncio.to_thread(_sync_get_available_models)

async def get_or_register_employee(user: types.User) -> str:
    return await asyncio.to_thread(_sync_get_or_register_employee, str(user.id).strip(), f"@{user.username.lower().strip()}" if user.username else "", (user.full_name or "Новый сотрудник").strip())

async def get_employee_requisites(employee_name: str) -> str:
    return await asyncio.to_thread(_sync_get_employee_requisites, employee_name)

async def save_employee_requisites(employee_name: str, requisites_text: str):
    await asyncio.to_thread(_sync_save_employee_requisites, employee_name, requisites_text)

async def get_active_projects() -> list[str]:
    return await asyncio.to_thread(_sync_get_active_projects)

async def log_expense(project: str, category: str, amount: float, employee: str, comment: str, check_type: str = "Норматив (без чека)"):
    await asyncio.to_thread(_sync_log_expense, project, category, amount, employee, comment, check_type)

async def check_for_duplicate_receipt(employee: str, amount: float, merchant: str) -> dict | None:
    return await asyncio.to_thread(_sync_check_duplicate_receipt, employee, amount, merchant)

async def get_debts_summary() -> dict[str, float]:
    return await asyncio.to_thread(_sync_get_debts_summary)

async def get_user_pending_receipts(employee_name: str) -> tuple[float, list[dict]]:
    return await asyncio.to_thread(_sync_get_user_pending_receipts, employee_name)

async def batch_settle_debts(person_name: str) -> int:
    return await asyncio.to_thread(_sync_batch_settle_debts, person_name)

async def create_project(data: dict) -> tuple[str, float]:
    return await asyncio.to_thread(_sync_create_project, data)


# ---------------------------------------------------------
# 5. КЛАВИАТУРЫ И ХЕЛПЕРЫ
# ---------------------------------------------------------
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="📸 Как отправить чек"), KeyboardButton(text="🍔 Обеденные (2 500 ₸)")],
        [KeyboardButton(text="⏱ Моя смена"), KeyboardButton(text="💰 Мои чеки")],
        [KeyboardButton(text="💳 Мои реквизиты")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton(text="💼 Панель выплат (Admin)")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

async def show_project_selection(target_msg, amount: float, merchant: str, data: dict, employee: str):
    projects = await get_active_projects()
    kb_buttons = [[InlineKeyboardButton(text=f"📌 {p[:30]}", callback_data=f"proj_{idx}")] for idx, p in enumerate(projects)]
    if "Склад / Общее" not in projects:
        kb_buttons.append([InlineKeyboardButton(text="🏢 Склад / Общее", callback_data="proj_general")])

    text = (
        f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({html.escape(merchant)})\n"
        f"📂 {html.escape(data.get('category', 'Прочее'))} — {html.escape(data.get('description', ''))}\n"
        f"👤 Сотрудник: <b>{html.escape(employee)}</b>\n\n"
        "👉 <b>К какому проекту привязать покупку?</b>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    if isinstance(target_msg, types.Message):
        await target_msg.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await target_msg.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


# ---------------------------------------------------------
# 6. БАЗОВЫЕ КОМАНДЫ, АДМИН И РЕКВИЗИТЫ
# ---------------------------------------------------------
@dp.message(Command("models"))
async def cmd_check_models(message: types.Message):
    """Админ-команда для проверки доступных моделей прямо в Telegram"""
    if message.from_user.id not in ADMIN_IDS:
        return
    
    msg_wait = await message.answer("🔄 Запрашиваю список моделей у API...")
    models = await get_available_models()
    
    if models:
        text = "<b>Доступные модели Gemini:</b>\n\n" + "\n".join([f"• <code>{m}</code>" for m in models])
    else:
        text = "Список пуст или произошла ошибка."
        
    await msg_wait.edit_text(text, parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    name = await get_or_register_employee(message.from_user)
    kb = get_main_keyboard(message.from_user.id)
    reqs = await get_employee_requisites(name)

    extra_text = ""
    if reqs == "Реквизиты еще не указаны":
        extra_text = "\n\n💡 <b>Важно:</b> Нажмите кнопку «💳 Мои реквизиты» и укажите номер Kaspi / ИИН для быстрых переводов!"

    await message.answer(
        f"Привет, {html.escape(name)}! 🎛️\n\n"
        "Я бот для учета расходов на площадках.\n"
        "• Отправь фото чека для распознавания\n"
        "• Жми «🍔 Обеденные» для суточных 2 500 ₸\n"
        "• Запускай «⏱ Моя смена» для учета времени на монтаже."
        f"{extra_text}",
        reply_markup=kb, parse_mode="HTML",
    )

@dp.message(F.text == "📸 Как отправить чек")
async def msg_how_to(message: types.Message):
    await message.answer(
        "📷 <b>Как отправить чек:</b>\n\n"
        "1. Нажмите на скрепку и отправьте фото чека (или скриншот Kaspi / Яндекс Go).\n"
        "2. Gemini автоматически определит сумму и категорию расхода.\n"
        "3. Выберите проект кнопкой.\n\n"
        "Сумма сразу добавится к вашим выплатам!", parse_mode="HTML",
    )

@dp.message(F.text == "💰 Мои чеки")
async def msg_my_receipts(message: types.Message):
    status_wait = await message.answer("🔍 Проверяю ваши чеки в таблице...")
    name = await get_or_register_employee(message.from_user)
    total, items = await get_user_pending_receipts(name)

    if not items:
        await status_wait.edit_text(f"👤 <b>{html.escape(name)}</b>\n\nУ вас нет активных чеков к возмещению. Все выплачено! 🎉", parse_mode="HTML")
        return

    text = f"👤 <b>Чеки к возмещению ({html.escape(name)})</b>\n\n💰 <b>Общая сумма:</b> {total:,.0f} ₸\n\n<b>Список в обработке:</b>\n"
    for it in items[:10]:
        text += f"• {it['amount']:,.0f} ₸ — <i>{html.escape(it['project'])}</i> ({html.escape(it['comment'])})\n"
    await status_wait.edit_text(text, parse_mode="HTML")

@dp.message(F.text == "💳 Мои реквизиты")
async def cmd_my_requisites(message: types.Message):
    employee = await get_or_register_employee(message.from_user)
    reqs = await get_employee_requisites(employee)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Изменить / Ввести реквизиты", callback_data="edit_reqs")]])
    await message.answer(
        f"💳 <b>Реквизиты для выплат:</b>\n\n👤 <b>Сотрудник:</b> {html.escape(employee)}\n📋 <b>Данные:</b>\n<code>{html.escape(reqs)}</code>\n\n"
        "<i>По этим реквизитам администратор переводит вам деньги за чеки и обеденные.</i>", reply_markup=kb, parse_mode="HTML"
    )

@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_requisites(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(RequisitesState.waiting_for_input)
    await callback.message.edit_text(
        "📝 <b>Введите ваши реквизиты одним сообщением:</b>\n\nУкажите номер перевода Kaspi, ИИН и номер карты.\n\n"
        "Пример:\n<code>+7 777 123 4567 (Kaspi)\nИИН: 990102350444\nКарта: 4400 4301 9876 5432</code>\n\nОтправьте данные текстом прямо в этот чат:", parse_mode="HTML"
    )

@dp.message(RequisitesState.waiting_for_input, F.text)
async def process_requisites_text(message: types.Message, state: FSMContext):
    req_text = message.text.strip()
    employee = await get_or_register_employee(message.from_user)
    await save_employee_requisites(employee, req_text)
    await state.clear()
    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n\n👤 {html.escape(employee)}\n📋 <b>Ваши данные:</b>\n<code>{html.escape(req_text)}</code>\n\n"
        "Теперь при выплатах администратор будет сразу видеть эти данные.", reply_markup=get_main_keyboard(message.from_user.id), parse_mode="HTML"
    )

# ---------------------------------------------------------
# 7. ОБЕДЕННЫЕ И ТАЙМ-ТРЕКЕР СМЕН
# ---------------------------------------------------------
@dp.message(F.text == "🍔 Обеденные (2 500 ₸)")
async def cmd_quick_meal(message: types.Message):
    projects = await get_active_projects()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=p[:30], callback_data=f"meal_{idx}")] for idx, p in enumerate(projects)])
    await message.answer("🍔 <b>Обеденные (2 500 ₸)</b>\nВыберите проект, на котором работаете:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("meal_"))
async def cb_confirm_meal(callback: types.CallbackQuery):
    await callback.answer()
    idx_str = callback.data.replace("meal_", "")
    projects = await get_active_projects()
    full_project = projects[int(idx_str)] if idx_str.isdigit() and int(idx_str) < len(projects) else "Склад / Общее"

    user_id = callback.from_user.id
    if user_id in active_shifts: active_shifts[user_id]["claimed_meals"] += 1
    employee = await get_or_register_employee(callback.from_user)

    await log_expense(project=full_project, category="Питание команды", amount=2500, employee=employee, comment="Обеденные (быстрая выплата)")
    await callback.message.edit_text(f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n\n🎯 Проект: {html.escape(full_project)}\nСумма передана в таблицу к возмещению.", parse_mode="HTML")

@dp.message(F.text == "⏱ Моя смена")
async def cmd_shift_menu(message: types.Message):
    user_id = message.from_user.id
    if user_id not in active_shifts:
        projects = await get_active_projects()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"🟢 {p[:30]}", callback_data=f"startshift_{idx}")] for idx, p in enumerate(projects)])
        await message.answer("⏱ <b>Учет смены</b>\nУ вас нет активной смены. Выберите проект для старта:", reply_markup=kb, parse_mode="HTML")
    else:
        shift = active_shifts[user_id]
        duration = datetime.now() - shift["start_time"]
        hours = int(duration.total_seconds() // 3600)
        minutes = int((duration.total_seconds() % 3600) // 60)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Завершить смену", callback_data="end_shift")],
            [InlineKeyboardButton(text="🍔 Взять обед сейчас (2 500 ₸)", callback_data="meal_shift")],
        ])
        await message.answer(
            "⏱ <b>Текущая смена в процессе</b>\n\n"
            f"🎯 Проект: <b>{html.escape(shift['project'])}</b>\n⏳ Прошло: <b>{hours} ч. {minutes} мин.</b>\n"
            f"🍔 Взято обеденных: {shift['claimed_meals'] * 2500} ₸\n\n<i>Каждые полные 6 часов смены начисляют 2 500 ₸.</i>", reply_markup=kb, parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("startshift_"))
async def cb_start_shift(callback: types.CallbackQuery):
    await callback.answer()
    idx_str = callback.data.replace("startshift_", "")
    projects = await get_active_projects()
    full_project = projects[int(idx_str)] if idx_str.isdigit() and int(idx_str) < len(projects) else "Склад / Общее"

    active_shifts[callback.from_user.id] = {"project": full_project, "start_time": datetime.now(), "claimed_meals": 0}
    await callback.message.edit_text(
        f"🟢 <b>Смена начата!</b>\n\n🎯 Проект: <b>{html.escape(full_project)}</b>\n🕒 Время старта: {datetime.now().strftime('%H:%M')}\n\n"
        "При завершении смены бот рассчитает отработанные часы и начислит обеденные.", parse_mode="HTML"
    )

@dp.callback_query(F.data == "meal_shift")
async def cb_meal_shift(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id not in active_shifts:
        await callback.message.edit_text("Смена не найдена.")
        return

    shift = active_shifts[user_id]
    shift["claimed_meals"] += 1
    employee = await get_or_register_employee(callback.from_user)

    await log_expense(project=shift["project"], category="Питание команды", amount=2500, employee=employee, comment="Обеденные во время смены")
    await callback.message.answer("✅ Обеденные 2 500 ₸ добавлены к смене и внесены в таблицу!")

@dp.callback_query(F.data == "end_shift")
async def cb_end_shift(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if user_id not in active_shifts:
        await callback.answer("Смена не найдена", show_alert=True)
        return

    shift = active_shifts.pop(user_id)
    duration = datetime.now() - shift["start_time"]
    hours = duration.total_seconds() / 3600
    earned_meals_count = int(hours // 6)
    remaining_payout = max(0, earned_meals_count - shift["claimed_meals"]) * 2500
    employee = await get_or_register_employee(callback.from_user)

    if remaining_payout > 0:
        await log_expense(project=shift["project"], category="Питание команды", amount=remaining_payout, employee=employee, comment=f"Обеденные за смену ({int(hours)} ч, остаток)")

    msg = (
        f"🔴 <b>Смена завершена!</b>\n\n🎯 Проект: {html.escape(shift['project'])}\n"
        f"⏱ Отработано: {int(hours)} ч. {int((duration.total_seconds() % 3600) // 60)} мин.\n🍔 Положено обедов: {earned_meals_count} × 2 500 ₸\n"
    )
    msg += f"💰 Начислено к выплате: <b>{remaining_payout:,.0f} ₸</b>" if remaining_payout > 0 else "👌 Все положенные обеденные уже были учтены."
    await callback.message.edit_text(msg, parse_mode="HTML")

# ---------------------------------------------------------
# 8. ПАНЕЛЬ УПРАВЛЕНИЯ (ADMIN) И ДИКТОВКА ПРОЕКТОВ
# ---------------------------------------------------------
@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ Нет прав доступа.", parse_mode="HTML")
        return
    await render_admin_menu(message)

async def render_admin_menu(event_target):
    debts = await get_debts_summary()
    total_debt = sum(debts.values()) if debts else 0

    text = f"💼 <b>Панель управления (Admin)</b>\n\n💵 <b>Текущий долг перед командой:</b> {total_debt:,.0f} ₸\n\nВыберите действие:"
    buttons = [
        [InlineKeyboardButton(text="🎙 Надиктовать проект", callback_data="admin_voice_project")],
        [InlineKeyboardButton(text=f"💸 Выплаты команде ({len(debts)})", callback_data="admin_payouts_list")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_hub")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(event_target, types.Message):
        await event_target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event_target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_hub")
async def cb_admin_hub(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await render_admin_menu(callback)

@dp.callback_query(F.data == "admin_voice_project")
async def cb_start_voice_project(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав доступа", show_alert=True)
        return

    await state.set_state(VoiceProjectState.waiting_for_voice)
    await callback.message.edit_text(
        "🎙 <b>Зажмите микрофон и надиктуйте проект:</b>\n\n"
        "Назовите дату, мероприятие, локацию, сумму сметы и предоплату.\n\n"
        "<i>Пример:\n«Свадьба Азамата 15 октября, отель Sheraton, смета полтора миллиона, предоплата 50%»</i>\n\nЖду голосовое сообщение...", parse_mode="HTML",
    )

async def render_project_card(target, data: dict):
    transcript = data.get("transcript", "")
    event_date = data.get("event_date", "-")
    proj_name = data.get("project_name", "-")
    location = data.get("location", "-")
    price = float(data.get("price", 0))
    prepay_pct = int(data.get("prepay_percent", 0))
    paid_preview = price * (prepay_pct / 100.0)

    transcript_block = f"🗣 <i>«{html.escape(transcript)}»</i>\n\n" if transcript else ""
    text = (
        f"📋 <b>Проверьте данные проекта:</b>\n\n{transcript_block}📅 <b>Дата мероприятия:</b> {html.escape(str(event_date))}\n"
        f"🎯 <b>Название:</b> {html.escape(str(proj_name))}\n📍 <b>Локация / Площадка:</b> {html.escape(str(location))}\n"
        f"💵 <b>Смета договора:</b> {price:,.0f} ₸\n💰 <b>Предоплата:</b> {prepay_pct}% ({paid_preview:,.0f} ₸)\n\nВсё верно?"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Все верно, создать!", callback_data="confirm_voice_proj")],
        [InlineKeyboardButton(text="🎙 Наговорить заново", callback_data="admin_voice_project"), InlineKeyboardButton(text="✏️ Исправить поле", callback_data="edit_voice_fields_menu")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hub")],
    ])

    if isinstance(target, types.Message):
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(VoiceProjectState.waiting_for_voice, F.voice)
async def process_voice_project(message: types.Message, state: FSMContext):
    status_msg = await message.answer("🔄 Отправка запроса к API Gemini...")

    voice = message.voice
    file_io = io.BytesIO()
    await bot.download(voice, destination=file_io)
    audio_bytes = file_io.getvalue()

    prompt = """
    Ты — экспертный финансовый ассистент компании по аренде сценического оборудования.
    Внимательно прослушай голосовое сообщение администратора о новом мероприятии/проекте.

    ИНСТРУКЦИИ:
    1. Сначала сделай точную текстовую транскрипцию всего, что услышал, в поле "transcript".
    2. Переведи словесные числа в цифры (полтора миллиона -> 1500000).
    3. Выдели дату мероприятия (пятнадцатое октября -> 15.10).
    4. Предоплата (аванс 50% -> 50, полная -> 100).

    Верни СТРОГО JSON:
    {"transcript": "текст", "event_date": "ДД.ММ", "project_name": "название", "location": "площадка", "price": 1500000, "prepay_percent": 50}
    """

    def _call_gemini_audio(model_name: str):
        return ai_client.models.generate_content(
            model=model_name,
            contents=[genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"), prompt],
            config=genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
        )

    response, last_error = None, None
    for model_name in AUDIO_MODELS_CASCADE:
        try:
            resp = await asyncio.to_thread(_call_gemini_audio, model_name)
            if resp and resp.text:
                response = resp
                break
        except Exception as e:
            last_error = str(e)
            logger.warning("Модель %s вернула ошибку: %s", model_name, e)
            continue

    if not response or not response.text:
        error_details = html.escape(last_error) if last_error else "Пустой ответ от API"
        await status_msg.edit_text(f"⚠️ Ошибка API Gemini. Детали: <code>{error_details}</code>", parse_mode="HTML")
        return

    try:
        data = json.loads(response.text.strip())
        await state.update_data(
            transcript=data.get("transcript", ""), event_date=data.get("event_date", datetime.now().strftime("%d.%m")),
            project_name=data.get("project_name", "Мероприятие"), location=data.get("location", "Площадка"),
            price=float(data.get("price", 0)), prepay_percent=int(data.get("prepay_percent", 0)),
        )
        await render_project_card(status_msg, await state.get_data())
    except Exception as e:
        logger.error("Ошибка парсинга аудио JSON: %s", e)
        await status_msg.edit_text(f"⚠️ Ошибка разбора JSON от Gemini. Детали: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "edit_voice_fields_menu")
async def cb_edit_fields_menu(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Дату", callback_data="field_event_date"), InlineKeyboardButton(text="🎯 Название", callback_data="field_project_name")],
        [InlineKeyboardButton(text="📍 Локацию", callback_data="field_location"), InlineKeyboardButton(text="💵 Смету", callback_data="field_price")],
        [InlineKeyboardButton(text="💰 Предоплату (%)", callback_data="field_prepay_percent")],
        [InlineKeyboardButton(text="⬅️ Назад к карточке", callback_data="back_to_card")],
    ])
    await callback.message.edit_text("✏️ <b>Выберите, какое поле нужно исправить:</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("field_"))
async def cb_select_field_to_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    field_name = callback.data.replace("field_", "")
    await state.update_data(editing_target=field_name)
    await state.set_state(VoiceProjectState.editing_field)

    prompts = {
        "event_date": "Введите правильную <b>дату мероприятия</b> (например: <code>25.09</code> или <code>15.10</code>):",
        "project_name": "Введите правильное <b>название проекта</b> (например: <code>Концерт Баста</code>):",
        "location": "Введите правильную <b>локацию / площадку</b> (например: <code>Отель Sheraton</code>):",
        "price": "Введите правильную <b>сумму сметы числом</b> (например: <code>1500000</code>):",
        "prepay_percent": "Введите процент предоплаты числом <b>(0, 50 или 100)</b>:",
    }
    await callback.message.edit_text(prompts.get(field_name, "Введите новое значение:"), parse_mode="HTML")

@dp.message(VoiceProjectState.editing_field, F.text)
async def process_field_edit_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("editing_target")
    text_val = message.text.strip()

    if field == "price":
        try: await state.update_data(price=float(text_val.replace(" ", "").replace(",", ".")))
        except ValueError:
            await message.answer("⚠️ Введите сумму числом (например: <code>1500000</code>):", parse_mode="HTML")
            return
    elif field == "prepay_percent":
        try: await state.update_data(prepay_percent=int(text_val.replace("%", "").strip()))
        except ValueError:
            await message.answer("⚠️ Введите 0, 50 или 100:", parse_mode="HTML")
            return
    elif field in ["event_date", "project_name", "location"]:
        await state.update_data({field: text_val})

    await state.set_state(VoiceProjectState.waiting_for_voice)
    await render_project_card(await message.answer("🔄 Обновляю данные карточки..."), await state.get_data())

@dp.callback_query(F.data == "back_to_card")
async def cb_back_to_card(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await render_project_card(callback, await state.get_data())

@dp.callback_query(F.data == "confirm_voice_proj")
async def cb_confirm_voice_proj(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if not data or "project_name" not in data:
        await callback.answer("Данные устарели, надиктуйте заново.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Записываю проект в Google Таблицу...")
    try:
        full_proj_name, paid_amount = await create_project(data)
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В панель управления", callback_data="admin_hub")]])
        await callback.message.edit_text(
            f"✅ <b>Мероприятие создано!</b>\n\n🎯 <b>{html.escape(full_proj_name)}</b>\n📅 Дата: {html.escape(str(data.get('event_date', '')))}\n"
            f"📍 Локация: {html.escape(str(data.get('location', 'Площадка')))}\n💵 Смета: {float(data.get('price', 0)):,.0f} ₸\n"
            f"💰 Предоплата: {paid_amount:,.0f} ₸\n\n<i>Проект внесен в таблицу и готов к работе!</i>", reply_markup=kb, parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка сохранения проекта: %s", e)
        await callback.message.edit_text(f"⚠️ Ошибка записи: <code>{html.escape(str(e))}</code>", parse_mode="HTML")


# ---------------------------------------------------------
# 9. ВЫПЛАТЫ КОМАНДЕ В АДМИНКЕ
# ---------------------------------------------------------
@dp.callback_query(F.data == "admin_payouts_list")
async def cb_admin_payouts_list(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    debts = await get_debts_summary()
    buttons = []
    if not debts:
        text = "💼 <b>Выплаты команде</b>\n\n🎉 <b>Все долги закрыты!</b> Нет расходов к возмещению."
    else:
        text = f"💼 <b>Выплаты команде к возмещению</b>\n\n💵 <b>Общий долг:</b> {sum(debts.values()):,.0f} ₸\n\n<b>По сотрудникам:</b>\n"
        for idx, (person, sum_amt) in enumerate(debts.items()):
            text += f"• <b>{html.escape(person)}</b>: {sum_amt:,.0f} ₸\n"
            buttons.append([InlineKeyboardButton(text=f"💸 Погасить: {person[:20]} ({sum_amt:,.0f} ₸)", callback_data=f"pay_{idx}")])

    buttons.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="admin_hub")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")

@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay_person_preview(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS: return

    idx_str = callback.data.replace("pay_", "")
    debts = await get_debts_summary()
    persons_list = list(debts.keys())

    if not idx_str.isdigit() or int(idx_str) >= len(persons_list):
        await callback.answer("Данные устарели, обновите список.", show_alert=True)
        return

    full_person_name = persons_list[int(idx_str)]
    amount, reqs = debts.get(full_person_name, 0.0), await get_employee_requisites(full_person_name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Переведено {amount:,.0f} ₸ (Списать)", callback_data=f"confirmpay_{idx_str}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payouts_list")],
    ])
    await callback.message.edit_text(
        f"💼 <b>Окно выплаты сотруднику</b>\n\n👤 <b>Сотрудник:</b> {html.escape(full_person_name)}\n💵 <b>Сумма к переводу:</b> <code>{amount:,.0f}</code> ₸\n\n"
        f"📋 <b>Реквизиты для Kaspi / Банка:</b>\n<code>{html.escape(reqs)}</code>\n\n<i>1. Скопируйте данные и сделайте перевод в Kaspi.\n2. Нажмите зеленую кнопку.</i>",
        reply_markup=kb, parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("confirmpay_"))
async def cb_confirm_payment(callback: types.CallbackQuery):
    await callback.answer("Списываю долг в таблице...", show_alert=False)
    if callback.from_user.id not in ADMIN_IDS: return

    idx_str = callback.data.replace("confirmpay_", "")
    debts = await get_debts_summary()
    persons_list = list(debts.keys())

    if idx_str.isdigit() and int(idx_str) < len(persons_list):
        await batch_settle_debts(persons_list[int(idx_str)])
    await render_admin_menu(callback)


# ---------------------------------------------------------
# 10. РАСПОЗНАВАНИЕ ЧЕКОВ (ФОТО)
# ---------------------------------------------------------
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    photo = message.photo[-1]
    if photo.file_unique_id in saved_receipt_file_ids:
        await message.answer("⚠️ <b>Этот чек уже был успешно внесен в таблицу ранее!</b> Повторная запись отменена.", parse_mode="HTML")
        return

    status_msg = await message.answer("🔄 Отправка запроса к API Gemini...")
    file_io = io.BytesIO()
    await bot.download(photo, destination=file_io)
    image_bytes = file_io.getvalue()

    prompt = """
    Ты финансовый сканер для компании по прокату сценического оборудования.
    Изучи чек и верни СТРОГО чистый JSON:
    {"amount": 4500, "merchant": "Яндекс Go", "category": "Такси / Логистика / ГСМ", "description": "суть покупки (2-4 слова)"}
    """

    def _call_gemini_image(model_name: str):
        return ai_client.models.generate_content(
            model=model_name,
            contents=[genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
            config=genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
        )

    response, last_error = None, None
    for model_name in IMAGE_MODELS_CASCADE:
        try:
            resp = await asyncio.to_thread(_call_gemini_image, model_name)
            if resp and resp.text:
                response = resp
                break
        except Exception as e:
            last_error = str(e)
            logger.warning("Модель %s вернула ошибку: %s", model_name, e)
            continue

    if not response or not response.text:
        error_details = html.escape(last_error) if last_error else "Пустой ответ от API"
        await status_msg.edit_text(f"⚠️ Ошибка API Gemini. Детали: <code>{error_details}</code>", parse_mode="HTML")
        return

    try:
        data = json.loads(response.text.strip())
        amount, merchant = float(data.get("amount", 0)), str(data.get("merchant", "Неизвестно"))
        employee = await get_or_register_employee(message.from_user)

        pending_receipts[message.from_user.id] = {
            "amount": amount, "merchant": merchant, "category": data.get("category", "Прочее"),
            "description": data.get("description", ""), "user_name": employee, "file_unique_id": photo.file_unique_id,
        }

        duplicate = await check_for_duplicate_receipt(employee, amount, merchant)
        if duplicate:
            kb_dup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Да, это отдельный чек", callback_data="confirm_duplicate_ok")],
                [InlineKeyboardButton(text="❌ Отмена (это дубль)", callback_data="cancel_duplicate")],
            ])
            await status_msg.edit_text(
                "⚠️ <b>Внимание: похожий чек уже был добавлен сегодня!</b>\n\n"
                f"🕒 Время: {html.escape(duplicate['time'])}\n🎯 Проект: {html.escape(str(duplicate['project']))}\n"
                f"💵 Сумма: {duplicate['amount']:,.0f} ₸ ({html.escape(merchant)})\n\nЭто точно <b>еще одна</b> отдельная покупка?",
                reply_markup=kb_dup, parse_mode="HTML",
            )
            return

        await show_project_selection(status_msg, amount, merchant, data, employee)

    except Exception as e:
        logger.error("Ошибка обработки чека: %s", e)
        await status_msg.edit_text(f"⚠️ Ошибка разбора JSON. Детали: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "confirm_duplicate_ok")
async def cb_confirm_duplicate(callback: types.CallbackQuery):
    await callback.answer()
    receipt = pending_receipts.get(callback.from_user.id)
    if not receipt:
        await callback.answer("Данные чека устарели", show_alert=True)
        return
    await show_project_selection(callback, receipt["amount"], receipt["merchant"], receipt, receipt["user_name"])

@dp.callback_query(F.data == "cancel_duplicate")
async def cb_cancel_duplicate(callback: types.CallbackQuery):
    await callback.answer()
    pending_receipts.pop(callback.from_user.id, None)
    await callback.message.edit_text("❌ <b>Загрузка отменена.</b> Дубликат не попал в таблицу.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("proj_"))
async def process_project_choice(callback: types.CallbackQuery):
    await callback.answer()
    choice_code = callback.data.replace("proj_", "")
    user_id = callback.from_user.id
    receipt = pending_receipts.get(user_id)

    if not receipt:
        await callback.answer("Данные чека устарели.", show_alert=True)
        return

    projects = await get_active_projects()
    full_project = projects[int(choice_code)] if choice_code.isdigit() and int(choice_code) < len(projects) else "Склад / Общее"

    await callback.message.edit_reply_markup(reply_markup=None)
    status_update = await callback.message.answer("⏳ Записываю в таблицу...")

    try:
        await log_expense(
            project=full_project, category=receipt["category"], amount=receipt["amount"],
            employee=receipt["user_name"], comment=f"{receipt['merchant']}: {receipt['description']}", check_type="Чек в Telegram",
        )

        if receipt.get("file_unique_id"): saved_receipt_file_ids.add(receipt["file_unique_id"])
        pending_receipts.pop(user_id, None)
        await status_update.edit_text(
            f"✅ <b>Расход внесен в таблицу!</b>\n\n🎯 Проект: {html.escape(full_project)}\n"
            f"💵 Сумма: {receipt['amount']:,.0f} ₸\n👤 Сотрудник: {html.escape(receipt['user_name'])}", parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка сохранения расхода: %s", e)
        await status_update.edit_text(f"⚠️ Ошибка записи в Google Таблицу: <code>{html.escape(str(e))}</code>", parse_mode="HTML")


# ---------------------------------------------------------
# 11. СЕРВЕР И ТОЧКА ВХОДА (RENDER 24/7)
# ---------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is active 24/7!")

async def main():
    logger.info("Проверка доступных моделей Gemini...")
    available_models = await get_available_models()
    logger.info("Доступные модели: %s", ", ".join(available_models))

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("Сервер слушает порт %d, запускаем бота...", port)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())