import asyncio
import calendar
from datetime import date, datetime, timedelta
import io
import json
import os
import random
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

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
import gspread

# 1. Загрузка конфигурации
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "disa2026").strip()
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "disa_manager").strip()

raw_admins = os.getenv("ADMIN_IDS", "")
INITIAL_ADMINS = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

# Часовой пояс Астаны (UTC+5)
TZ_ASTANA = ZoneInfo("Asia/Almaty")

# Настройки таймера смены
SHIFT_CHECK_INTERVAL_HOURS = 6  # Проверка каждые 6 часов
SHIFT_RESPONSE_TIMEOUT_MINUTES = 30  # 30 минут на ответ, иначе автозавершение


def get_now_astana() -> datetime:
  return datetime.now(TZ_ASTANA)


# 2. Инициализация Telegram и Google AI
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_KEY)

# 3. Подключение к Google Таблицам и Календарю
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
]
gc = gspread.service_account(filename="credentials.json", scopes=SCOPES)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

authenticated_admins = set(INITIAL_ADMINS)
authenticated_managers = set()
employee_cache = {}

pending_receipts = {}
# Структура active_shifts[user_id]:
# {
#   "project": str,
#   "start_time": datetime,
#   "employee": str,
#   "claimed_meals": int,
#   "next_prompt_at": datetime,
#   "awaiting_extension": bool,
#   "prompt_sent_at": datetime | None
# }
active_shifts = {}
waiting_for_requisites = {}
pending_shift_requests = {}
saved_receipt_file_ids = set()

MONTH_NAMES_RU = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


# --- ПРОВЕРКА РОЛЕЙ ---


def is_admin(user_id: int) -> bool:
  return user_id in authenticated_admins


def is_manager_or_admin(user_id: int) -> bool:
  return user_id in authenticated_admins or user_id in authenticated_managers


def load_managers_from_sheets_sync():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    rows = ws_pay.get_all_values()
    ws_ref = spreadsheet.worksheet("Справочники")
    ref_rows = ws_ref.get_all_records()

    manager_names = set()
    for row in rows[1:]:
      if len(row) >= 2:
        name, role = row[0].strip(), row[1].strip().lower()
        if "управл" in role or "менедж" in role or "admin" in role:
          manager_names.add(name.lower())

    for r in ref_rows:
      name = str(r.get("Имя", "")).strip().lower()
      chat_id = str(r.get("Chat ID", "")).strip()
      if name in manager_names and chat_id.isdigit():
        authenticated_managers.add(int(chat_id))
  except Exception as e:
    print(f"Ошибка загрузки менеджеров: {e}")


# --- КАСКАД GEMINI 3.6 С ЗАЩИТОЙ ОТ 503 ---


async def call_gemini_safe(contents, json_mode: bool = True):
  models_cascade = [
      "gemini-3.6-flash",
      "gemini-3.6-flash",
      "gemini-2.5-flash",
      "gemini-2.0-flash",
      "gemini-1.5-flash",
  ]
  config = (
      genai_types.GenerateContentConfig(
          response_mime_type="application/json", temperature=0.1
      )
      if json_mode
      else None
  )
  for idx, model_name in enumerate(models_cascade):
    try:
      resp = ai_client.models.generate_content(
          model=model_name, contents=contents, config=config
      )
      if resp and resp.text:
        return resp.text
    except Exception as e:
      print(f"Попытка на {model_name} временно не удалась: {e}")
      await asyncio.sleep(1.0 if idx == 0 else 0.3)
  return None


# --- СИНХРОНИЗАЦИЯ С GOOGLE КАЛЕНДАРЕМ ---


def get_calendar_events_for_date_sync(target_date: date) -> list[str]:
  if not GOOGLE_CALENDAR_ID:
    return []
  try:
    creds = gc.auth
    if not creds.token:
      from google.auth.transport.requests import Request
      creds.refresh(Request())

    start_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        0,
        0,
        0,
        tzinfo=TZ_ASTANA,
    ).isoformat()
    end_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        23,
        59,
        59,
        tzinfo=TZ_ASTANA,
    ).isoformat()

    cal_encoded = urllib.parse.quote(GOOGLE_CALENDAR_ID)
    params = urllib.parse.urlencode({
        "timeMin": start_dt,
        "timeMax": end_dt,
        "singleEvents": "true",
        "orderBy": "startTime",
    })
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_encoded}/events?{params}"

    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {creds.token}"}
    )
    with urllib.request.urlopen(req, timeout=8) as response:
      data = json.loads(response.read().decode("utf-8"))
      events = []
      for item in data.get("items", []):
        summary = item.get("summary", "").strip()
        if summary:
          date_str = target_date.strftime("%d.%m.%Y")
          events.append(f"{date_str} ({summary})")
      return events
  except Exception as e:
    print(f"Инфо Google Календаря: {e}")
    return []


async def resolve_date_or_calendar_label(date_str: str) -> str:
  if date_str == "Склад / Общее" or not GOOGLE_CALENDAR_ID:
    return date_str
  try:
    dt = datetime.strptime(date_str, "%d.%m.%Y").date()
    events = await asyncio.to_thread(get_calendar_events_for_date_sync, dt)
    if len(events) == 1:
      return events[0]
    elif len(events) > 1:
      names = ", ".join(
          [e.split("(", 1)[1].rstrip(")") for e in events if "(" in e]
      )
      return f"{date_str} ({names[:40]})"
  except Exception:
    pass
  return date_str


# --- ИНТЕРАКТИВНЫЙ КАЛЕНДАРЬ И ВЫБОР ДАТЫ В TELEGRAM ---


def build_quick_date_keyboard(prefix: str) -> InlineKeyboardMarkup:
  now = get_now_astana()
  today_str = now.strftime("%d.%m.%Y")
  yesterday_str = (now - timedelta(days=1)).strftime("%d.%m.%Y")
  tomorrow_str = (now + timedelta(days=1)).strftime("%d.%m.%Y")

  buttons = [
      [
          InlineKeyboardButton(
              text=f"📍 Сегодня ({today_str})",
              callback_data=f"pickdate_{prefix}_{today_str}",
          )
      ],
      [
          InlineKeyboardButton(
              text=f"⏪ Вчера ({yesterday_str})",
              callback_data=f"pickdate_{prefix}_{yesterday_str}",
          ),
          InlineKeyboardButton(
              text=f"⏩ Завтра ({tomorrow_str})",
              callback_data=f"pickdate_{prefix}_{tomorrow_str}",
          ),
      ],
      [
          InlineKeyboardButton(
              text="🗓 Выбрать дату в календаре",
              callback_data=f"opencal_{prefix}_{now.year}_{now.month}",
          )
      ],
      [
          InlineKeyboardButton(
              text="🏢 Склад / Общее (без даты)",
              callback_data=f"pickdate_{prefix}_Склад / Общее",
          )
      ],
  ]
  return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_month_calendar_keyboard(
    prefix: str, year: int, month: int
) -> InlineKeyboardMarkup:
  kb = []
  kb.append([
      InlineKeyboardButton(
          text=f"🗓 {MONTH_NAMES_RU[month]} {year}", callback_data="cal_ignore"
      )
  ])
  days_header = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
  kb.append([
      InlineKeyboardButton(text=d, callback_data="cal_ignore")
      for d in days_header
  ])

  month_matrix = calendar.monthcalendar(year, month)
  now_date = get_now_astana().date()

  for week in month_matrix:
    row = []
    for day in week:
      if day == 0:
        row.append(InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
      else:
        d_str = f"{day:02d}.{month:02d}.{year}"
        label = (
            f"[{day}]"
            if (
                year == now_date.year
                and month == now_date.month
                and day == now_date.day
            )
            else str(day)
        )
        row.append(
            InlineKeyboardButton(
                text=label, callback_data=f"pickdate_{prefix}_{d_str}"
            )
        )
    kb.append(row)

  prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
  next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)

  kb.append([
      InlineKeyboardButton(
          text="⬅️ Пред. месяц",
          callback_data=f"opencal_{prefix}_{prev_year}_{prev_month}",
      ),
      InlineKeyboardButton(
          text="След. месяц ➡️",
          callback_data=f"opencal_{prefix}_{next_year}_{next_month}",
      ),
  ])
  kb.append([
      InlineKeyboardButton(
          text="🔙 Назад к быстрым датам",
          callback_data=f"backquick_{prefix}",
      )
  ])
  return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.callback_query(F.data == "cal_ignore")
