import asyncio
from datetime import datetime
import io
import json
import os
import random
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
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from google.oauth2.service_account import Credentials
import gspread

# 1. Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

SPREADSHEET_ID = "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA"

# 2. Инициализация Telegram и Google AI
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_KEY)

# Модели ИИ
AUDIO_MODELS_CASCADE = [
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

IMAGE_MODELS_CASCADE = [
    "gemini-3.6-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

# 3. Подключение к Google Таблицам
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# Память состояний и кэш
employee_cache = {}  # Кэш сотрудников для устранения задержек
pending_receipts = {}
active_shifts = {}
waiting_for_requisites = {}
saved_receipt_file_ids = set()


class VoiceProjectState(StatesGroup):
  waiting_for_voice = State()
  editing_field = State()


NAV_BUTTONS = [
    "📸 Как отправить чек",
    "🍔 Обеденные (2 500 ₸)",
    "⏱ Моя смена",
    "💰 Мои чеки",
    "💳 Мои реквизиты",
    "💼 Панель выплат (Admin)",
]


# --- СИНХРОНИЗАЦИЯ СОТРУДНИКОВ С КЭШЕМ ---


def get_or_register_employee_sync(user: types.User) -> str:
  user_id_str = str(user.id).strip()
  if user_id_str in employee_cache:
    return employee_cache[user_id_str]

  username_str = f"@{user.username.lower().strip()}" if user.username else ""
  full_name = (user.full_name or "Новый сотрудник").strip()
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
          "",
          "",
          official_name,
          f"@{user.username}" if user.username else "-",
          user_id_str,
      ])

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

  except Exception as e:
    print(f"Ошибка синхронизации сотрудника: {e}")
    official_name = official_name or full_name

  employee_cache[user_id_str] = official_name
  return official_name


async def get_or_register_employee(user: types.User) -> str:
  user_id_str = str(user.id).strip()
  if user_id_str in employee_cache:
    return employee_cache[user_id_str]
  return await asyncio.to_thread(get_or_register_employee_sync, user)


def get_employee_requisites_sync(employee_name: str) -> str:
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    rows = ws_pay.get_all_values()
    for row in rows[1:]:
      if (
          len(row) >= 3
          and (
              employee_name.lower() in row[0].strip().lower()
              or row[0].strip().lower() in employee_name.lower()
          )
          and row[0].strip()
      ):
        reqs = row[2].strip()
        if reqs and reqs not in ["-", "#ERROR!"]:
          return reqs.lstrip("'")
        return "Реквизиты еще не указаны"
  except Exception as e:
    print(f"Ошибка чтения реквизитов: {e}")
  return "Реквизиты еще не указаны"


def save_employee_requisites_sync(employee_name: str, requisites_text: str):
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    col_names = ws_pay.col_values(1)

    matched_idx = None
    for idx, name in enumerate(col_names, start=1):
      if (
          employee_name.lower() in name.lower()
          or name.lower() in employee_name.lower()
      ) and name.strip():
        matched_idx = idx
        break

    if matched_idx:
      clean_text = requisites_text.strip()
      if clean_text.startswith(("+", "=")):
        clean_text = "'" + clean_text
      ws_pay.update_cell(matched_idx, 3, clean_text)
  except Exception as e:
    print(f"Ошибка сохранения реквизитов: {e}")


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
  keyboard = [
      [
          KeyboardButton(text="📸 Как отправить чек"),
          KeyboardButton(text="🍔 Обеденные (2 500 ₸)"),
      ],
      [
          KeyboardButton(text="⏱ Моя смена"),
          KeyboardButton(text="💰 Мои чеки"),
      ],
      [KeyboardButton(text="💳 Мои реквизиты")],
  ]
  if user_id in ADMIN_IDS:
    keyboard.append([KeyboardButton(text="💼 Панель выплат (Admin)")])

  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_active_projects_sync():
  try:
    ws = spreadsheet.worksheet("Проекты")
    rows = ws.get_all_records()
    active = [
        r.get("Проект")
        for r in rows
        if str(r.get("Статус проекта")).strip().lower() in ["в работе", "активен"]
    ]
    return active if active else ["Склад / Общее"]
  except Exception:
    return ["Склад / Общее"]


def log_expense_sync(
    project: str,
    category: str,
    amount: float,
    user: types.User,
    comment: str,
    check_type: str = "Норматив (без чека)",
):
  employee = get_or_register_employee_sync(user)
  ws = spreadsheet.worksheet("Операции")
  tx_id = f"TX-{random.randint(10000, 99999)}"
  now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

  ws.append_row([
      tx_id,
      now_str,
      "Расход",
      project,
      category,
      amount,
      employee,
      "К возмещению",
      check_type,
      comment,
  ])


