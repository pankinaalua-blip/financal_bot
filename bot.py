import asyncio
from datetime import datetime, timedelta
import io
import json
import os
import random
import re
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
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
import gspread

# 1. Загрузка конфигурации
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "disa2026").strip()
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "disa_manager").strip()

raw_admins = os.getenv("ADMIN_IDS", "")
INITIAL_ADMINS = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

# Часовой пояс Астаны (UTC+5)
TZ_ASTANA = ZoneInfo("Asia/Almaty")


def get_now_astana() -> datetime:
  return datetime.now(TZ_ASTANA)


SPREADSHEET_ID = "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA"

# 2. Инициализация Telegram и Google AI
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_KEY)

# 3. Подключение к Google Таблицам
gc = gspread.service_account(filename="credentials.json")
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

authenticated_admins = set(INITIAL_ADMINS)
authenticated_managers = set()
employee_cache = {}

pending_receipts = {}
active_shifts = {}
waiting_for_requisites = {}
pending_shift_requests = {}
saved_receipt_file_ids = set()


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


# --- ИИ: YANDEX SPEECHKIT + КАСКАД GEMINI 3.6 ---


async def recognize_speech_yandex(audio_bytes: bytes) -> str | None:
  if not YANDEX_API_KEY:
    return None
  url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
  headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
  params = {"topic": "general", "lang": "ru-RU", "format": "oggopus"}
  try:
    async with aiohttp.ClientSession() as session:
      async with session.post(
          url, headers=headers, params=params, data=audio_bytes, timeout=10
      ) as resp:
        if resp.status == 200:
          data = await resp.json()
          return data.get("result", "").strip()
  except Exception as e:
    print(f"Ошибка SpeechKit: {e}")
  return None


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


# --- GOOGLE DRIVE: СОХРАНЕНИЕ ЧЕКОВ ---


def upload_receipt_to_drive_sync(
    file_bytes: bytes, filename: str
) -> str | None:
  try:
    creds = gc.auth
    token = creds.token
    if not token:
      creds.refresh(None)
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

    import urllib.request

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


class ProjectCreationState(StatesGroup):
  waiting_for_input = State()
  editing_field = State()


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
    ws_proj = spreadsheet.worksheet("Проекты")
    proj_rows = ws_proj.get_all_values()
    for r_idx in range(2, len(proj_rows) + 1):
      proj_name = ws_proj.cell(r_idx, 1).value
      if not proj_name:
        continue
      formula_exp = f'=SUMIFS(Операции!F:F; Операции!D:D; A{r_idx}; Операции!C:C; "Расход")'
      formula_prof = f"=D{r_idx}-E{r_idx}"
      formula_marg = f"=IF(D{r_idx}>0; F{r_idx}/D{r_idx}; 0)"
      ws_proj.update_cell(r_idx, 5, formula_exp)
      ws_proj.update_cell(r_idx, 6, formula_prof)
      ws_proj.update_cell(r_idx, 7, formula_marg)

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


def get_filtered_projects_sync(show_all: bool = False) -> list[str]:
  try:
    ws = spreadsheet.worksheet("Проекты")
    rows = ws.get_all_records()
    now = get_now_astana()
    current_year = now.year

    filtered = []
    for r in rows:
      proj = str(r.get("Проект", "")).strip()
      status = str(r.get("Статус проекта", "")).strip().lower()
      if not proj or status in ["завершен", "архив", "отменен"]:
        continue
      if proj == "Склад / Общее":
        continue
      if show_all:
        filtered.append(proj)
        continue

      date_match = re.search(r"\b(\d{1,2})[./](\d{1,2})\b", proj)
      if date_match:
        try:
          day, month = int(date_match.group(1)), int(date_match.group(2))
          p_date = datetime(current_year, month, day, tzinfo=TZ_ASTANA)
          diff_days = (p_date.date() - now.date()).days
          if -3 <= diff_days <= 10:
            filtered.append(proj)
        except ValueError:
          filtered.append(proj)
      else:
        filtered.append(proj)

    filtered.insert(0, "Склад / Общее")
    return filtered
  except Exception as e:
    print(f"Ошибка получения проектов: {e}")
    return ["Склад / Общее"]


