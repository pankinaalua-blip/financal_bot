import asyncio
from datetime import datetime
import io
import json
import os
import random
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
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

# Сверхбыстрый каскад моделей
AI_MODELS_CASCADE = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
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

# Память состояний
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


# --- АВТОРЕГИСТРАЦИЯ И СИНХРОНИЗАЦИЯ СОТРУДНИКОВ ---


def get_or_register_employee(user: types.User) -> str:
  user_id_str = str(user.id).strip()
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
      print(f"➕ Новый сотрудник '{official_name}' добавлен в 'Справочники'")
  except Exception as e:
    print(f"Ошибка чтения 'Справочников': {e}")
    official_name = full_name

  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    col_names = ws_pay.col_values(1)

    if official_name not in col_names:
      next_row = len(col_names) + 1
      # Разделитель аргументов строго точка с запятой (;)
      formula_debt = f'=SUMIFS(Операции!F:F; Операции!G:G; A{next_row}; Операции!H:H; "К возмещению")'
      formula_paid = f'=SUMIFS(Операции!F:F; Операции!G:G; A{next_row}; Операции!H:H; "Выплачено")'

      ws_pay.append_row(
          [official_name, "Команда", "-", formula_debt, formula_paid],
          value_input_option="USER_ENTERED",
      )
      print(
          f"➕ Сотрудник '{official_name}' добавлен в 'Выплаты команде' с формулами"
      )
  except Exception as e:
    print(f"Ошибка проверки 'Выплат': {e}")

  return official_name


def get_employee_requisites(employee_name: str) -> str:
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


def save_employee_requisites(employee_name: str, requisites_text: str):
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
      print(f"✅ Реквизиты для {employee_name} сохранены в строку {matched_idx}")
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


def get_active_projects():
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


def log_expense(
    project: str,
    category: str,
    amount: float,
    user: types.User,
    comment: str,
    check_type: str = "Норматив (без чека)",
):
  employee = get_or_register_employee(user)
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