def check_for_duplicate_receipt_sync(
    employee: str, amount: float, merchant: str
) -> dict | None:
  try:
    ws = spreadsheet.worksheet("Операции")
    rows = ws.get_all_records()
    today_str = datetime.now().strftime("%d.%m.%Y")

    for r in reversed(rows[-50:]):
      dt = str(r.get("Дата и время", ""))
      who = str(r.get("Кто оплатил", "")).strip().lower()
      comm = str(r.get("Комментарий", "")).lower()

      try:
        amt = float(
            str(r.get("Сумма (₸)", 0)).replace(" ", "").replace(",", ".")
        )
      except ValueError:
        amt = 0.0

      if (
          today_str in dt
          and (employee.lower() in who or who in employee.lower())
          and abs(amt - amount) < 0.01
      ):
        if (
            merchant.lower() in comm
            or not merchant
            or merchant == "Неизвестно"
        ):
          return {
              "time": dt.split(" ")[-1] if " " in dt else dt,
              "project": r.get("Проект", "-"),
              "amount": amt,
              "comment": r.get("Комментарий", "-"),
          }
  except Exception as e:
    print(f"Ошибка проверки дубликатов: {e}")
  return None


async def show_project_selection(
    target_msg, amount: float, merchant: str, data: dict, employee: str
):
  projects = await asyncio.to_thread(get_active_projects_sync)
  kb_buttons = [
      [InlineKeyboardButton(text=f"📌 {p}", callback_data=f"proj_{p[:25]}")]
      for p in projects
  ]
  if "Склад / Общее" not in projects:
    kb_buttons.append([
        InlineKeyboardButton(
            text="🏢 Склад / Общее", callback_data="proj_Склад / Общее"
        )
    ])

  text = (
      f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({merchant})\n"
      f"📂 {data.get('category', 'Прочее')} — {data.get('description', '')}\n"
      f"👤 Сотрудник: <b>{employee}</b>\n\n"
      "👉 <b>К какому проекту привязать покупку?</b>"
  )

  if isinstance(target_msg, types.Message):
    await target_msg.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
        parse_mode="HTML",
    )
  else:
    await target_msg.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
        parse_mode="HTML",
    )


def get_debts_summary_sync():
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


def get_user_pending_receipts_sync(user: types.User):
  employee_name = get_or_register_employee_sync(user)
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
      user_items.append(
          {"project": proj, "amount": amt, "comment": comm, "date": dt}
      )
  return employee_name, total, user_items


