import asyncio
from datetime import datetime
import io
import json
import os
import random
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
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
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_KEY)

# Каскад моделей при перегрузках (ошибка 503)
AI_MODELS_CASCADE = [
    "gemini-3.6-flash",
    "gemini-2.5-flash",
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

# Временная память состояний
pending_receipts = {}
active_shifts = {}
waiting_for_requisites = {}

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
  """Проверяет сотрудника по базам.

  Если новый — автоматически прописывает его и в 'Справочники', и в 'Выплаты
  команде' с формулами.
  """
  user_id_str = str(user.id).strip()
  username_str = f"@{user.username.lower().strip()}" if user.username else ""
  full_name = (user.full_name or "Новый сотрудник").strip()

  official_name = None

  # 1. Поиск в листе 'Справочники'
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
    print(f"Ошибка 'Справочников': {e}")
    official_name = full_name

  # 2. Проверка и добавление в лист 'Выплаты команде'
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    col_names = ws_pay.col_values(1)

    if official_name not in col_names:
      next_row = len(col_names) + 1
      formula_debt = f'=SUMIFS(Операции!F:F, Операции!G:G, A{next_row}, Операции!H:H, "К возмещению")'
      formula_paid = f'=SUMIFS(Операции!F:F, Операции!G:G, A{next_row}, Операции!H:H, "Выплачено")'

      ws_pay.append_row(
          [official_name, "Команда", "-", formula_debt, formula_paid],
          value_input_option="USER_ENTERED",
      )
      print(
          f"➕ Сотрудник '{official_name}' добавлен в 'Выплаты команде' с формулами"
      )
  except Exception as e:
    print(f"Ошибка листа 'Выплаты команде': {e}")

  return official_name


def get_employee_requisites(employee_name: str) -> str:
  """Считывает реквизиты из листа 'Выплаты команде' (колонка C)"""
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
        return reqs if reqs and reqs != "-" else "Реквизиты еще не указаны"
  except Exception as e:
    print(f"Ошибка чтения реквизитов: {e}")
  return "Реквизиты еще не указаны"


def save_employee_requisites(employee_name: str, requisites_text: str):
  """Сохраняет реквизиты в лист 'Выплаты команде' (колонка C)"""
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
      ws_pay.update_cell(matched_idx, 3, requisites_text)
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
        "\n\n💡 <b>Важно:</b> Пожалуйста, нажмите кнопку «💳 Мои реквизиты» и"
        " укажите ваш номер Kaspi и ИИН для переводов!"
    )

  await message.answer(
      f"Привет, {name}! 🎛️\n\n"
      "Я бот для учета расходов на площадках.\n"
      "• Отправь фото чека для распознавания\n"
      "• Жми «🍔 Обеденные» для суточных 2 500 ₸\n"
      "• Или запускай «⏱ Моя смена» для автоматического учета времени."
      f"{extra_text}",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.message(F.text == "📸 Как отправить чек")
async def msg_how_to(message: types.Message):
  await message.answer(
      "📷 <b>Как отправить чек:</b>\n\n"
      "1. Нажмите на скрепку и отправьте фото чека (или скриншот Kaspi / Яндекс Go).\n"
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
        f"👤 <b>{name}</b>\n\nУ вас нет активных чеков к возмещению. Все выплачено! 🎉",
        parse_mode="HTML",
    )
    return

  text = (
      f"👤 <b>Чеки к возмещению ({name})</b>\n\n"
      f"💰 <b>Общая сумма:</b> {total:,.0f} ₸\n\n"
      "<b>Список в обработке:</b>\n"
  )
  for it in items[:10]:
    text += f"• {it['amount']:,.0f} ₸ — <i>{it['project']}</i> ({it['comment']})\n"

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
      "<i>По этим реквизитам администратор переводит вам деньги за чеки и обеденные.</i>",
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
      "<code>+7 777 123 4567 (Kaspi)\nИИН: 990102350444\nКарта: 4400 4301 9876 5432</code>\n\n"
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
      f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n\n"
      f"🎯 Проект: {full_project}\n"
      f"Сумма передана в таблицу к возмещению.",
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
        "⏱ <b>Учет смены</b>\nУ вас нет активной смены. Выберите проект для старта:",
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
        f"⏱ <b>Текущая смена в процессе</b>\n\n"
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
      f"🟢 <b>Смена начата!</b>\n\n"
      f"🎯 Проект: <b>{full_project}</b>\n"
      f"🕒 Время старта: {datetime.now().strftime('%H:%M')}\n\n"
      "При завершении смены бот рассчитает отработанные часы и начислит обеденные.",
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
      f"🔴 <b>Смена завершена!</b>\n\n"
      f"🎯 Проект: {shift['project']}\n"
      f"⏱ Отработано: {int(hours)} ч. {int((duration.total_seconds() % 3600) // 60)} мин.\n"
      f"🍔 Положено обедов: {earned_meals_count} × 2 500 ₸\n"
  )
  if remaining_payout > 0:
    msg += f"💰 Начислено к выплате: <b>{remaining_payout:,.0f} ₸</b>"
  else:
    msg += "👌 Все положенные обеденные уже были учтены."

  await callback.message.edit_text(msg, parse_mode="HTML")