async def cb_cal_ignore(callback: types.CallbackQuery):
  await callback.answer()


@dp.callback_query(F.data.startswith("opencal_"))
async def cb_open_calendar(callback: types.CallbackQuery):
  await callback.answer()
  parts = callback.data.split("_")
  prefix = parts[1]
  year, month = int(parts[2]), int(parts[3])
  kb = build_month_calendar_keyboard(prefix, year, month)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("backquick_"))
async def cb_back_quick_dates(callback: types.CallbackQuery):
  await callback.answer()
  prefix = callback.data.replace("backquick_", "")
  kb = build_quick_date_keyboard(prefix)
  await callback.message.edit_reply_markup(reply_markup=kb)


# --- GOOGLE DRIVE: СОХРАНЕНИЕ ЧЕКОВ ---


def upload_receipt_to_drive_sync(
    file_bytes: bytes, filename: str
) -> str | None:
  try:
    creds = gc.auth
    token = creds.token
    if not token:
      from google.auth.transport.requests import Request
      creds.refresh(Request())
      token = creds.token

    boundary = "-------314159265358979323846"
    delimiter = f"\r\n--{boundary}\r\n".encode("utf-8")
    close_delim = f"\r\n--{boundary}--\r\n".encode("utf-8")

    metadata = {"name": filename, "mimeType": "image/jpeg"}
    body = (
        delimiter
        + b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata).encode("utf-8")
        + delimiter
        + b"Content-Type: image/jpeg\r\n\r\n"
        + file_bytes
        + close_delim
    )

    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as response:
      res_json = json.loads(response.read().decode("utf-8"))
      return res_json.get("webViewLink")
  except Exception as e:
    print(f"Ошибка загрузки на Google Диск: {e}")
    return None


# --- FSM СОСТОЯНИЯ ---


class QuickExpenseState(StatesGroup):
  waiting_for_details = State()


class RetroShiftState(StatesGroup):
  waiting_for_time = State()


class CustomPayoutState(StatesGroup):
  waiting_for_amount = State()


NAV_BUTTONS = [
    "🚕 Такси",
    "🍔 Обеды",
    "🧾 Расходы",
    "⏱ Моя смена",
    "💰 Мои выплаты",
    "💳 Мои реквизиты",
    "💼 Панель управления",
]


# --- СИНХРОНИЗАЦИЯ ТАБЛИЦЫ ---


def repair_spreadsheet_sync():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    pay_rows = ws_pay.get_all_values()
    for r_idx in range(2, len(pay_rows) + 1):
      emp_name = ws_pay.cell(r_idx, 1).value
      if not emp_name:
        continue
      formula_debt = f'=SUMIFS(Операции!F:F; Операции!G:G; A{r_idx}; Операции!H:H; "К возмещению")'
      formula_paid = f'=SUMIFS(Операции!F:F; Операции!G:G; A{r_idx}; Операции!H:H; "Выплачено")'
      ws_pay.update_cell(r_idx, 4, formula_debt)
      ws_pay.update_cell(r_idx, 5, formula_paid)
    return True
  except Exception as e:
    print(f"Ошибка починки формул: {e}")
    return False


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
    print(f"Ошибка сотрудника: {e}")
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
    print(f"Ошибка реквизитов: {e}")
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


def get_all_team_roles_sync():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    rows = ws_pay.get_all_values()
    team = []
    for row in rows[1:]:
      if row and row[0].strip():
        name = row[0].strip()
        role = row[1].strip() if len(row) > 1 else "Команда"
        team.append((name, role))
    return team
  except Exception as e:
    print(f"Ошибка чтения ролей: {e}")
    return []


def set_employee_role_sync(employee_name: str, new_role: str):
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    col_names = ws_pay.col_values(1)
    for idx, name in enumerate(col_names, start=1):
      if name.strip().lower() == employee_name.strip().lower():
        ws_pay.update_cell(idx, 2, new_role)
        return True
  except Exception as e:
    print(f"Ошибка смены роли: {e}")
  return False


def log_expense_sync(
    date_label: str,
    category: str,
    amount: float,
    user: types.User | None,
    comment: str,
    check_type: str = "Норматив (без чека)",
    payer: str = None,
    status: str = "К возмещению",
    drive_link: str = None,
    tx_id: str = None,
):
  employee = payer if payer else get_or_register_employee_sync(user)
  ws = spreadsheet.worksheet("Операции")
  tx_id = tx_id if tx_id else f"TX-{random.randint(10000, 99999)}"
  now_str = get_now_astana().strftime("%d.%m.%Y %H:%M")

  check_val = (
      f'=HYPERLINK("{drive_link}"; "📎 Чек")' if drive_link else check_type
  )
  ws.append_row(
      [
          tx_id,
          now_str,
          "Расход",
          date_label,
          category,
          amount,
          employee,
          status,
          check_val,
          comment,
      ],
      value_input_option="USER_ENTERED",
  )


def update_expense_status_sync(tx_id: str, new_status: str):
  try:
    ws = spreadsheet.worksheet("Операции")
    all_values = ws.get_all_values()
    for r_idx, row in enumerate(all_values[1:], start=2):
      if row and row[0].strip() == tx_id:
        ws.update_cell(r_idx, 8, new_status)
        return True
  except Exception as e:
    print(f"Ошибка обновления статуса TX: {e}")
  return False


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


def process_custom_payout_sync(
    employee_name: str, payout_amount: float, current_debt: float
):
  ws = spreadsheet.worksheet("Операции")
  all_values = ws.get_all_values()
  if not all_values:
    return False

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
          employee_name.lower() in who_val.lower()
          or who_val.lower() in employee_name.lower()
      ) and "к возмещению" in status_val:
        ws.update_cell(row_idx, status_col_idx, "Выплачено")

  diff = payout_amount - current_debt
  now_str = get_now_astana().strftime("%d.%m.%Y %H:%M")
  tx_id = f"TX-{random.randint(10000, 99999)}"

  if diff > 0.01:
    ws.append_row(
        [
            tx_id,
            now_str,
            "Расход",
            "Склад / Общее",
            "Взаиморасчеты",
            -diff,
            employee_name,
            "К возмещению",
            "Перевод Kaspi",
            (
                f"Аванс / переплата на баланс (выплачено {payout_amount:,.0f} ₸"
                f" при долге {current_debt:,.0f} ₸)"
            ),
        ],
        value_input_option="USER_ENTERED",
    )
  elif diff < -0.01:
    remaining_debt = abs(diff)
    ws.append_row(
        [
            tx_id,
            now_str,
            "Расход",
            "Склад / Общее",
            "Взаиморасчеты",
            remaining_debt,
            employee_name,
            "К возмещению",
            "Перевод Kaspi",
            (
                f"Остаток долга после частичной выплаты {payout_amount:,.0f} ₸"
                f" (долг был {current_debt:,.0f} ₸)"
            ),
        ],
        value_input_option="USER_ENTERED",
    )
  return True