# --- ОБРАБОТЧИКИ КОМАНД И КНОПОК ---


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  name = await get_or_register_employee(message.from_user)
  kb = get_main_keyboard(message.from_user.id)
  reqs = await asyncio.to_thread(get_employee_requisites_sync, name)

  extra_text = ""
  if reqs == "Реквизиты еще не указаны":
    extra_text = (
        "\n\n💡 <b>Важно:</b> Нажмите кнопку «💳 Мои реквизиты» и укажите номер"
        " Kaspi / ИИН для быстрых переводов!"
    )

  await message.answer(
      f"Привет, {name}! 🎛️\n\n"
      "Я бот для учета расходов на площадках.\n"
      "• Отправь фото чека для распознавания\n"
      "• Жми «🍔 Обеденные» для суточных 2 500 ₸\n"
      "• Запускай «⏱ Моя смена» для учета времени на монтаже."
      f"{extra_text}",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.message(F.text == "📸 Как отправить чек")
async def msg_how_to(message: types.Message):
  await message.answer(
      "📷 <b>Как отправить чек:</b>\n\n"
      "1. Нажмите на скрепку и отправьте фото чека (или скриншот Kaspi / Яндекс"
      " Go).\n"
      "2. Gemini автоматически определит сумму и категорию расхода.\n"
      "3. Выберите проект кнопкой.\n\n"
      "Сумма сразу добавится к вашим выплатам!",
      parse_mode="HTML",
  )


@dp.message(F.text == "💰 Мои чеки")
async def msg_my_receipts(message: types.Message):
  status_wait = await message.answer("🔍 Проверяю ваши чеки в таблице...")
  name, total, items = await asyncio.to_thread(
      get_user_pending_receipts_sync, message.from_user
  )

  if not items:
    await status_wait.edit_text(
        f"👤 <b>{name}</b>\n\nУ вас нет активных чеков к возмещению. Все"
        " выплачено! 🎉",
        parse_mode="HTML",
    )
    return

  text = (
      f"👤 <b>Чеки к возмещению ({name})</b>\n\n"
      f"💰 <b>Общая сумма:</b> {total:,.0f} ₸\n\n"
      "<b>Список в обработке:</b>\n"
  )
  for it in items[:10]:
    text += (
        f"• {it['amount']:,.0f} ₸ — <i>{it['project']}</i> ({it['comment']})\n"
    )

  await status_wait.edit_text(text, parse_mode="HTML")


@dp.message(F.text == "💳 Мои реквизиты")
async def cmd_my_requisites(message: types.Message):
  employee = await get_or_register_employee(message.from_user)
  reqs = await asyncio.to_thread(get_employee_requisites_sync, employee)

  kb = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="✏️ Изменить / Ввести реквизиты", callback_data="edit_reqs"
          )
      ]]
  )
  await message.answer(
      f"💳 <b>Реквизиты для выплат:</b>\n\n"
      f"👤 <b>Сотрудник:</b> {employee}\n"
      f"📋 <b>Данные:</b>\n<code>{reqs}</code>\n\n"
      "<i>По этим реквизитам администратор переводит вам деньги за чеки и"
      " обеденные.</i>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_requisites(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  waiting_for_requisites[user_id] = True

  await callback.message.edit_text(
      "📝 <b>Введите ваши реквизиты одним сообщением:</b>\n\n"
      "Укажите номер перевода Kaspi, ИИН и номер карты.\n\n"
      "Пример:\n"
      "<code>+7 777 123 4567 (Kaspi)\nИИН: 990102350444\nКарта: 4400 4301 9876"
      " 5432</code>\n\n"
      "Отправьте данные текстом прямо в этот чат:",
      parse_mode="HTML",
  )


# --- ОБЕДЕННЫЕ И СМЕНЫ ---


@dp.message(F.text == "🍔 Обеденные (2 500 ₸)")
async def cmd_quick_meal(message: types.Message):
  projects = await asyncio.to_thread(get_active_projects_sync)
  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text=p, callback_data=f"meal_{p[:25]}")]
          for p in projects
      ]
  )
  await message.answer(
      "🍔 <b>Обеденные (2 500 ₸)</b>\nВыберите проект, на котором работаете:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("meal_"))
async def cb_confirm_meal(callback: types.CallbackQuery):
  await callback.answer("Записываю обеденные...")
  project_short = callback.data.replace("meal_", "")
  projects = await asyncio.to_thread(get_active_projects_sync)
  full_project = next(
      (p for p in projects if p.startswith(project_short)), "Склад / Общее"
  )

  user_id = callback.from_user.id
  if user_id in active_shifts:
    active_shifts[user_id]["claimed_meals"] += 1

  await asyncio.to_thread(
      log_expense_sync,
      full_project,
      "Питание команды",
      2500,
      callback.from_user,
      "Обеденные (быстрая выплата)",
  )

  await callback.message.edit_text(
      "✅ <b>Обеденные 2 500 ₸ начислены!</b>\n\n"
      f"🎯 Проект: {full_project}\n"
      "Сумма передана в таблицу к возмещению.",
      parse_mode="HTML",
  )


@dp.message(F.text == "⏱ Моя смена")
async def cmd_shift_menu(message: types.Message):
  user_id = message.from_user.id

  if user_id not in active_shifts:
    projects = await asyncio.to_thread(get_active_projects_sync)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🟢 Начать: {p}", callback_data=f"startshift_{p[:20]}"
                )
            ]
            for p in projects
        ]
    )
    await message.answer(
        "⏱ <b>Учет смены</b>\nУ вас нет активной смены. Выберите проект для"
        " старта:",
        reply_markup=kb,
        parse_mode="HTML",
    )
  else:
    shift = active_shifts[user_id]
    duration = datetime.now() - shift["start_time"]
    hours = int(duration.total_seconds() // 3600)
    minutes = int((duration.total_seconds() % 3600) // 60)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 Завершить смену", callback_data="end_shift"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🍔 Взять обед сейчас (2 500 ₸)",
                    callback_data=f"meal_{shift['project'][:25]}",
                )
            ],
        ]
    )
    await message.answer(
        "⏱ <b>Текущая смена в процессе</b>\n\n"
        f"🎯 Проект: <b>{shift['project']}</b>\n"
        f"⏳ Прошло: <b>{hours} ч. {minutes} мин.</b>\n"
        f"🍔 Взято обеденных: {shift['claimed_meals'] * 2500} ₸\n\n"
        "<i>Каждые полные 6 часов смены начисляют 2 500 ₸.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("startshift_"))
async def cb_start_shift(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  proj_prefix = callback.data.replace("startshift_", "")
  projects = await asyncio.to_thread(get_active_projects_sync)
  full_project = next(
      (p for p in projects if p.startswith(proj_prefix)), "Склад / Общее"
  )

  active_shifts[user_id] = {
      "project": full_project,
      "start_time": datetime.now(),
      "claimed_meals": 0,
  }

  await callback.message.edit_text(
      "🟢 <b>Смена начата!</b>\n\n"
      f"🎯 Проект: <b>{full_project}</b>\n"
      f"🕒 Время старта: {datetime.now().strftime('%H:%M')}\n\n"
      "При завершении смены бот рассчитает отработанные часы и начислит"
      " обеденные.",
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "end_shift")
async def cb_end_shift(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  if user_id not in active_shifts:
    await callback.answer("Смена не найдена", show_alert=True)
    return

  await callback.answer("Завершаю смену...")
  shift = active_shifts.pop(user_id)
  duration = datetime.now() - shift["start_time"]
  hours = duration.total_seconds() / 3600

  earned_meals_count = int(hours // 6)
  already_claimed = shift["claimed_meals"]
  remaining_meals = max(0, earned_meals_count - already_claimed)
  remaining_payout = remaining_meals * 2500

  if remaining_payout > 0:
    await asyncio.to_thread(
        log_expense_sync,
        shift["project"],
        "Питание команды",
        remaining_payout,
        callback.from_user,
        f"Обеденные за смену ({int(hours)} ч, остаток)",
    )

  msg = (
      "🔴 <b>Смена завершена!</b>\n\n"
      f"🎯 Проект: {shift['project']}\n"
      f"⏱ Отработано: {int(hours)} ч."
      f" {int((duration.total_seconds() % 3600) // 60)} мин.\n"
      f"🍔 Положено обедов: {earned_meals_count} × 2 500 ₸\n"
  )
  if remaining_payout > 0:
    msg += f"💰 Начислено к выплате: <b>{remaining_payout:,.0f} ₸</b>"
  else:
    msg += "👌 Все положенные обеденные уже были учтены."

  await callback.message.edit_text(msg, parse_mode="HTML")


# --- ПАНЕЛЬ ADMIN И ГОЛОС ---


@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    await message.answer("⛔️ Нет прав доступа.", parse_mode="HTML")
    return
  await render_admin_menu(message)


async def render_admin_menu(event_target):
  debts = await asyncio.to_thread(get_debts_summary_sync)
  total_debt = sum(debts.values()) if debts else 0

  text = (
      "💼 <b>Панель управления (Admin)</b>\n\n"
      f"💵 <b>Текущий долг перед командой:</b> {total_debt:,.0f} ₸\n\n"
      "Выберите действие:"
  )

  buttons = [
      [
          InlineKeyboardButton(
              text="🎙 Надиктовать проект", callback_data="admin_voice_project"
          )
      ],
      [
          InlineKeyboardButton(
              text=f"💸 Выплаты команде ({len(debts)})",
              callback_data="admin_payouts_list",
          )
      ],
      [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_hub")],
  ]

  kb = InlineKeyboardMarkup(inline_keyboard=buttons)
  if isinstance(event_target, types.Message):
    await event_target.answer(text, reply_markup=kb, parse_mode="HTML")
  else:
    await event_target.message.edit_text(
        text, reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_hub")
async def cb_admin_hub(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return
  await state.clear()
  await render_admin_menu(callback)


@dp.callback_query(F.data == "admin_voice_project")
async def cb_start_voice_project(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  await state.set_state(VoiceProjectState.waiting_for_voice)
  await callback.message.edit_text(
      "🎙 <b>Зажмите микрофон и надиктуйте проект:</b>\n\n"
      "Назовите дату, мероприятие, локацию, сумму сметы и предоплату.\n\n"
      "<i>Пример:\n«Свадьба Азамата 15 октября, отель Sheraton, смета полтора"
      " миллиона, предоплата 50%»</i>\n\n"
      "Жду голосовое сообщение...",
      parse_mode="HTML",
  )


async def render_project_card(target, data: dict):
  transcript = data.get("transcript", "")
  event_date = data.get("event_date", "-")
  proj_name = data.get("project_name", "-")
  location = data.get("location", "-")
  price = float(data.get("price", 0))
  prepay_pct = int(data.get("prepay_percent", 0))
  paid_preview = price * (prepay_pct / 100.0)

  transcript_block = f"🗣 <i>«{transcript}»</i>\n\n" if transcript else ""

  text = (
      "📋 <b>Проверьте данные проекта:</b>\n\n"
      f"{transcript_block}"
      f"📅 <b>Дата мероприятия:</b> {event_date}\n"
      f"🎯 <b>Название:</b> {proj_name}\n"
      f"📍 <b>Локация / Площадка:</b> {location}\n"
      f"💵 <b>Смета договора:</b> {price:,.0f} ₸\n"
      f"💰 <b>Предоплата:</b> {prepay_pct}% ({paid_preview:,.0f} ₸)\n\n"
      "Всё верно?"
  )

  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Все верно, создать!",
                  callback_data="confirm_voice_proj",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎙 Наговорить заново",
                  callback_data="admin_voice_project",
              ),
              InlineKeyboardButton(
                  text="✏️ Исправить поле",
                  callback_data="edit_voice_fields_menu",
              ),
          ],
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hub")],
      ]
  )

  if isinstance(target, types.Message):
    await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
  else:
    await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(VoiceProjectState.waiting_for_voice, F.voice)
async def process_voice_project(message: types.Message, state: FSMContext):
  status_msg = await message.answer("🎧 Вслушиваюсь в голосовое...")

  voice = message.voice
  file_io = io.BytesIO()
  await bot.download(voice, destination=file_io)
  audio_bytes = file_io.getvalue()

  prompt = """
    Ты — экспертный финансовый ассистент компании по аренде сценического оборудования.
    Внимательно прослушай голосовое сообщение администратора о новом мероприятии/проекте.

    ИНСТРУКЦИИ:
    1. Сначала сделай точную текстовую транскрипцию всего, что услышал, в поле "transcript".
    2. Переведи словесные числа в цифры:
       - "полтора миллиона" / "полтора ляма" -> 1500000
       - "пятьсот тысяч" / "полмиллиона" -> 500000
       - "два миллиона триста тысяч" -> 2300000
       - "пятьдесят тысяч" -> 50000
    3. Выдели дату мероприятия:
       - "двадцать пятое сентября" -> "25.09"
       - "пятнадцатое октября" -> "15.10"
       - "первое ноября" -> "01.11"
    4. Предоплата:
       - если сказали "аванс 50%", "предоплата половина" -> 50
       - если сказали "полная оплата", "100%", "оплатили всё" -> 100
       - если не упомянули или сказали "без предоплаты" -> 0

    Верни СТРОГО JSON:
    {
      "transcript": "текст",
      "event_date": "дата ДД.ММ",
      "project_name": "название БЕЗ даты",
      "location": "площадка",
      "price": сумма числом,
      "prepay_percent": 0, 50 или 100
    }
    """

  response = None
  for model_name in AUDIO_MODELS_CASCADE:
    try:
      resp = ai_client.models.generate_content(
          model=model_name,
          contents=[
              genai_types.Part.from_bytes(
                  data=audio_bytes, mime_type="audio/ogg"
              ),
              prompt,
          ],
          config=genai_types.GenerateContentConfig(
              response_mime_type="application/json",
              temperature=0.1,
          ),
      )
      if resp and resp.text:
        response = resp
        break
    except Exception as e:
      print(f"Модель {model_name} пропущена ({e})...")
      continue

  if not response or not response.text:
    await status_msg.edit_text("⚠️ Не удалось разобрать аудио. Попробуйте еще.")
    return

  try:
    data = json.loads(response.text.strip())
    await state.update_data(
        transcript=data.get("transcript", ""),
        event_date=data.get("event_date", datetime.now().strftime("%d.%m")),
        project_name=data.get("project_name", "Мероприятие"),
        location=data.get("location", "Площадка"),
        price=float(data.get("price", 0)),
        prepay_percent=int(data.get("prepay_percent", 0)),
    )
    await render_project_card(status_msg, await state.get_data())
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка: {e}")


@dp.callback_query(F.data == "edit_voice_fields_menu")
async def cb_edit_fields_menu(callback: types.CallbackQuery):
  await callback.answer()
  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="📅 Дату", callback_data="field_event_date"
              ),
              InlineKeyboardButton(
                  text="🎯 Название", callback_data="field_project_name"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="📍 Локацию", callback_data="field_location"
              ),
              InlineKeyboardButton(
                  text="💵 Смету", callback_data="field_price"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="💰 Предоплату (%)", callback_data="field_prepay_percent"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к карточке", callback_data="back_to_card"
              )
          ],
      ]
  )
  await callback.message.edit_text(
      "✏️ <b>Выберите, какое поле нужно исправить:</b>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("field_"))
async def cb_select_field_to_edit(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  field_name = callback.data.replace("field_", "")
  await state.update_data(editing_target=field_name)
  await state.set_state(VoiceProjectState.editing_field)

  prompts = {
      "event_date": "Введите правильную <b>дату мероприятия</b> (25.09):",
      "project_name": "Введите правильное <b>название проекта</b>:",
      "location": "Введите правильную <b>локацию / площадку</b>:",
      "price": "Введите правильную <b>сумму сметы числом</b> (1500000):",
      "prepay_percent": "Введите процент предоплаты <b>(0, 50 или 100)</b>:",
  }
  await callback.message.edit_text(
      prompts.get(field_name, "Введите новое значение:"), parse_mode="HTML"
  )


@dp.message(VoiceProjectState.editing_field, F.text)
async def process_field_edit_text(message: types.Message, state: FSMContext):
  data = await state.get_data()
  field = data.get("editing_target")
  text_val = message.text.strip()

  if field == "price":
    try:
      val = float(text_val.replace(" ", "").replace(",", "."))
      await state.update_data(price=val)
    except ValueError:
      await message.answer("⚠️ Введите число:")
      return
  elif field == "prepay_percent":
    try:
      val = int(text_val.replace("%", "").strip())
      await state.update_data(prepay_percent=val)
    except ValueError:
      await message.answer("⚠️ Введите 0, 50 или 100:")
      return
  elif field in ["event_date", "project_name", "location"]:
    await state.update_data({field: text_val})

  await state.set_state(VoiceProjectState.waiting_for_voice)
  updated_data = await state.get_data()
  msg_wait = await message.answer("🔄 Обновляю...")
  await render_project_card(msg_wait, updated_data)


@dp.callback_query(F.data == "back_to_card")
async def cb_back_to_card(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  data = await state.get_data()
  await render_project_card(callback, data)


def add_project_to_sheets_sync(data: dict):
  ws_proj = spreadsheet.worksheet("Проекты")
  next_row = len(ws_proj.col_values(1)) + 1

  event_date = data.get("event_date", "").strip()
  raw_name = data.get("project_name", "").strip()

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
          full_proj_name,
          event_date,
          data.get("location", "Площадка"),
          "В работе",
          total_price,
          formula_expenses,
          formula_profit,
          formula_margin,
      ],
      value_input_option="USER_ENTERED",
  )

  if paid_amount > 0:
    ws_ops = spreadsheet.worksheet("Операции")
    tx_id = f"TX-{random.randint(10000, 99999)}"
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    ws_ops.append_row([
        tx_id,
        now_str,
        "Доход",
        full_proj_name,
        "Комплексный продакшн",
        paid_amount,
        "Клиент",
        "Не требуется",
        "-",
        f"Предоплата ({prepay_pct}%)",
    ])

  return full_proj_name, total_price, paid_amount


@dp.callback_query(F.data == "confirm_voice_proj")
async def cb_confirm_voice_proj(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  data = await state.get_data()
  if not data or "project_name" not in data:
    await callback.answer("Данные устарели", show_alert=True)
    return

  await callback.message.edit_text("⏳ Записываю проект в Google Таблицу...")

  try:
    full_proj_name, total_price, paid_amount = await asyncio.to_thread(
        add_project_to_sheets_sync, data
    )
    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⬅️ В панель управления", callback_data="admin_hub"
            )
        ]]
    )

    await callback.message.edit_text(
        f"✅ <b>Мероприятие создано!</b>\n\n"
        f"🎯 <b>{full_proj_name}</b>\n"
        f"📅 Дата: {data.get('event_date', '-')}\n"
        f"📍 Локация: {data.get('location', 'Площадка')}\n"
        f"💵 Смета: {total_price:,.0f} ₸\n"
        f"💰 Предоплата: {paid_amount:,.0f} ₸",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await callback.message.edit_text(f"⚠️ Ошибка записи: {e}")


# --- ВЫПЛАТЫ КОМАНДЕ В АДМИНКЕ ---


@dp.callback_query(F.data == "admin_payouts_list")
async def cb_admin_payouts_list(callback: types.CallbackQuery):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  debts = await asyncio.to_thread(get_debts_summary_sync)
  buttons = []

  if not debts:
    text = (
        "💼 <b>Выплаты команде</b>\n\n"
        "🎉 <b>Все долги закрыты!</b> Нет расходов к возмещению."
    )
  else:
    total_debt = sum(debts.values())
    text = (
        "💼 <b>Выплаты команде к возмещению</b>\n\n"
        f"💵 <b>Общий долг:</b> {total_debt:,.0f} ₸\n\n"
        "<b>По сотрудникам:</b>\n"
    )
    for person, sum_amt in debts.items():
      text += f"• <b>{person}</b>: {sum_amt:,.0f} ₸\n"
      buttons.append([
          InlineKeyboardButton(
              text=f"💸 Погасить: {person} ({sum_amt:,.0f} ₸)",
              callback_data=f"pay_{person[:25]}",
          )
      ])

  buttons.append(
      [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="admin_hub")]
  )
  kb = InlineKeyboardMarkup(inline_keyboard=buttons)
  await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay_person_preview(callback: types.CallbackQuery):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  person_prefix = callback.data.replace("pay_", "")
  debts = await asyncio.to_thread(get_debts_summary_sync)

  full_person_name = next(
      (p for p in debts if p.startswith(person_prefix)), person_prefix
  )
  amount = debts.get(full_person_name, 0.0)
  reqs = await asyncio.to_thread(get_employee_requisites_sync, full_person_name)

  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=f"✅ Переведено {amount:,.0f} ₸ (Списать долг)",
                  callback_data=f"confirmpay_{person_prefix[:20]}",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="admin_payouts_list"
              )
          ],
      ]
  )

  await callback.message.edit_text(
      f"💼 <b>Окно выплаты сотруднику</b>\n\n"
      f"👤 <b>Сотрудник:</b> {full_person_name}\n"
      f"💵 <b>Сумма к переводу:</b> <code>{amount:,.0f}</code> ₸\n\n"
      f"📋 <b>Реквизиты для Kaspi / Банка:</b>\n"
      f"<code>{reqs}</code>",
      reply_markup=kb,
      parse_mode="HTML",
  )