def set_project_status_sync(
    proj_prefix: str, new_status: str = "Завершен"
) -> str | None:
  try:
    ws = spreadsheet.worksheet("Проекты")
    all_values = ws.get_all_values()
    if not all_values:
      return None

    status_col_idx = 3
    proj_col_idx = 1
    headers = [str(h).strip().lower() for h in all_values[0]]
    for idx, h in enumerate(headers, start=1):
      if "статус" in h:
        status_col_idx = idx
      elif "проект" in h:
        proj_col_idx = idx

    for r_idx, row in enumerate(all_values[1:], start=2):
      if len(row) >= proj_col_idx:
        proj_name = row[proj_col_idx - 1].strip()
        if proj_name.startswith(proj_prefix) or proj_prefix in proj_name:
          ws.update_cell(r_idx, status_col_idx, new_status)
          return proj_name
    return None
  except Exception as e:
    print(f"Ошибка статуса: {e}")
    return None


def log_expense_sync(
    project: str,
    category: str,
    amount: float,
    user: types.User,
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
          project,
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


# --- КЛАВИАТУРЫ ---


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


async def render_project_keyboard(
    callback_prefix: str, show_all: bool = False
) -> InlineKeyboardMarkup:
  projects = await asyncio.to_thread(get_filtered_projects_sync, show_all)
  kb_buttons = [
      [
          InlineKeyboardButton(
              text=f"📌 {p}", callback_data=f"{callback_prefix}_{p[:25]}"
          )
      ]
      for p in projects
  ]
  if not show_all:
    kb_buttons.append([
        InlineKeyboardButton(
            text="📂 Показать все проекты",
            callback_data=f"{callback_prefix}_showall",
        )
    ])
  return InlineKeyboardMarkup(inline_keyboard=kb_buttons)


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
        "🔓 <b>Доступ управляющего проектами активирован!</b>",
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
      "Бот учета сценического оборудования готов к работе:\n"
      "• 🚕 <b>Такси</b> — ночные поездки (22:00–07:00) сразу к выплате\n"
      "• 🍔 <b>Обеды</b> — начисление обеденных 2 500 ₸\n"
      "• 🧾 <b>Расходы</b> — отправка любых чеков по проектам\n"
      "• ⏱ <b>Моя смена</b> — фиксация рабочих часов",
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


# --- РАЗДЕЛ: ОБЕДЫ ---


@dp.message(F.text == "🍔 Обеды")
async def cmd_meals_menu(message: types.Message):
  kb = await render_project_keyboard("meal", show_all=False)
  await message.answer(
      "🍔 <b>Обеденные (2 500 ₸)</b>\nВыберите проект, на котором работаете:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "meal_showall")
async def cb_meal_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("meal", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("meal_") & (F.data != "meal_showall"))
async def cb_confirm_meal(callback: types.CallbackQuery):
  await callback.answer("Записываю...")
  project_short = callback.data.replace("meal_", "")
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  full_project = next(
      (p for p in projects if p.startswith(project_short)), "Склад / Общее"
  )

  if callback.from_user.id in active_shifts:
    active_shifts[callback.from_user.id]["claimed_meals"] += 1

  await asyncio.to_thread(
      log_expense_sync,
      full_project,
      "Питание команды",
      2500,
      callback.from_user,
      "Обеденные (быстрая выплата)",
  )
  await callback.message.edit_text(
      f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n🎯 Проект: {full_project}",
      parse_mode="HTML",
  )


# --- РАЗДЕЛ: СМЕНА ---


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
                    text="🕒 Начать задним числом",
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🔴 Завершить смену", callback_data="end_shift"
            )
        ]]
    )
    await message.answer(
        f"⏱ <b>Смена в процессе:</b> <b>{shift['project']}</b>\n"
        f"🕒 Старт: {shift['start_time'].strftime('%H:%M')}\n"
        f"⏳ Прошло: <b>{hours} ч. {minutes} мин.</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "shift_mode_now")
async def cb_shift_mode_now(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  await state.update_data(is_retro=False)
  kb = await render_project_keyboard("startshift", show_all=False)
  await callback.message.edit_text(
      "Выберите проект для старта:", reply_markup=kb, parse_mode="HTML"
  )


@dp.callback_query(F.data == "shift_mode_retro")
async def cb_shift_mode_retro(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  await state.update_data(is_retro=True)
  kb = await render_project_keyboard("startshift", show_all=False)
  await callback.message.edit_text(
      "Выберите проект для смены задним числом:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "startshift_showall")
async def cb_startshift_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("startshift", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(
    F.data.startswith("startshift_") & (F.data != "startshift_showall")
)
async def cb_choose_project_shift(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  proj_prefix = callback.data.replace("startshift_", "")
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  full_project = next(
      (p for p in projects if p.startswith(proj_prefix)), "Склад / Общее"
  )

  data = await state.get_data()
  is_retro = data.get("is_retro", False)
  emp_name = await get_or_register_employee(callback.from_user)

  if not is_retro:
    active_shifts[callback.from_user.id] = {
        "project": full_project,
        "start_time": get_now_astana(),
        "employee": emp_name,
        "claimed_meals": 0,
    }
    await callback.message.edit_text(
        f"🟢 <b>Смена начата!</b>\n🎯 Проект: <b>{full_project}</b>\n🕒 Время"
        f" старта: {get_now_astana().strftime('%H:%M')}",
        parse_mode="HTML",
    )
  else:
    await state.update_data(project=full_project)
    await state.set_state(RetroShiftState.waiting_for_time)
    await callback.message.edit_text(
        f"🎯 Проект: <b>{full_project}</b>\n\n"
        "🕒 <b>Во сколько фактически началась ваша смена?</b>\n"
        "Напишите время в формате <code>14:00</code> или <code>2 часа"
        " назад</code>:",
        parse_mode="HTML",
    )


@dp.message(RetroShiftState.waiting_for_time, F.text)
async def process_retro_shift_time(message: types.Message, state: FSMContext):
  time_text = message.text.strip()
  data = await state.get_data()
  project = data.get("project", "Склад / Общее")
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
      "project": project,
      "start_time": target_dt,
      "employee": employee,
  }

  kb = InlineKeyboardMarkup(
      inline_keyboard=[[
          InlineKeyboardButton(
              text="✅ Подтвердить старт", callback_data=f"apprshift_{req_id}"
          ),
          InlineKeyboardButton(
              text="❌ Отклонить", callback_data=f"rejshift_{req_id}"
          ),
      ]]
  )

  approvers = authenticated_admins | authenticated_managers
  for approver_id in approvers:
    try:
      await bot.send_message(
          approver_id,
          f"⚠️ <b>Запрос на смену задним числом!</b>\n\n"
          f"👤 Сотрудник: <b>{employee}</b>\n"
          f"🎯 Проект: {project}\n"
          f"🕒 Указал старт: <b>{target_dt.strftime('%H:%M (%d.%m)')}</b>\n\n"
          "Подтвердить начало смены?",
          reply_markup=kb,
          parse_mode="HTML",
      )
    except Exception:
      pass

  await message.answer(
      "⏳ <b>Запрос отправлен управляющему на согласование.</b>\n"
      f"После подтверждения ваша смена запустится с {target_dt.strftime('%H:%M')}.",
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("apprshift_"))
async def cb_appr_shift(callback: types.CallbackQuery):
  await callback.answer()
  req_id = callback.data.replace("apprshift_", "")
  shift_info = pending_shift_requests.pop(req_id, None)
  if not shift_info:
    await callback.message.edit_text("Смена уже была обработана.")
    return

  u_id = shift_info["user_id"]
  active_shifts[u_id] = {
      "project": shift_info["project"],
      "start_time": shift_info["start_time"],
      "employee": shift_info["employee"],
      "claimed_meals": 0,
  }

  await callback.message.edit_text(
      f"✅ Смена сотрудника <b>{shift_info['employee']}</b> подтверждена с"
      f" {shift_info['start_time'].strftime('%H:%M')}!",
      parse_mode="HTML",
  )
  try:
    await bot.send_message(
        u_id,
        f"✅ <b>Управляющий подтвердил вашу смену!</b>\n🎯"
        f" {shift_info['project']}\n🕒 Старт зафиксирован:"
        f" {shift_info['start_time'].strftime('%H:%M')}",
        parse_mode="HTML",
    )
  except Exception:
    pass


@dp.callback_query(F.data.startswith("rejshift_"))
async def cb_rej_shift(callback: types.CallbackQuery):
  await callback.answer()
  req_id = callback.data.replace("rejshift_", "")
  shift_info = pending_shift_requests.pop(req_id, None)
  if shift_info:
    try:
      await bot.send_message(
          shift_info["user_id"],
          "❌ <b>Запрос на смену задним числом отклонен.</b>",
          parse_mode="HTML",
      )
    except Exception:
      pass
  await callback.message.edit_text("❌ Запрос на смену отклонен.")


@dp.callback_query(F.data == "end_shift")
async def cb_end_shift(callback: types.CallbackQuery):
  user_id = callback.from_user.id
  if user_id not in active_shifts:
    await callback.answer("Смена не найдена", show_alert=True)
    return

  await callback.answer("Завершаю...")
  shift = active_shifts.pop(user_id)
  duration = get_now_astana() - shift["start_time"]
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

  await callback.message.edit_text(
      f"🔴 <b>Смена завершена!</b>\n🎯 {shift['project']}\n⏱ {int(hours)} ч."
      f" {int((duration.total_seconds() % 3600) // 60)} мин.",
      parse_mode="HTML",
  )


# --- РАСХОДЫ И ЧЕКИ ---


@dp.message(F.text == "🧾 Расходы")
async def cmd_expenses_info(message: types.Message):
  await message.answer(
      "🧾 <b>Отправка чеков и накладных</b>\n\n"
      "Просто пришлите фото чека в этот чат. Бот распознает сумму, сохранит его"
      " на диск для просмотра с телефона и внесет в смету проекта.",
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
      f"receipt_{message.from_user.id}_{datetime.now().strftime('%d%m_%H%M%S')}.jpg",
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

    kb = await render_project_keyboard("proj", show_all=False)
    await status_msg.edit_text(
        f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({merchant})\n"
        f"📂 {category} — {data.get('description', '')}\n"
        f"👤 {employee}{extra_note}\n\n"
        "👉 <b>К какому проекту привязать чек?</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")


@dp.callback_query(F.data == "proj_showall")
async def cb_proj_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("proj", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("proj_") & (F.data != "proj_showall"))
async def process_project_choice(callback: types.CallbackQuery):
  await callback.answer("Записываю...")
  project_short = callback.data.replace("proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)
  if not receipt:
    await callback.answer("Данные чека устарели.", show_alert=True)
    return

  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  full_project = next(
      (p for p in projects if p.startswith(project_short)), "Склад / Общее"
  )

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
        full_project,
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
            f"🎯 Проект: {full_project}\n"
            f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
            f"🕒 Время поездки: {now.strftime('%H:%M')}\n\nОдобрить выплату?",
            reply_markup=kb_appr,
            parse_mode="HTML",
        )
      except Exception:
        pass

    del pending_receipts[user_id]
    await callback.message.edit_text(
        f"⏳ <b>Дневное такси отправлено на согласование управляющему!</b>\n\n🎯"
        f" {full_project}\n💵 {receipt['amount']:,.0f} ₸",
        parse_mode="HTML",
    )
    return

  # Ночное такси или обычный расход -> сразу к возмещению
  try:
    await asyncio.to_thread(
        log_expense_sync,
        full_project,
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
        f"🎯 Проект: {full_project}\n"
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
        "✅ <b>Дневное такси одобрено!</b> Сумма добавлена к выплате."
    )
  else:
    await callback.message.edit_text("⚠️ Ошибка обновления статуса.")


@dp.callback_query(F.data.startswith("rejtaxi_"))
async def cb_rej_taxi(callback: types.CallbackQuery):
  await callback.answer()
  tx_id = callback.data.replace("rejtaxi_", "")
  await asyncio.to_thread(update_expense_status_sync, tx_id, "Отклонено")
  await callback.message.edit_text("❌ Дневное такси отклонено.")


# --- ПАНЕЛЬ УПРАВЛЕНИЯ (ADMIN + MANAGERS) ---


@dp.message(F.text == "💼 Панель управления")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if not is_manager_or_admin(message.from_user.id):
    await message.answer("⛔️ Нет прав доступа.")
    return
  await render_admin_menu(message)


async def render_admin_menu(event_target):
  user_id = (
      event_target.from_user.id
      if isinstance(event_target, types.Message)
      else event_target.from_user.id
  )

  buttons = [
      [
          InlineKeyboardButton(
              text="➕ Создать проект (Голос/Текст)",
              callback_data="admin_create_project",
          ),
          InlineKeyboardButton(
              text="⚡️ Быстрый расход", callback_data="admin_fast_expense"
          ),
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
                text="🏁 Завершить проект", callback_data="admin_close_project"
            ),
            InlineKeyboardButton(
                text=f"💸 Выплаты ({len(debts)})",
                callback_data="admin_payouts_list",
            ),
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
        "💼 <b>Панель Управляющего проектами</b>\n\n"
        f"⏱ <b>Сотрудников на смене:</b> {len(active_shifts)}\n"
        "Вам доступно создание проектов, быстрые расходы и мониторинг смен:"
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
    text += (
        f"👤 <b>{shift.get('employee', 'Сотрудник')}</b>\n"
        f"🎯 Проект: <i>{shift['project']}</i>\n"
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
      " в Команду):\n"
      "<i>(Роль мгновенно сохранится в Google Таблицу)</i>",
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


@dp.callback_query(F.data == "admin_close_project")
async def cb_admin_close_project(callback: types.CallbackQuery):
  await callback.answer()
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  active_projs = [p for p in projects if p != "Склад / Общее"]
  if not active_projs:
    await callback.message.edit_text("🏁 Все проекты уже завершены!")
    return
  kb = [
      [
          InlineKeyboardButton(
              text=f"🏁 {p}", callback_data=f"closeproj_{p[:25]}"
          )
      ]
      for p in active_projs
  ]
  kb.append(
      [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_hub")]
  )
  await callback.message.edit_text(
      "Выберите проект для перевода в статус <b>«Завершен»</b>:",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("closeproj_"))
async def cb_confirm_close_proj(callback: types.CallbackQuery):
  await callback.answer("Завершаю...")
  proj_prefix = callback.data.replace("closeproj_", "")
  closed = await asyncio.to_thread(
      set_project_status_sync, proj_prefix, "Завершен"
  )
  await callback.message.edit_text(
      f"✅ Проект <b>{closed}</b> успешно завершен и скрыт из меню!",
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[[
              InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hub")
          ]]
      ),
      parse_mode="HTML",
  )


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
      " полностью погашен!"
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


# --- СОЗДАНИЕ МЕРОПРИЯТИЙ (ГОЛОС SPEECHKIT + GEMINI / ТЕКСТ) ---


@dp.callback_query(F.data == "admin_create_project")
async def cb_start_create_project(
    callback: types.CallbackQuery, state: FSMContext
):
  await callback.answer()
  await state.set_state(ProjectCreationState.waiting_for_input)
  await callback.message.edit_text(
      "➕ <b>Создание нового мероприятия</b>\n\n"
      "🎙 <b>Надиктуйте голосовое</b> ИЛИ ✍️ <b>напишите текстом:</b>\n\n"
      "<i>Пример:\n«25.09 Концерт Баста, Дворец Спорта, смета 1.5 млн,"
      " предоплата 50%»</i>",
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
      f"📅 <b>Дата:</b> {event_date}\n"
      f"🎯 <b>Название:</b> {proj_name}\n"
      f"📍 <b>Площадка:</b> {location}\n"
      f"💵 <b>Смета:</b> {price:,.0f} ₸\n"
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
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hub")],
      ]
  )
  if isinstance(target, types.Message):
    await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
  else:
    await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def parse_project_with_ai(
    message: types.Message, state: FSMContext, input_data
):
  status_msg = await message.answer("🎧 Распознаю проект...")
  prompt = """
    Ты финансовый ассистент компании сценического оборудования.
    Извлеки данные проекта и верни ТОЛЬКО JSON:
    {
      "event_date": "дата ДД.ММ (если не названа, текущая)",
      "project_name": "название без даты",
      "location": "площадка",
      "price": общая сумма числом,
      "prepay_percent": 0, 50 или 100
    }
    """
  try:
    if isinstance(input_data, bytes):
      recognized_text = await recognize_speech_yandex(input_data)
      contents = (
          [f'Текст: "{recognized_text}"', prompt]
          if recognized_text
          else [
              genai_types.Part.from_bytes(
                  data=input_data, mime_type="audio/ogg"
              ),
              prompt,
          ]
      )
    else:
      contents = [f'Текст: "{input_data}"', prompt]

    resp_text = await call_gemini_safe(contents, json_mode=True)
    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    data = json.loads(raw[start:end])

    await state.set_state(ProjectCreationState.waiting_for_input)
    await state.update_data(
        event_date=data.get(
            "event_date", get_now_astana().strftime("%d.%m")
        ),
        project_name=data.get("project_name", "Мероприятие"),
        location=data.get("location", "Не указана"),
        price=float(data.get("price", 0)),
        prepay_percent=int(data.get("prepay_percent", 0)),
    )
    await render_project_card(status_msg, await state.get_data())
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")


@dp.message(ProjectCreationState.waiting_for_input, F.voice | F.audio)
@dp.message(F.from_user.id.in_(authenticated_admins), F.voice | F.audio)
async def process_project_voice(message: types.Message, state: FSMContext):
  voice_obj = message.voice or message.audio
  file_io = io.BytesIO()
  await bot.download(voice_obj, destination=file_io)
  await parse_project_with_ai(message, state, file_io.getvalue())


@dp.message(ProjectCreationState.waiting_for_input, F.text)
async def process_project_text(message: types.Message, state: FSMContext):
  await parse_project_with_ai(message, state, message.text.strip())


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

  def add_proj():
    ws = spreadsheet.worksheet("Проекты")
    next_row = len(ws.col_values(1)) + 1
    event_date = data.get("event_date", "").strip()
    raw_name = data.get("project_name", "").strip()
    full_name = (
        f"{event_date} {raw_name}"
        if event_date and not raw_name.startswith(event_date)
        else raw_name
    )

    total_price = float(data.get("price", 0))
    prepay_pct = int(data.get("prepay_percent", 0))
    paid = total_price * (prepay_pct / 100.0)

    formula_exp = f'=SUMIFS(Операции!F:F; Операции!D:D; A{next_row}; Операции!C:C; "Расход")'
    formula_prof = f"=D{next_row}-E{next_row}"
    formula_marg = f"=IF(D{next_row}>0; F{next_row}/D{next_row}; 0)"

    ws.append_row(
        [
            full_name,
            data.get("location", "Площадка"),
            "В работе",
            total_price,
            formula_exp,
            formula_prof,
            formula_marg,
        ],
        value_input_option="USER_ENTERED",
    )
    if paid > 0:
      ws_ops = spreadsheet.worksheet("Операции")
      ws_ops.append_row(
          [
              f"TX-{random.randint(10000, 99999)}",
              get_now_astana().strftime("%d.%m.%Y %H:%M"),
              "Доход",
              full_name,
              "Комплексный продакшн",
              paid,
              "Клиент",
              "Не требуется",
              "-",
              f"Предоплата ({prepay_pct}%)",
          ],
          value_input_option="USER_ENTERED",
      )
    return full_name

  try:
    proj_title = await asyncio.to_thread(add_proj)
    await state.clear()
    await callback.message.edit_text(
        f"✅ <b>Мероприятие создано!</b>\n\n🎯 <b>{proj_title}</b>\n💵 Смета:"
        f" {data.get('price', 0):,.0f} ₸",
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
    await callback.message.edit_text(f"⚠️ Ошибка: {e}")


# --- БЫСТРЫЙ РАСХОД (1 ШАГ) ---


@dp.callback_query(F.data == "admin_fast_expense")
async def cb_fast_exp_start(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  kb = await render_project_keyboard("fastexp", show_all=False)
  await callback.message.edit_text(
      "⚡️ <b>Быстрый расход</b>\nВыберите проект:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "fastexp_showall")
async def cb_fastexp_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("fastexp", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(
    F.data.startswith("fastexp_") & (F.data != "fastexp_showall")
)
async def cb_fast_exp_proj(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  proj_short = callback.data.replace("fastexp_", "")
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  full_project = next(
      (p for p in projects if p.startswith(proj_short)), "Склад / Общее"
  )

  await state.update_data(project=full_project)
  await state.set_state(QuickExpenseState.waiting_for_details)
  await callback.message.edit_text(
      f"🎯 Проект: <b>{full_project}</b>\n\n"
      "Напишите сумму и суть одной строчкой:\n"
      "<i>Например: <code>75000 субаренда микрофонов</code> или <code>20000"
      " газель лично</code></i>",
      parse_mode="HTML",
  )


@dp.message(QuickExpenseState.waiting_for_details, F.text)
async def process_fast_exp(message: types.Message, state: FSMContext):
  text_input = message.text.strip()
  data = await state.get_data()
  project = data.get("project", "Склад / Общее")
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
        project,
        category,
        amount,
        message.from_user,
        desc,
        "Вручную (быстрый ввод)",
        payer,
        status_str,
    )
    await status_msg.edit_text(
        f"✅ <b>Расход внесен!</b>\n🎯 {project}\n💵 {amount:,.0f} ₸ ({category})\n💳"
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
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())