# --- ГЛАВНАЯ КЛАВИАТУРА ---


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
  keyboard = [
      [KeyboardButton(text="🚕 Такси"), KeyboardButton(text="🍔 Обеды")],
      [
          KeyboardButton(text="🧾 Расходы"),
          KeyboardButton(text="⏱ Моя смена"),
      ],
      [
          KeyboardButton(text="💰 Мои выплаты"),
          KeyboardButton(text="💳 Мои реквизиты"),
      ],
  ]
  if is_manager_or_admin(user_id):
    keyboard.append([KeyboardButton(text="💼 Панель управления")])
  return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# --- АВТОРИЗАЦИЯ ГЛАВНОГО АДМИНА И МЕНЕДЖЕРОВ ---


@dp.message(Command("login"))
async def cmd_login(message: types.Message):
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.answer(
        "⚠️ Введите пароль: <code>/login ваш_пароль</code>", parse_mode="HTML"
    )
    return
  if args[1].strip() == ADMIN_PASSWORD:
    authenticated_admins.add(message.from_user.id)
    await message.answer(
        "🔓 <b>Доступ главного администратора активирован!</b>",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )
  else:
    await message.answer("⛔️ Неверный пароль.")


@dp.message(Command("manager"))
async def cmd_manager_login(message: types.Message):
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.answer(
        "⚠️ Введите пароль: <code>/manager пароль</code>", parse_mode="HTML"
    )
    return
  if args[1].strip() == MANAGER_PASSWORD:
    authenticated_managers.add(message.from_user.id)
    await message.answer(
        "🔓 <b>Доступ управляющего активирован!</b>",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode="HTML",
    )
  else:
    await message.answer("⛔️ Неверный пароль управляющего.")


# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  name = await get_or_register_employee(message.from_user)
  kb = get_main_keyboard(message.from_user.id)
  await message.answer(
      f"Привет, {name}! 🎛️ (Время Астаны: {get_now_astana().strftime('%H:%M')})\n\n"
      "Бот учета расходов и смен по календарю готов к работе:\n"
      "• 🚕 <b>Такси</b> — ночные поездки (22:00–07:00) сразу к выплате\n"
      "• 🍔 <b>Обеды</b> — начисление обеденных 2 500 ₸ по дате\n"
      "• 🧾 <b>Расходы</b> — отправка любых чеков с выбором даты в календаре\n"
      "• ⏱ <b>Моя смена</b> — фиксация часов (с проверкой каждые 6 часов)",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.message(F.text == "💰 Мои выплаты")
async def msg_my_payouts(message: types.Message):
  status_wait = await message.answer("🔍 Проверяю ваш баланс...")
  name, total, items = await asyncio.to_thread(
      get_user_pending_receipts_sync, message.from_user
  )
  if total < -0.01:
    balance_text = (
        f"🟢 <b>Аванс / переплата на вашем балансе:</b> {abs(total):,.0f} ₸\n<i>(Будет"
        " автоматически зачтен в счет будущих чеков)</i>"
    )
  elif total > 0.01:
    balance_text = f"💰 <b>К возмещению вам:</b> {total:,.0f} ₸"
  else:
    balance_text = "🎉 Все чеки выплачены! Баланс нулевой."

  text = f"👤 <b>Финансовый баланс ({name})</b>\n\n{balance_text}\n\n"
  if items:
    text += "<b>История операций:</b>\n"
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
      f"💳 <b>Реквизиты для выплат:</b>\n\n👤 <b>Сотрудник:</b> {employee}\n📋"
      f" <code>{reqs}</code>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_requisites(callback: types.CallbackQuery):
  await callback.answer()
  waiting_for_requisites[callback.from_user.id] = True
  await callback.message.edit_text(
      "📝 <b>Введите ваши реквизиты (Kaspi номер, ИИН, карта):</b>",
      parse_mode="HTML",
  )


# --- РАЗДЕЛ: ТАКСИ ---


@dp.message(F.text == "🚕 Такси")
async def cmd_taxi_menu(message: types.Message):
  now = get_now_astana()
  is_night = now.hour >= 22 or now.hour < 7
  status_info = (
      "🌙 <b>Сейчас ночной режим (22:00–07:00):</b> поездка подтверждается"
      " автоматически!"
      if is_night
      else "☀️ <b>Сейчас дневной режим:</b> поездка потребует согласования"
      " управляющего/админа."
  )
  await message.answer(
      f"🚕 <b>Учет поездки на такси</b>\n\n"
      f"🕒 Время Астаны: <b>{now.strftime('%H:%M')}</b>\n{status_info}\n\n"
      "Отправьте <b>скриншот / чек поездки</b> из Яндекс Go в этот чат:",
      parse_mode="HTML",
  )


# --- РАЗДЕЛ: ОБЕДЫ ПО КАЛЕНДАРЮ ---


@dp.message(F.text == "🍔 Обеды")
async def cmd_meals_menu(message: types.Message):
  kb = build_quick_date_keyboard("meal")
  await message.answer(
      "🍔 <b>Обеденные (2 500 ₸)</b>\nВыберите дату выезда / смены:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("pickdate_meal_"))
async def cb_confirm_meal(callback: types.CallbackQuery):
  await callback.answer("Записываю...")
  raw_date = callback.data.replace("pickdate_meal_", "")
  date_label = await resolve_date_or_calendar_label(raw_date)

  if callback.from_user.id in active_shifts:
    active_shifts[callback.from_user.id]["claimed_meals"] += 1

  await asyncio.to_thread(
      log_expense_sync,
      date_label,
      "Питание команды",
      2500,
      callback.from_user,
      "Обеденные (быстрая выплата)",
  )
  await callback.message.edit_text(
      f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n📅 Дата: <b>{date_label}</b>",
      parse_mode="HTML",
  )


# --- РАЗДЕЛ: СМЕНА, ТАЙМЕР 6 ЧАСОВ И ЗАДНЕЕ ЧИСЛО ЧЕРЕЗ АДМИНА ---


def calculate_next_6h_prompt(start_time: datetime, now: datetime) -> datetime:
  """Вычисляет время следующей 6-часовой проверки от старта смены"""
  elapsed_seconds = max(0, (now - start_time).total_seconds())
  elapsed_hours = elapsed_seconds / 3600.0
  next_block = (
      int(elapsed_hours // SHIFT_CHECK_INTERVAL_HOURS) + 1
  ) * SHIFT_CHECK_INTERVAL_HOURS
  return start_time + timedelta(hours=next_block)


async def close_shift_and_calculate_meals(
    user_id: int, end_dt: datetime, auto_closed: bool = False
) -> str | None:
  """Единая функция завершения смены (вручную или автоматически по таймеру)"""
  if user_id not in active_shifts:
    return None

  shift = active_shifts.pop(user_id)
  duration = max(timedelta(0), end_dt - shift["start_time"])
  hours = duration.total_seconds() / 3600.0
  hrs_int = int(duration.total_seconds() // 3600)
  mins_int = int((duration.total_seconds() % 3600) // 60)

  earned_meals_count = int(hours // 6)
  already_claimed = shift["claimed_meals"]
  remaining_meals = max(0, earned_meals_count - already_claimed)
  remaining_payout = remaining_meals * 2500

  reason_note = "автозавершение (нет ответа)" if auto_closed else "остаток"
  if remaining_payout > 0:
    await asyncio.to_thread(
        log_expense_sync,
        shift["project"],
        "Питание команды",
        remaining_payout,
        None,
        f"Обеденные за смену ({hrs_int} ч, {reason_note})",
        payer=shift["employee"],
    )

  header = (
      "🔴 <b>Смена завершена автоматически (нет ответа на таймер 6 ч)!</b>"
      if auto_closed
      else "🔴 <b>Смена завершена!</b>"
  )
  msg = (
      f"{header}\n\n"
      f"📅 Дата: <b>{shift['project']}</b>\n"
      f"🕒 Старт: {shift['start_time'].strftime('%H:%M')} ➔ Конец:"
      f" {end_dt.strftime('%H:%M')}\n"
      f"⏱ Отработано: <b>{hrs_int} ч. {mins_int} мин.</b>\n"
      f"🍔 Положено обедов: {earned_meals_count} × 2 500 ₸\n"
  )
  if remaining_payout > 0:
    msg += f"💰 Начислено к выплате: <b>{remaining_payout:,.0f} ₸</b>"
  else:
    msg += "👌 Все положенные обеденные уже были учтены."

  return msg


async def shift_monitor_loop():
  """Фоновый таймер: проверяет смены каждые 6 часов и закрывает при отсутствии ответа"""
  while True:
    try:
      now = get_now_astana()
      for user_id, shift in list(active_shifts.items()):
        # 1. Если мы уже ждем ответа от сотрудника — проверяем таймаут (30 минут)
        if shift.get("awaiting_extension"):
          prompt_sent_at = shift.get("prompt_sent_at", now)
          if now >= prompt_sent_at + timedelta(
              minutes=SHIFT_RESPONSE_TIMEOUT_MINUTES
          ):
            emp_name = shift.get("employee", "Сотрудник")
            proj_label = shift.get("project", "-")
            # Закрываем смену ровно по времени отправки 6-часового запроса
            summary_text = await close_shift_and_calculate_meals(
                user_id, end_dt=prompt_sent_at, auto_closed=True
            )
            if summary_text:
              try:
                await bot.send_message(
                    user_id, summary_text, parse_mode="HTML"
                )
              except Exception:
                pass
              # Уведомляем админов об автозакрытии
              for admin_id in authenticated_admins:
                try:
                  await bot.send_message(
                      admin_id,
                      f"ℹ️ <b>Автозавершение смены:</b>\n👤 {emp_name} не"
                      " ответил на таймер продления (6 ч).\n📅"
                      f" {proj_label}\nСмена закрыта в"
                      f" {prompt_sent_at.strftime('%H:%M')}.",
                      parse_mode="HTML",
                  )
                except Exception:
                  pass

        # 2. Если подошло время очередной 6-часовой проверки — отправляем запрос
        elif now >= shift.get("next_prompt_at", now + timedelta(hours=6)):
          shift["awaiting_extension"] = True
          shift["prompt_sent_at"] = now

          duration = now - shift["start_time"]
          hrs = int(duration.total_seconds() // 3600)

          kb = InlineKeyboardMarkup(
              inline_keyboard=[
                  [
                      InlineKeyboardButton(
                          text="🟢 Да, продлить смену (+6 ч)",
                          callback_data="extend_shift",
                      )
                  ],
                  [
                      InlineKeyboardButton(
                          text="🔴 Завершить смену сейчас",
                          callback_data="end_shift",
                      )
                  ],
              ]
          )
          try:
            await bot.send_message(
                user_id,
                f"⏰ <b>Проверка смены: прошло {hrs} ч.!</b>\n\n"
                f"📅 Дата: <b>{shift['project']}</b>\n"
                "Вы продолжаете работать на площадке?\n\n"
                f"⚠️ <i>Подтвердите продление в течение <b>{SHIFT_RESPONSE_TIMEOUT_MINUTES}"
                " минут</b>, иначе смена закроется автоматически!</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )
          except Exception as e:
            print(f"Ошибка отправки 6ч уведомления {user_id}: {e}")

    except Exception as e:
      print(f"Ошибка в цикле мониторинга смен: {e}")

    await asyncio.sleep(60)


@dp.callback_query(F.data == "extend_shift")
async def cb_extend_shift(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  if user_id not in active_shifts:
    await callback.answer(
        "Ваша смена уже была завершена.", show_alert=True
    )
    return

  await callback.answer("Смена продлена!")
  now = get_now_astana()
  shift = active_shifts[user_id]
  shift["awaiting_extension"] = False
  shift["prompt_sent_at"] = None
  shift["next_prompt_at"] = now + timedelta(hours=SHIFT_CHECK_INTERVAL_HOURS)

  next_str = shift["next_prompt_at"].strftime("%H:%M")
  await callback.message.edit_text(
      f"✅ <b>Смена успешно продлена!</b>\n\n"
      f"📅 Дата: <b>{shift['project']}</b>\n"
      f"⏰ Следующая проверка через 6 часов (в {next_str}).",
      parse_mode="HTML",
  )


@dp.message(F.text == "⏱ Моя смена")
async def cmd_shift_menu(message: types.Message):
  user_id = message.from_user.id
  if user_id not in active_shifts:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Начать прямо сейчас", callback_data="shift_mode_now"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🕒 Начать задним числом (через Админа)",
                    callback_data="shift_mode_retro",
                )
            ],
        ]
    )
    await message.answer(
        "⏱ <b>Учет рабочего времени</b>\nУ вас нет активной смены. Как хотите"
        " начать?",
        reply_markup=kb,
        parse_mode="HTML",
    )
  else:
    shift = active_shifts[user_id]
    duration = get_now_astana() - shift["start_time"]
    hours = int(duration.total_seconds() // 3600)
    minutes = int((duration.total_seconds() % 3600) // 60)
    next_check = shift["next_prompt_at"].strftime("%H:%M")

    buttons = [
        [
            InlineKeyboardButton(
                text="🔴 Завершить смену", callback_data="end_shift"
            )
        ]
    ]
    if shift.get("awaiting_extension"):
      buttons.insert(
          0,
          [
              InlineKeyboardButton(
                  text="🟢 Подтвердить продление (+6 ч)",
                  callback_data="extend_shift",
              )
          ],
      )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        f"⏱ <b>Смена в процессе:</b> <b>{shift['project']}</b>\n"
        f"🕒 Старт: {shift['start_time'].strftime('%H:%M')}\n"
        f"⏳ Прошло: <b>{hours} ч. {minutes} мин.</b>\n"
        f"⏰ Следующий таймер (6 ч): {next_check}",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "shift_mode_now")
async def cb_shift_mode_now(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  await state.update_data(is_retro=False)
  kb = build_quick_date_keyboard("startshift")
  await callback.message.edit_text(
      "📅 Выберите дату смены:", reply_markup=kb, parse_mode="HTML"
  )


@dp.callback_query(F.data == "shift_mode_retro")
async def cb_shift_mode_retro(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  await state.update_data(is_retro=True)
  kb = build_quick_date_keyboard("startshift")
  await callback.message.edit_text(
      "📅 Выберите дату смены (для запроса задним числом у Администратора):",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("pickdate_startshift_"))
async def cb_choose_date_shift(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  raw_date = callback.data.replace("pickdate_startshift_", "")
  date_label = await resolve_date_or_calendar_label(raw_date)

  data = await state.get_data()
  is_retro = data.get("is_retro", False)
  emp_name = await get_or_register_employee(callback.from_user)
  now = get_now_astana()

  if not is_retro:
    next_check = now + timedelta(hours=SHIFT_CHECK_INTERVAL_HOURS)
    active_shifts[callback.from_user.id] = {
        "project": date_label,
        "start_time": now,
        "employee": emp_name,
        "claimed_meals": 0,
        "next_prompt_at": next_check,
        "awaiting_extension": False,
        "prompt_sent_at": None,
    }
    await callback.message.edit_text(
        f"🟢 <b>Смена начата!</b>\n"
        f"📅 Дата: <b>{date_label}</b>\n"
        f"🕒 Время старта: {now.strftime('%H:%M')}\n"
        f"⏰ Таймер проверки через 6 ч (в {next_check.strftime('%H:%M')})",
        parse_mode="HTML",
    )
  else:
    await state.update_data(project=date_label)
    await state.set_state(RetroShiftState.waiting_for_time)
    await callback.message.edit_text(
        f"📅 Дата: <b>{date_label}</b>\n\n"
        "🕒 <b>Во сколько фактически началась ваша смена?</b>\n"
        "Напишите время в формате <code>14:00</code> или <code>2 часа"
        " назад</code>:\n\n"
        "<i>Запрос будет отправлен Главному администратору на одобрение.</i>",
        parse_mode="HTML",
    )


@dp.message(RetroShiftState.waiting_for_time, F.text)
async def process_retro_shift_time(message: types.Message, state: FSMContext):
  time_text = message.text.strip()
  data = await state.get_data()
  date_label = data.get("project", get_now_astana().strftime("%d.%m.%Y"))
  await state.clear()

  now = get_now_astana()
  target_dt = now

  match_hhmm = re.search(r"\b(\d{1,2})[:.](\d{2})\b", time_text)
  if match_hhmm:
    h, m = int(match_hhmm.group(1)), int(match_hhmm.group(2))
    target_dt = now.replace(hour=h, minute=m, second=0)
    if target_dt > now:
      target_dt -= timedelta(days=1)
  elif "час" in time_text:
    digits = re.findall(r"\d+", time_text)
    sub_h = int(digits[0]) if digits else 2
    target_dt = now - timedelta(hours=sub_h)

  req_id = f"SHIFT-{random.randint(1000, 9999)}"
  employee = await get_or_register_employee(message.from_user)

  pending_shift_requests[req_id] = {
      "user_id": message.from_user.id,
      "project": date_label,
      "start_time": target_dt,
      "employee": employee,
  }

  kb = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="✅ Одобрить смену", callback_data=f"apprshift_{req_id}"
          ),
          InlineKeyboardButton(
              text="❌ Отклонить", callback_data=f"rejshift_{req_id}"
          ),
      ]]
  )

  # Строго отправляем Главному администратору на одобрение
  for admin_id in authenticated_admins:
    try:
      await bot.send_message(
          admin_id,
          f"⚠️ <b>Запрос на смену задним числом!</b>\n\n"
          f"👤 Сотрудник: <b>{employee}</b>\n"
          f"📅 Дата: {date_label}\n"
          f"🕒 Указал старт: <b>{target_dt.strftime('%H:%M (%d.%m)')}</b>\n\n"
          "Одобрить начало смены задним числом?",
          reply_markup=kb,
          parse_mode="HTML",
      )
    except Exception:
      pass

  await message.answer(
      "⏳ <b>Запрос отправлен Администратору на одобрение.</b>\n"
      f"После подтверждения ваша смена будет зачтена с {target_dt.strftime('%H:%M')}.",
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("apprshift_"))
async def cb_appr_shift(callback: types.CallbackQuery):
  if not is_admin(callback.from_user.id):
    await callback.answer(
        "Одобрять смены задним числом может только Администратор!",
        show_alert=True,
    )
    return

  await callback.answer()
  req_id = callback.data.replace("apprshift_", "")
  shift_info = pending_shift_requests.pop(req_id, None)
  if not shift_info:
    await callback.message.edit_text("Запрос уже был обработан.")
    return

  u_id = shift_info["user_id"]
  now = get_now_astana()
  start_dt = shift_info["start_time"]
  next_check = calculate_next_6h_prompt(start_dt, now)

  active_shifts[u_id] = {
      "project": shift_info["project"],
      "start_time": start_dt,
      "employee": shift_info["employee"],
      "claimed_meals": 0,
      "next_prompt_at": next_check,
      "awaiting_extension": False,
      "prompt_sent_at": None,
  }

  await callback.message.edit_text(
      f"✅ Смена сотрудника <b>{shift_info['employee']}</b> одобрена с"
      f" {start_dt.strftime('%H:%M')}!",
      parse_mode="HTML",
  )
  try:
    await bot.send_message(
        u_id,
        f"✅ <b>Администратор одобрил вашу смену задним числом!</b>\n"
        f"📅 Дата: {shift_info['project']}\n"
        f"🕒 Старт зафиксирован: <b>{start_dt.strftime('%H:%M')}</b>\n"
        f"⏰ Следующая проверка таймера (6 ч): {next_check.strftime('%H:%M')}",
        parse_mode="HTML",
    )
  except Exception:
    pass


@dp.callback_query(F.data.startswith("rejshift_"))
async def cb_rej_shift(callback: types.CallbackQuery):
  if not is_admin(callback.from_user.id):
    await callback.answer(
        "Отклонять смены может только Администратор!", show_alert=True
    )
    return

  await callback.answer()
  req_id = callback.data.replace("rejshift_", "")
  shift_info = pending_shift_requests.pop(req_id, None)
  if shift_info:
    try:
      await bot.send_message(
          shift_info["user_id"],
          "❌ <b>Администратор отклонил запрос на смену задним числом.</b>",
          parse_mode="HTML",
      )
    except Exception:
      pass
  await callback.message.edit_text("❌ Запрос на смену отклонен.")


@dp.callback_query(F.data == "end_shift")
async def cb_end_shift(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  if user_id not in active_shifts:
    await callback.answer("Смена уже завершена", show_alert=True)
    return

  await callback.answer("Завершаю смену...")
  now = get_now_astana()
  summary_text = await close_shift_and_calculate_meals(
      user_id, end_dt=now, auto_closed=False
  )
  if summary_text:
    await callback.message.edit_text(summary_text, parse_mode="HTML")


# --- РАСХОДЫ И ЧЕКИ ПО КАЛЕНДАРЮ ---


@dp.message(F.text == "🧾 Расходы")
async def cmd_expenses_info(message: types.Message):
  await message.answer(
      "🧾 <b>Отправка чеков и накладных</b>\n\n"
      "Просто пришлите фото чека в этот чат. Бот распознает сумму, сохранит его"
      " на диск и предложит выбрать дату в календаре.",
      parse_mode="HTML",
  )


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  photo = message.photo[-1]
  if photo.file_unique_id in saved_receipt_file_ids:
    await message.answer("⚠️ Этот чек уже был успешно внесен ранее!")
    return

  status_msg = await message.answer("🔍 Распознаю чек (Gemini 3.6)...")
  file_io = io.BytesIO()
  await bot.download(photo, destination=file_io)
  image_bytes = file_io.getvalue()

  drive_link = await asyncio.to_thread(
      upload_receipt_to_drive_sync,
      image_bytes,
      f"receipt_{message.from_user.id}_{get_now_astana().strftime('%d%m_%H%M%S')}.jpg",
  )

  prompt = """
    Ты финансовый сканер сценического оборудования.
    Изучи чек и верни СТРОГО чистый JSON:
    {
      "amount": итоговая сумма числом,
      "merchant": продавец,
      "category": одна из категорий ("Такси / Логистика / ГСМ", "Питание команды", "Расходники (тейп, батарейки)", "Субаренда оборудования", "Прочее"),
      "description": суть покупки (2-4 слова)
    }
    """

  try:
    resp_text = await call_gemini_safe(
        [
            genai_types.Part.from_bytes(
                data=image_bytes, mime_type="image/jpeg"
            ),
            prompt,
        ],
        json_mode=True,
    )
    if not resp_text:
      await status_msg.edit_text(
          "⚠️ Серверы временно перегружены. Попробуйте отправить чек еще раз."
      )
      return

    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    data = json.loads(raw[start:end])
    amount = float(data.get("amount", 0))
    merchant = data.get("merchant", "Неизвестно")
    category = data.get("category", "Прочее")
    employee = await get_or_register_employee(message.from_user)

    pending_receipts[message.from_user.id] = {
        "amount": amount,
        "merchant": merchant,
        "category": category,
        "description": data.get("description", ""),
        "user_name": employee,
        "drive_link": drive_link,
        "file_unique_id": photo.file_unique_id,
    }

    now = get_now_astana()
    is_night = now.hour >= 22 or now.hour < 7
    extra_note = ""
    if "такси" in category.lower() or "яндекс" in merchant.lower():
      extra_note = (
          "\n\n🌙 <i>Ночное такси (22:00–07:00) зачислится сразу!</i>"
          if is_night
          else "\n\n☀️ <i>Дневное такси потребует согласования.</i>"
      )

    kb = build_quick_date_keyboard("proj")
    await status_msg.edit_text(
        f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({merchant})\n"
        f"📂 {category} — {data.get('description', '')}\n"
        f"👤 {employee}{extra_note}\n\n"
        "👉 <b>Выберите дату расхода в календаре:</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")


@dp.callback_query(F.data.startswith("pickdate_proj_"))
async def process_receipt_date_choice(callback: types.CallbackQuery):
  await callback.answer("Записываю...")
  raw_date = callback.data.replace("pickdate_proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)
  if not receipt:
    await callback.answer("Данные чека устарели.", show_alert=True)
    return

  date_label = await resolve_date_or_calendar_label(raw_date)

  now = get_now_astana()
  category = receipt["category"]
  is_taxi = (
      "такси" in category.lower() or "яндекс" in receipt["merchant"].lower()
  )

  # Дневное такси -> на согласование
  if is_taxi and not (now.hour >= 22 or now.hour < 7):
    tx_id = f"TX-{random.randint(10000, 99999)}"
    await asyncio.to_thread(
        log_expense_sync,
        date_label,
        category,
        float(receipt["amount"]),
        callback.from_user,
        f"{receipt['merchant']}: {receipt['description']} (Дневное такси)",
        "Чек такси",
        status="На согласовании",
        drive_link=receipt.get("drive_link"),
        tx_id=tx_id,
    )
    kb_appr = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Одобрить", callback_data=f"apprtaxi_{tx_id}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить", callback_data=f"rejtaxi_{tx_id}"
            ),
        ]]
    )
    approvers = authenticated_admins | authenticated_managers
    for a_id in approvers:
      try:
        await bot.send_message(
            a_id,
            f"🚕 <b>Запрос на дневное такси!</b>\n\n"
            f"👤 Сотрудник: <b>{receipt['user_name']}</b>\n"
            f"📅 Дата: {date_label}\n"
            f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
            f"🕒 Время: {now.strftime('%H:%M')}\n\nОдобрить выплату?",
            reply_markup=kb_appr,
            parse_mode="HTML",
        )
      except Exception:
        pass

    del pending_receipts[user_id]
    await callback.message.edit_text(
        f"⏳ <b>Дневное такси отправлено на согласование управляющему!</b>\n\n📅"
        f" {date_label}\n💵 {receipt['amount']:,.0f} ₸",
        parse_mode="HTML",
    )
    return

  # Ночное такси или обычный расход -> сразу к возмещению
  try:
    await asyncio.to_thread(
        log_expense_sync,
        date_label,
        category,
        float(receipt["amount"]),
        callback.from_user,
        f"{receipt['merchant']}: {receipt['description']}",
        "Чек в Telegram",
        status="К возмещению",
        drive_link=receipt.get("drive_link"),
    )
    if receipt.get("file_unique_id"):
      saved_receipt_file_ids.add(receipt["file_unique_id"])

    del pending_receipts[user_id]
    await callback.message.edit_text(
        f"✅ <b>Расход сохранен в таблицу!</b>\n\n"
        f"📅 Дата: <b>{date_label}</b>\n"
        f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
        f"👤 Сотрудник: {receipt['user_name']}\n"
        f"📎 Чек прикреплен ссылкой в Google Таблице.",
        parse_mode="HTML",
    )
  except Exception as e:
    await callback.message.edit_text(f"⚠️ Ошибка записи: {e}")


@dp.callback_query(F.data.startswith("apprtaxi_"))
async def cb_appr_taxi(callback: types.CallbackQuery):
  await callback.answer()
  tx_id = callback.data.replace("apprtaxi_", "")
  ok = await asyncio.to_thread(
      update_expense_status_sync, tx_id, "К возмещению"
  )
  if ok:
    await callback.message.edit_text(
        "✅ <b>Дневное такси одобрено!</b> Сумма добавлена к выплате.",
        parse_mode="HTML",
    )
  else:
    await callback.message.edit_text("⚠️ Ошибка обновления статуса.")


@dp.callback_query(F.data.startswith("rejtaxi_"))
async def cb_rej_taxi(callback: types.CallbackQuery):
  await callback.answer()
  tx_id = callback.data.replace("rejtaxi_", "")
  await asyncio.to_thread(update_expense_status_sync, tx_id, "Отклонено")
  await callback.message.edit_text("❌ Дневное такси отклонено.")


# --- ПАНЕЛЬ УПРАВЛЕНИЯ ---


@dp.message(F.text == "💼 Панель управления")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if not is_manager_or_admin(message.from_user.id):
    await message.answer("⛔️ Нет прав доступа.")
    return
  await render_admin_menu(message)


async def render_admin_menu(event_target):
  user_id = event_target.from_user.id

  buttons = [
      [
          InlineKeyboardButton(
              text="⚡️ Внести быстрый расход по дате",
              callback_data="admin_fast_expense",
          )
      ],
      [
          InlineKeyboardButton(
              text=f"⏱ Смены команды онлайн ({len(active_shifts)})",
              callback_data="admin_view_active_shifts",
          )
      ],
  ]

  if is_admin(user_id):
    debts = await asyncio.to_thread(get_debts_summary_sync)
    total_debt = sum(debts.values()) if debts else 0
    text = (
        "💼 <b>Панель Главного Администратора</b>\n\n"
        f"💵 <b>Текущий долг перед командой:</b> {total_debt:,.0f} ₸\n"
        f"⏱ <b>Сотрудников на смене:</b> {len(active_shifts)}\n\n"
        "Выберите действие:"
    )
    buttons.extend([
        [
            InlineKeyboardButton(
                text=f"💸 Выплаты команде ({len(debts)})",
                callback_data="admin_payouts_list",
            )
        ],
        [
            InlineKeyboardButton(
                text="👥 Управляющие", callback_data="admin_manage_roles"
            ),
            InlineKeyboardButton(
                text="🛠 Починить формулы", callback_data="admin_repair_tables"
            ),
        ],
    ])
  else:
    text = (
        "💼 <b>Панель Управляющего</b>\n\n"
        f"⏱ <b>Сотрудников на смене:</b> {len(active_shifts)}\n"
        "Вам доступно внесение расходов по календарю и мониторинг смен:"
    )

  buttons.append(
      [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_hub")]
  )
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
  await state.clear()
  await render_admin_menu(callback)


@dp.callback_query(F.data == "admin_repair_tables")
async def cb_admin_repair_tables(callback: types.CallbackQuery):
  await callback.answer("Проверяю формулы...")
  ok = await asyncio.to_thread(repair_spreadsheet_sync)
  if ok:
    await callback.message.answer("✅ Формулы выплат успешно синхронизированы!")
  else:
    await callback.message.answer("⚠️ Не удалось обновить формулы.")
  await render_admin_menu(callback)


@dp.callback_query(F.data == "admin_view_active_shifts")
async def cb_view_active_shifts(callback: types.CallbackQuery):
  await callback.answer()
  if not active_shifts:
    await callback.message.edit_text(
        "⏱ <b>Активные смены команды</b>\n\n"
        "Сейчас никто из сотрудников не находится на смене.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data="admin_hub"
                )
            ]]
        ),
        parse_mode="HTML",
    )
    return

  now = get_now_astana()
  text = "⏱ <b>Активные смены команды прямо сейчас:</b>\n\n"
  for u_id, shift in active_shifts.items():
    start_dt = shift["start_time"]
    duration = now - start_dt
    hrs = int(duration.total_seconds() // 3600)
    mins = int((duration.total_seconds() % 3600) // 60)
    wait_status = (
        " ⚠️ <i>(ждём ответ на 6ч таймер)</i>"
        if shift.get("awaiting_extension")
        else ""
    )
    text += (
        f"👤 <b>{shift.get('employee', 'Сотрудник')}</b>{wait_status}\n"
        f"📅 Дата: <i>{shift['project']}</i>\n"
        f"🕒 Начал: <b>{start_dt.strftime('%H:%M')}</b> (прошло: {hrs} ч."
        f" {mins} мин.)\n\n"
    )

  await callback.message.edit_text(
      text,
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[[
              InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hub")
          ]]
      ),
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "admin_manage_roles")
async def cb_admin_manage_roles(callback: types.CallbackQuery):
  await callback.answer()
  if not is_admin(callback.from_user.id):
    return
  team = await asyncio.to_thread(get_all_team_roles_sync)
  kb = []
  for name, role in team:
    is_m = "управл" in role.lower() or "менедж" in role.lower()
    icon = "⭐️ [Управляющий]" if is_m else "👤 [Команда]"
    kb.append([
        InlineKeyboardButton(
            text=f"{icon} {name}", callback_data=f"togglerole_{name[:20]}"
        )
    ])
  kb.append(
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hub")]
  )
  await callback.message.edit_text(
      "👥 <b>Управление ролями команды</b>\n\n"
      "Нажмите на сотрудника, чтобы сделать его <b>Управляющим</b> (или вернуть"
      " в Команду):",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("togglerole_"))
async def cb_toggle_role(callback: types.CallbackQuery):
  await callback.answer()
  emp_prefix = callback.data.replace("togglerole_", "")
  team = await asyncio.to_thread(get_all_team_roles_sync)
  target_name = next(
      (n for n, r in team if n.startswith(emp_prefix)), emp_prefix
  )
  curr_role = next((r for n, r in team if n == target_name), "Команда")
  new_role = (
      "Команда"
      if ("управл" in curr_role.lower() or "менедж" in curr_role.lower())
      else "Управляющий"
  )

  await asyncio.to_thread(set_employee_role_sync, target_name, new_role)
  await asyncio.to_thread(load_managers_from_sheets_sync)
  await cb_admin_manage_roles(callback)


# --- ВЫПЛАТЫ КОМАНДЕ (ПРОИЗВОЛЬНАЯ СУММА + АВАНС) ---


@dp.callback_query(F.data == "admin_payouts_list")
async def cb_admin_payouts_list(callback: types.CallbackQuery):
  await callback.answer()
  if not is_admin(callback.from_user.id):
    return

  debts = await asyncio.to_thread(get_debts_summary_sync)
  buttons = []
  if not debts:
    text = "💼 <b>Выплаты команде</b>\n\n🎉 Все долги закрыты!"
  else:
    total_debt = sum(debts.values())
    text = (
        f"💼 <b>Выплаты команде</b>\n\n💵 Общий долг: <b>{total_debt:,.0f} ₸</b>\n\n"
    )
    for person, sum_amt in debts.items():
      text += f"• <b>{person}</b>: {sum_amt:,.0f} ₸\n"
      buttons.append([
          InlineKeyboardButton(
              text=f"💸 Выплатить: {person} ({sum_amt:,.0f} ₸)",
              callback_data=f"payprep_{person[:25]}",
          )
      ])

  buttons.append(
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hub")]
  )
  kb = InlineKeyboardMarkup(inline_keyboard=buttons)
  await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("payprep_"))