def close_debt_sync(person_prefix: str):
  ws = spreadsheet.worksheet("Операции")
  all_values = ws.get_all_values()
  if not all_values:
    return

  headers = [str(h).strip().lower() for h in all_values[0]]
  status_col_idx = 8
  who_col_idx = 7

  for idx, h in enumerate(headers, start=1):
    if "статус" in h:
      status_col_idx = idx
    elif "кто" in h or "сотрудник" in h:
      who_col_idx = idx

  for row_idx, row in enumerate(all_values[1:], start=2):
    if len(row) >= max(status_col_idx, who_col_idx):
      who_val = row[who_col_idx - 1].strip()
      status_val = row[status_col_idx - 1].strip().lower()

      if (
          person_prefix.lower() in who_val.lower()
          or who_val.lower() in person_prefix.lower()
      ) and "к возмещению" in status_val:
        ws.update_cell(row_idx, status_col_idx, "Выплачено")


@dp.callback_query(F.data.startswith("confirmpay_"))
async def cb_confirm_payment(callback: types.CallbackQuery):
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  await callback.answer("Списываю долг в таблице...")
  person_prefix = callback.data.replace("confirmpay_", "")
  await asyncio.to_thread(close_debt_sync, person_prefix)
  await render_admin_menu(callback)


# --- ПЕРЕХВАТ ВВОДА РЕКВИЗИТОВ ТЕКСТОМ ---