def check_for_duplicate_receipt(
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
  projects = get_active_projects()
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


def get_debts_summary():
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


def get_user_pending_receipts(user: types.User):
  employee_name = get_or_register_employee(user)
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


# --- ОБРАБОТЧИКИ МЕНЮ И КОМАНД ---


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  name = get_or_register_employee(message.from_user)
  kb = get_main_keyboard(message.from_user.id)
  reqs = get_employee_requisites(name)

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
  name, total, items = get_user_pending_receipts(message.from_user)

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


# --- РАБОТА С РЕКВИЗИТАМИ ---


@dp.message(F.text == "💳 Мои реквизиты")
async def cmd_my_requisites(message: types.Message):
  employee = get_or_register_employee(message.from_user)
  reqs = get_employee_requisites(employee)

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


# --- ОБЕДЕННЫЕ И ТАЙМ-ТРЕКЕР СМЕН ---


@dp.message(F.text == "🍔 Обеденные (2 500 ₸)")
async def cmd_quick_meal(message: types.Message):
  projects = get_active_projects()
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
  project_short = callback.data.replace("meal_", "")
  full_project = next(
      (p for p in get_active_projects() if p.startswith(project_short)),
      "Склад / Общее",
  )

  user_id = callback.from_user.id
  if user_id in active_shifts:
    active_shifts[user_id]["claimed_meals"] += 1

  log_expense(
      project=full_project,
      category="Питание команды",
      amount=2500,
      user=callback.from_user,
      comment="Обеденные (быстрая выплата)",
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
    projects = get_active_projects()
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
  user_id = callback.from_user.id
  proj_prefix = callback.data.replace("startshift_", "")
  full_project = next(
      (p for p in get_active_projects() if p.startswith(proj_prefix)),
      "Склад / Общее",
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

  shift = active_shifts.pop(user_id)
  duration = datetime.now() - shift["start_time"]
  hours = duration.total_seconds() / 3600

  earned_meals_count = int(hours // 6)
  already_claimed = shift["claimed_meals"]
  remaining_meals = max(0, earned_meals_count - already_claimed)
  remaining_payout = remaining_meals * 2500

  if remaining_payout > 0:
    log_expense(
        project=shift["project"],
        category="Питание команды",
        amount=remaining_payout,
        user=callback.from_user,
        comment=f"Обеденные за смену ({int(hours)} ч, остаток)",
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


# --- ПАНЕЛЬ УПРАВЛЕНИЯ (ADMIN) И ГОЛОСОВОЙ ВВОД С ДАТОЙ ---


@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    await message.answer("⛔️ Нет прав доступа.", parse_mode="HTML")
    return
  await render_admin_menu(message)


async def render_admin_menu(event_target):
  debts = get_debts_summary()
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
  if callback.from_user.id not in ADMIN_IDS:
    return
  await state.clear()
  await render_admin_menu(callback)


@dp.callback_query(F.data == "admin_voice_project")
async def cb_start_voice_project(
    callback: types.CallbackQuery, state: FSMContext
):
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  await state.set_state(VoiceProjectState.waiting_for_voice)
  await callback.message.edit_text(
      "🎙 <b>Зажмите микрофон и надиктуйте проект:</b>\n\n"
      "Назовите дату, мероприятие, локацию, сумму сметы и предоплату.\n\n"
      "<i>Пример:\n«Концерт Баста 25 сентября, площадка Дворец Спорта, смета"
      " полтора миллиона, предоплата 50%»</i>\n\n"
      "Жду голосовое сообщение...",
      parse_mode="HTML",
  )


async def render_project_card(target, data: dict):
  event_date = data.get("event_date", "-")
  proj_name = data.get("project_name", "-")
  location = data.get("location", "-")
  price = float(data.get("price", 0))
  prepay_pct = int(data.get("prepay_percent", 0))
  paid_preview = price * (prepay_pct / 100.0)

  text = (
      "📋 <b>Проверьте данные проекта:</b>\n\n"
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
  status_msg = await message.answer("🎧 Распознаю проект (1–2 сек)...")

  voice = message.voice
  file_io = io.BytesIO()
  await bot.download(voice, destination=file_io)
  audio_bytes = file_io.getvalue()

  prompt = """
    Ты финансовый ассистент компании по аренде сценического оборудования.
    Послушай аудиозапись администратора о новом мероприятии/проекте.
    Верни JSON строго следующего формата:
    {
      "event_date": "дата мероприятия в коротком формате (например: 25.09, 15.10 или 20.11)",
      "project_name": "название события, артиста или заказчика БЕЗ даты (например: Концерт Баста, Свадьба Азамата, Форум Digital)",
      "location": "площадка / отель / локация (например: Отель Sheraton, Дворец Спорта, Склад, Rixos)",
      "price": общая сумма договора числом (например: 1500000),
      "prepay_percent": 0, 50 или 100
    }
    """

  response = None
  for model_name in AI_MODELS_CASCADE:
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
      print(f"Ошибка аудио на {model_name}: {e}")
      continue

  if not response or not response.text:
    await status_msg.edit_text(
        "⚠️ Не удалось быстро разобрать аудио. Попробуйте наговорить еще раз."
    )
    return

  try:
    data = json.loads(response.text.strip())
    event_date = data.get("event_date", datetime.now().strftime("%d.%m"))
    proj_name = data.get("project_name", "Мероприятие")
    location = data.get("location", "Площадка")
    price = float(data.get("price", 0))
    prepay_pct = int(data.get("prepay_percent", 0))

    await state.update_data(
        event_date=event_date,
        project_name=proj_name,
        location=location,
        price=price,
        prepay_percent=prepay_pct,
    )

    await render_project_card(status_msg, await state.get_data())

  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора аудио: {e}")


@dp.callback_query(F.data == "edit_voice_fields_menu")
async def cb_edit_fields_menu(callback: types.CallbackQuery):
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
  field_name = callback.data.replace("field_", "")
  await state.update_data(editing_target=field_name)
  await state.set_state(VoiceProjectState.editing_field)

  prompts = {
      "event_date": (
          "Введите правильную <b>дату мероприятия</b> (например:"
          " <code>25.09</code> или <code>15.10</code>):"
      ),
      "project_name": (
          "Введите правильное <b>название проекта</b> (например: <code>Концерт"
          " Баста</code>):"
      ),
      "location": (
          "Введите правильную <b>локацию / площадку</b> (например: <code>Отель"
          " Sheraton</code>):"
      ),
      "price": (
          "Введите правильную <b>сумму сметы числом</b> (например:"
          " <code>1500000</code>):"
      ),
      "prepay_percent": (
          "Введите процент предоплаты числом <b>(0, 50 или 100)</b>:"
      ),
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
      await message.answer(
          "⚠️ Введите сумму числом (например: <code>1500000</code>):",
          parse_mode="HTML",
      )
      return
  elif field == "prepay_percent":
    try:
      val = int(text_val.replace("%", "").strip())
      await state.update_data(prepay_percent=val)
    except ValueError:
      await message.answer(
          "⚠️ Введите 0, 50 или 100:",
          parse_mode="HTML",
      )
      return
  elif field in ["event_date", "project_name", "location"]:
    await state.update_data({field: text_val})

  await state.set_state(VoiceProjectState.waiting_for_voice)
  updated_data = await state.get_data()
  msg_wait = await message.answer("🔄 Обновляю данные карточки...")
  await render_project_card(msg_wait, updated_data)


@dp.callback_query(F.data == "back_to_card")
async def cb_back_to_card(callback: types.CallbackQuery, state: FSMContext):
  data = await state.get_data()
  await render_project_card(callback, data)


@dp.callback_query(F.data == "confirm_voice_proj")
async def cb_confirm_voice_proj(
    callback: types.CallbackQuery, state: FSMContext
):
  data = await state.get_data()
  if not data or "project_name" not in data:
    await callback.answer(
        "Данные устарели, надиктуйте заново.", show_alert=True
    )
    return

  await callback.message.edit_text("⏳ Записываю проект в Google Таблицу...")

  try:
    ws_proj = spreadsheet.worksheet("Проекты")
    next_row = len(ws_proj.col_values(1)) + 1

    event_date = data.get("event_date", "").strip()
    raw_name = data.get("project_name", "").strip()

    # В названии проекта для кнопок монтажников оставляем дату
    if event_date and not raw_name.startswith(event_date):
      full_proj_name = f"{event_date} {raw_name}"
    else:
      full_proj_name = raw_name

    total_price = float(data.get("price", 0))
    prepay_pct = int(data.get("prepay_percent", 0))
    paid_amount = total_price * (prepay_pct / 100.0)

    # ВНИМАНИЕ: Формулы с точкой с запятой (;) и сдвигом на новую колонку B:
    # E = Доход, F = Расходы, G = Прибыль, H = Маржа
    formula_expenses = f'=SUMIFS(Операции!F:F; Операции!D:D; A{next_row}; Операции!C:C; "Расход")'
    formula_profit = f"=E{next_row}-F{next_row}"
    formula_margin = f"=IF(E{next_row}>0; G{next_row}/E{next_row}; 0)"

    # Запись в 8 колонок листа "Проекты":
    # A: Проект, B: Дата мероприятия, C: Локация / Площадка, D: Статус проекта,
    # E: Доход, F: Расходы, G: Прибыль, H: Маржа
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
        f"📅 Дата: {event_date}\n"
        f"📍 Локация: {data.get('location', 'Площадка')}\n"
        f"💵 Смета: {total_price:,.0f} ₸\n"
        f"💰 Предоплата: {paid_amount:,.0f} ₸\n\n"
        "<i>Проект внесен в таблицу без ошибок и доступен команде!</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await callback.message.edit_text(f"⚠️ Ошибка записи: {e}")


# --- ВЫПЛАТЫ КОМАНДЕ В АДМИНКЕ ---


@dp.callback_query(F.data == "admin_payouts_list")
async def cb_admin_payouts_list(callback: types.CallbackQuery):
  if callback.from_user.id not in ADMIN_IDS:
    return

  debts = get_debts_summary()
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
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  person_prefix = callback.data.replace("pay_", "")
  debts = get_debts_summary()

  full_person_name = next(
      (p for p in debts if p.startswith(person_prefix)), person_prefix
  )
  amount = debts.get(full_person_name, 0.0)
  reqs = get_employee_requisites(full_person_name)

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
      f"<code>{reqs}</code>\n\n"
      "<i>1. Скопируйте данные и сделайте перевод в Kaspi.\n"
      "2. Нажмите зеленую кнопку подтверждения ниже, чтобы закрыть долг в"
      " таблице.</i>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("confirmpay_"))
async def cb_confirm_payment(callback: types.CallbackQuery):
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  person_prefix = callback.data.replace("confirmpay_", "")
  await callback.answer("Списываю долг в таблице...", show_alert=False)

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

  await render_admin_menu(callback)


# --- ПЕРЕХВАТ ВВОДА РЕКВИЗИТОВ ТЕКСТОМ ---


@dp.message(F.text & ~F.text.startswith("/") & ~F.text.in_(NAV_BUTTONS))
async def handle_user_text_input(message: types.Message):
  user_id = message.from_user.id
  if user_id in waiting_for_requisites:
    del waiting_for_requisites[user_id]
    employee = get_or_register_employee(message.from_user)
    save_employee_requisites(employee, message.text.strip())

    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n\n"
        f"👤 {employee}\n"
        f"📋 <b>Ваши данные:</b>\n<code>{message.text.strip()}</code>\n\n"
        "Теперь при выплатах администратор будет сразу видеть эти данные.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )


# --- РАСПОЗНАВАНИЕ ЧЕКОВ, КАСКАД ИИ И ЗАЩИТА ОТ ДУБЛИКАТОВ ---


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  photo = message.photo[-1]

  if photo.file_unique_id in saved_receipt_file_ids:
    await message.answer(
        "⚠️ <b>Этот чек уже был успешно внесен в таблицу ранее!</b> Повторная"
        " запись отменена.",
        parse_mode="HTML",
    )
    return

  status_msg = await message.answer("🔍 Распознаю чек...")

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
  for model_name in AI_MODELS_CASCADE:
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
        "⚠️ Серверы ИИ сейчас временно перегружены. Пожалуйста, отправьте чек"
        " еще раз через пару секунд."
    )
    return

  try:
    data = json.loads(response.text.strip())
    amount = data.get("amount", 0)
    merchant = data.get("merchant", "Неизвестно")

    employee = get_or_register_employee(message.from_user)

    pending_receipts[message.from_user.id] = {
        "amount": amount,
        "merchant": merchant,
        "category": data.get("category", "Прочее"),
        "description": data.get("description", ""),
        "user_name": employee,
        "file_unique_id": photo.file_unique_id,
    }

    duplicate = check_for_duplicate_receipt(employee, float(amount), merchant)
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
          "Это точно <b>еще одна</b> отдельная покупка?",
          reply_markup=kb_dup,
          parse_mode="HTML",
      )
      return

    await show_project_selection(status_msg, amount, merchant, data, employee)

  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора чека: {e}")


@dp.callback_query(F.data == "confirm_duplicate_ok")
async def cb_confirm_duplicate(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)
  if not receipt:
    await callback.answer("Данные чека устарели", show_alert=True)
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
  user_id = callback.from_user.id
  if user_id in pending_receipts:
    del pending_receipts[user_id]
  await callback.message.edit_text(
      "❌ <b>Загрузка отменена.</b> Дубликат не попал в таблицу.",
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("proj_"))
async def process_project_choice(callback: types.CallbackQuery):
  project_short = callback.data.replace("proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)

  if not receipt:
    await callback.answer("Данные чека устарели.", show_alert=True)
    return

  full_project = next(
      (p for p in get_active_projects() if p.startswith(project_short)),
      "Склад / Общее",
  )

  await callback.message.edit_reply_markup(reply_markup=None)
  status_update = await callback.message.answer("⏳ Записываю в таблицу...")

  try:
    log_expense(
        project=full_project,
        category=receipt["category"],
        amount=float(receipt["amount"]),
        user=callback.from_user,
        comment=f"{receipt['merchant']}: {receipt['description']}",
        check_type="Чек в Telegram",
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