async def cb_pay_person_prep(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  if not is_admin(callback.from_user.id):
    return

  person_prefix = callback.data.replace("payprep_", "")
  debts = await asyncio.to_thread(get_debts_summary_sync)
  full_name = next(
      (p for p in debts if p.startswith(person_prefix)), person_prefix
  )
  debt_amount = debts.get(full_name, 0.0)
  reqs = await asyncio.to_thread(get_employee_requisites_sync, full_name)

  await state.update_data(target_employee=full_name, current_debt=debt_amount)

  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=f"✅ Выплатить ровно долг ({debt_amount:,.0f} ₸)",
                  callback_data=f"confirmpay_exact_{person_prefix[:20]}",
              )
          ],
          [
              InlineKeyboardButton(
                  text="✏️ Ввести другую сумму (с переплатой/авансом)",
                  callback_data="pay_custom_amount",
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
      f"💼 <b>Выплата сотруднику:</b> {full_name}\n\n"
      f"💵 <b>Текущий долг к выплате:</b> <code>{debt_amount:,.0f}</code> ₸\n\n"
      f"📋 <b>Реквизиты Kaspi:</b>\n<code>{reqs}</code>\n\n"
      "<i>Выберите действие или введите точную сумму перевода:</i>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("confirmpay_exact_"))
async def cb_confirm_exact_pay(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer("Списываю...")
  data = await state.get_data()
  emp_name = data.get("target_employee")
  debt = data.get("current_debt", 0.0)
  await state.clear()

  await asyncio.to_thread(process_custom_payout_sync, emp_name, debt, debt)
  await callback.message.edit_text(
      f"✅ Долг сотруднику <b>{emp_name}</b> на сумму <b>{debt:,.0f} ₸</b>"
      " полностью погашен!",
      parse_mode="HTML",
  )
  await render_admin_menu(callback)


@dp.callback_query(F.data == "pay_custom_amount")
async def cb_ask_custom_amount(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  data = await state.get_data()
  emp_name = data.get("target_employee", "Сотрудник")
  debt = data.get("current_debt", 0.0)

  await state.set_state(CustomPayoutState.waiting_for_amount)
  await callback.message.edit_text(
      f"👤 Сотрудник: <b>{emp_name}</b> (долг: {debt:,.0f} ₸)\n\n"
      "💸 <b>Введите фактически переведенную сумму числом:</b>\n"
      "<i>(Если вы перевели больше долга, разница автоматически перейдет на его"
      " баланс как аванс)</i>\n\n"
      "Например: <code>50000</code>",
      parse_mode="HTML",
  )


@dp.message(CustomPayoutState.waiting_for_amount, F.text)
async def process_custom_amount_input(
    message: types.Message, state: FSMContext
):
  try:
    payout_val = float(message.text.replace(" ", "").replace(",", ".").strip())
  except ValueError:
    await message.answer("⚠️ Введите число (например: 50000):")
    return

  data = await state.get_data()
  emp_name = data.get("target_employee", "Сотрудник")
  debt = data.get("current_debt", 0.0)
  await state.clear()

  wait_msg = await message.answer("⏳ Обновляю баланс в таблице...")
  await asyncio.to_thread(
      process_custom_payout_sync, emp_name, payout_val, debt
  )

  diff = payout_val - debt
  if diff > 0.01:
    res_text = (
        f"✅ <b>Выплата проведена с переплатой!</b>\n\n"
        f"👤 Сотрудник: <b>{emp_name}</b>\n"
        f"💵 Долг был: {debt:,.0f} ₸\n"
        f"💳 Переведено: <b>{payout_val:,.0f} ₸</b>\n"
        f"🟢 <b>Аванс на баланс сотрудника: +{diff:,.0f} ₸</b>\n\n"
        "<i>Эта переплата автоматически зачтется в следующих расходах!</i>"
    )
  elif diff < -0.01:
    res_text = (
        f"✅ <b>Частичная выплата проведена!</b>\n\n"
        f"👤 Сотрудник: <b>{emp_name}</b>\n"
        f"💵 Долг был: {debt:,.0f} ₸\n"
        f"💳 Выплачено: <b>{payout_val:,.0f} ₸</b>\n"
        f"⚠️ <b>Остаток долга: {abs(diff):,.0f} ₸</b>"
    )
  else:
    res_text = (
        f"✅ Долг сотруднику <b>{emp_name}</b> на сумму <b>{payout_val:,.0f}"
        " ₸</b> закрыт ровно в ноль!"
    )

  await wait_msg.edit_text(
      res_text,
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[[
              InlineKeyboardButton(
                  text="⬅️ В панель управления", callback_data="admin_hub"
              )
          ]]
      ),
      parse_mode="HTML",
  )


# --- БЫСТРЫЙ РАСХОД ПО ДАТЕ КАЛЕНДАРЯ ---


@dp.callback_query(F.data == "admin_fast_expense")
async def cb_fast_exp_start(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  kb = build_quick_date_keyboard("fastexp")
  await callback.message.edit_text(
      "⚡️ <b>Быстрый расход</b>\nВыберите дату расхода в календаре:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("pickdate_fastexp_"))
async def cb_fast_exp_date(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  raw_date = callback.data.replace("pickdate_fastexp_", "")
  date_label = await resolve_date_or_calendar_label(raw_date)

  await state.update_data(project=date_label)
  await state.set_state(QuickExpenseState.waiting_for_details)
  await callback.message.edit_text(
      f"📅 Дата: <b>{date_label}</b>\n\n"
      "Напишите сумму и суть одной строчкой:\n"
      "<i>Например: <code>75000 субаренда микрофонов</code> или <code>20000"
      " газель лично</code></i>",
      parse_mode="HTML",
  )


@dp.message(QuickExpenseState.waiting_for_details, F.text)
async def process_fast_exp(message: types.Message, state: FSMContext):
  text_input = message.text.strip()
  data = await state.get_data()
  date_label = data.get("project", get_now_astana().strftime("%d.%m.%Y"))
  await state.clear()

  status_msg = await message.answer("🤖 Разбираю расход...")
  prompt = f"""
    Расход: "{text_input}".
    Извлеки JSON:
    - "amount": число,
    - "category": категория ("Субаренда оборудования", "Гонорары наемным техникам", "Такси / Логистика / ГСМ", "Расходники (тейп, батарейки)", "Склад / Ремонт оборудования", "Прочее"),
    - "description": суть покупки,
    - "is_personal": true если написано "лично", "из своих", иначе false.
    """
  try:
    resp_text = await call_gemini_safe(prompt, json_mode=True)
    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    exp_data = json.loads(raw[start:end])

    amount = float(exp_data.get("amount", 0))
    category = exp_data.get("category", "Прочее")
    desc = exp_data.get("description", text_input)
    is_personal = exp_data.get("is_personal", False)

    payer = (
        await get_or_register_employee(message.from_user)
        if is_personal
        else "Касса / ИП"
    )
    status_str = "К возмещению" if is_personal else "Не требуется"

    await asyncio.to_thread(
        log_expense_sync,
        date_label,
        category,
        amount,
        message.from_user,
        desc,
        "Вручную (быстрый ввод)",
        payer,
        status_str,
    )
    await status_msg.edit_text(
        f"✅ <b>Расход внесен!</b>\n📅 {date_label}\n💵 {amount:,.0f} ₸ ({category})\n💳"
        f" {payer} ({status_str})",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="⬅️ В панель управления", callback_data="admin_hub"
                )
            ]]
        ),
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка: {e}")


# --- ПЕРЕХВАТ ТЕКСТА РЕКВИЗИТОВ ---


@dp.message(
    StateFilter(None),
    F.text & ~F.text.startswith("/") & ~F.text.in_(NAV_BUTTONS),
)
async def handle_user_text(message: types.Message):
  user_id = message.from_user.id
  if user_id in waiting_for_requisites:
    del waiting_for_requisites[user_id]
    employee = await get_or_register_employee(message.from_user)
    await asyncio.to_thread(
        save_employee_requisites_sync, employee, message.text.strip()
    )
    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n\n👤 {employee}\n📋"
        f" <code>{message.text.strip()}</code>",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )


# --- ТОЧКА ВХОДА И СЕРВЕР RENDER ---


async def handle_ping(request):
  return web.Response(text="Bot is active in Astana UTC+5!")


async def main():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 8080))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()

  print(
      f"Сервер слушает порт {port}, время Астаны:"
      f" {get_now_astana().strftime('%Y-%m-%d %H:%M:%S')}"
  )
  await asyncio.to_thread(repair_spreadsheet_sync)
  await asyncio.to_thread(load_managers_from_sheets_sync)

  # Запуск фонового таймера проверки смен (каждые 6 часов)
  asyncio.create_task(shift_monitor_loop())

  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())