@dp.message(
    StateFilter(None),
    F.text & ~F.text.startswith("/") & ~F.text.in_(NAV_BUTTONS),
)
async def handle_user_text_input(message: types.Message):
  user_id = message.from_user.id
  if user_id in waiting_for_requisites:
    del waiting_for_requisites[user_id]
    employee = await get_or_register_employee(message.from_user)
    await asyncio.to_thread(
        save_employee_requisites_sync, employee, message.text.strip()
    )

    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n\n"
        f"👤 {employee}\n"
        f"📋 <b>Ваши данные:</b>\n<code>{message.text.strip()}</code>",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )


# --- РАСПОЗНАВАНИЕ ЧЕКОВ (ФОТО) ---


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  photo = message.photo[-1]

  if photo.file_unique_id in saved_receipt_file_ids:
    await message.answer(
        "⚠️ <b>Этот чек уже был успешно внесен ранее!</b>", parse_mode="HTML"
    )
    return

  status_msg = await message.answer("🔍 Распознаю чек (3.6)...")

  file_io = io.BytesIO()
  await bot.download(photo, destination=file_io)
  image_bytes = file_io.getvalue()

  prompt = """
    Ты финансовый сканер для компании по прокату сценического оборудования.
    Изучи чек и верни СТРОГО чистый JSON:
    {
      "amount": итоговая сумма числом (например: 4500),
      "merchant": продавец (Яндекс Go, Magnum, Додо Пицца, АЗС и т.п.),
      "category": одна из категорий: "Такси / Логистика / ГСМ", "Питание команды", "Расходники (тейп, батарейки)", "Субаренда оборудования", "Прочее",
      "description": суть покупки (2-4 слова)
    }
    """

  response = None
  for model_name in IMAGE_MODELS_CASCADE:
    try:
      resp = ai_client.models.generate_content(
          model=model_name,
          contents=[
              genai_types.Part.from_bytes(
                  data=image_bytes, mime_type="image/jpeg"
              ),
              prompt,
          ],
          config=genai_types.GenerateContentConfig(
              response_mime_type="application/json",
              temperature=0.1,
          ),
      )
      if resp and resp.text:
        response = resp
        break
    except Exception as e:
      print(f"Модель {model_name} временно недоступна ({e})...")
      continue

  if not response or not response.text:
    await status_msg.edit_text(
        "⚠️ Серверы ИИ перегружены. Отправьте чек еще раз."
    )
    return

  try:
    data = json.loads(response.text.strip())
    amount = data.get("amount", 0)
    merchant = data.get("merchant", "Неизвестно")

    employee = await get_or_register_employee(message.from_user)

    pending_receipts[message.from_user.id] = {
        "amount": amount,
        "merchant": merchant,
        "category": data.get("category", "Прочее"),
        "description": data.get("description", ""),
        "user_name": employee,
        "file_unique_id": photo.file_unique_id,
    }

    duplicate = await asyncio.to_thread(
        check_for_duplicate_receipt_sync, employee, float(amount), merchant
    )
    if duplicate:
      kb_dup = InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="➕ Да, это отдельный чек",
                      callback_data="confirm_duplicate_ok",
                  )
              ],
              [
                  InlineKeyboardButton(
                      text="❌ Отмена (это дубль)",
                      callback_data="cancel_duplicate",
                  )
              ],
          ]
      )
      await status_msg.edit_text(
          "⚠️ <b>Внимание: похожий чек уже был добавлен сегодня!</b>\n\n"
          f"🕒 Время: {duplicate['time']}\n"
          f"🎯 Проект: {duplicate['project']}\n"
          f"💵 Сумма: {duplicate['amount']:,.0f} ₸ ({merchant})\n\n"
          "Это точно отдельная покупка?",
          reply_markup=kb_dup,
          parse_mode="HTML",
      )
      return

    await show_project_selection(status_msg, amount, merchant, data, employee)

  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка: {e}")