# --- ПАНЕЛЬ ВЫПЛАТ (ADMIN) ---


@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    await message.answer("⛔️ Нет прав доступа.", parse_mode="HTML")
    return
  await render_admin_menu(message)


async def render_admin_menu(event_target):
  debts = get_debts_summary()
  buttons = []

  if not debts:
    text = (
        "💼 <b>Панель управления (Admin)</b>\n\n"
        "🎉 <b>Все долги закрыты!</b> Нет расходов к возмещению."
    )
    buttons.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh")]
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
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh")]
    )

  kb = InlineKeyboardMarkup(inline_keyboard=buttons)
  if isinstance(event_target, types.Message):
    await event_target.answer(text, reply_markup=kb, parse_mode="HTML")
  else:
    await event_target.message.edit_text(
        text, reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_refresh")
async def cb_admin_refresh(callback: types.CallbackQuery):
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return
  await render_admin_menu(callback)
  await callback.answer("Обновлено")


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay_person_preview(callback: types.CallbackQuery):
  """Этап 1: Просмотр суммы и реквизитов сотрудника перед переводом"""
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  person_prefix = callback.data.replace("pay_", "")
  debts = get_debts_summary()

  # Находим полное имя человека
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
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_refresh")],
      ]
  )

  await callback.message.edit_text(
      f"💼 <b>Окно выплаты сотруднику</b>\n\n"
      f"👤 <b>Сотрудник:</b> {full_person_name}\n"
      f"💵 <b>Сумма к переводу:</b> <code>{amount:,.0f}</code> ₸\n\n"
      f"📋 <b>Реквизиты для Kaspi / Банка:</b>\n"
      f"<code>{reqs}</code>\n\n"
      "<i>1. Скопируйте данные и сделайте перевод в приложении Kaspi.\n"
      "2. Нажмите зеленую кнопку подтверждения ниже, чтобы закрыть долг в таблице.</i>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("confirmpay_"))
async def cb_confirm_payment(callback: types.CallbackQuery):
  """Этап 2: Подтверждение перевода и списание чеков в таблице"""
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
        "Теперь при выплатах администратор будет сразу видеть эти реквизиты.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )


# --- РАСПОЗНАВАНИЕ ЧЕКОВ (ФОТО) ЧЕРЕЗ КАСКАД МОДЕЛЕЙ ИИ ---


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  status_msg = await message.answer("🔍 Распознаю чек...")

  photo = message.photo[-2] if len(message.photo) > 1 else message.photo[-1]
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
    Отвечай ТОЛЬКО валидным JSON без markdown.
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
      )
      if resp and resp.text:
        response = resp
        break
    except Exception as e:
      print(f"Модель {model_name} временно недоступна ({e}). Переключаюсь...")
      await asyncio.sleep(0.5)
      continue

  if not response or not response.text:
    await status_msg.edit_text(
        "⚠️ Все серверы ИИ сейчас временно перегружены. Попробуйте еще раз через 20–30 секунд."
    )
    return

  try:
    raw = response.text.strip()
    if raw.startswith("```json"):
      raw = raw[7:]
    if raw.startswith("```"):
      raw = raw[3:]
    if raw.endswith("```"):
      raw = raw[:-3]

    data = json.loads(raw.strip())
    amount = data.get("amount", 0)

    employee = get_or_register_employee(message.from_user)

    pending_receipts[message.from_user.id] = {
        "amount": amount,
        "merchant": data.get("merchant", "Неизвестно"),
        "category": data.get("category", "Прочее"),
        "description": data.get("description", ""),
        "user_name": employee,
    }

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

    await status_msg.edit_text(
        f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({data.get('merchant')})\n"
        f"📂 {data.get('category')} — {data.get('description')}\n"
        f"👤 Сотрудник: <b>{employee}</b>\n\n"
        "👉 <b>К какому проекту привязать покупку?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора чека: {e}")


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