@dp.callback_query(F.data == "confirm_duplicate_ok")
async def cb_confirm_duplicate(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)
  if not receipt:
    await callback.answer("Данные устарели", show_alert=True)
    return

  await show_project_selection(
      callback,
      receipt["amount"],
      receipt["merchant"],
      receipt,
      receipt["user_name"],
  )


@dp.callback_query(F.data == "cancel_duplicate")
async def cb_cancel_duplicate(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  if user_id in pending_receipts:
    del pending_receipts[user_id]
  await callback.message.edit_text(
      "❌ <b>Загрузка отменена.</b>", parse_mode="HTML"
  )


@dp.callback_query(F.data.startswith("proj_"))
async def process_project_choice(callback: types.CallbackQuery):
  await callback.answer("Записываю чек...")
  project_short = callback.data.replace("proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)

  if not receipt:
    await callback.answer("Данные устарели.", show_alert=True)
    return

  projects = await asyncio.to_thread(get_active_projects_sync)
  full_project = next(
      (p for p in projects if p.startswith(project_short)), "Склад / Общее"
  )

  await callback.message.edit_reply_markup(reply_markup=None)
  status_update = await callback.message.answer("⏳ Записываю в таблицу...")

  try:
    await asyncio.to_thread(
        log_expense_sync,
        full_project,
        receipt["category"],
        float(receipt["amount"]),
        callback.from_user,
        f"{receipt['merchant']}: {receipt['description']}",
        "Чек в Telegram",
    )

    if receipt.get("file_unique_id"):
      saved_receipt_file_ids.add(receipt["file_unique_id"])

    del pending_receipts[user_id]
    await status_update.edit_text(
        "✅ <b>Расход внесен в таблицу!</b>\n\n"
        f"🎯 Проект: {full_project}\n"
        f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
        f"👤 Сотрудник: {receipt['user_name']}",
        parse_mode="HTML",
    )
  except Exception as e:
    await status_update.edit_text(f"⚠️ Ошибка записи: {e}")


# --- ВЕБ-СЕРВЕР И ТОЧКА ВХОДА ДЛЯ RENDER ---


async def handle_ping(request):
  return web.Response(text="Bot is active 24/7!")


async def main():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 8080))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()

  print(f"Сервер слушает порт {port}, запускаем бота...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())