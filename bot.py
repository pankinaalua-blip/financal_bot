import asyncio
from datetime import datetime, timedelta
import io
import json
import os
import random
import re
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

# 3. Подключение к Google Таблицам
gc = gspread.service_account(filename="credentials.json")
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

employee_cache = {}
pending_receipts = {}
active_shifts = {}
waiting_for_requisites = {}
saved_receipt_file_ids = set()


# Функция защиты от ошибки 503 (Всегда 3.6 в приоритете + авто-повтор)
async def call_gemini_safe(contents, json_mode: bool = True):
  models_cascade = [
      "gemini-3.6-flash",  # 1-я попытка на 3.6
      "gemini-3.6-flash",  # Повтор на 3.6 при кратковременном скачке
      "gemini-2.5-flash",  # Мгновенная подстраховка
      "gemini-2.0-flash",  # Резерв
      "gemini-1.5-flash",  # Запасной шлюз
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
          model=model_name,
          contents=contents,
          config=config,
      )
      if resp and resp.text:
        return resp.text
    except Exception as e:
      print(f"Попытка на {model_name} временно не прошла ({e})...")
      # Если это первый сбой на 3.6, ждем секунду и повторяем
      if idx == 0:
        await asyncio.sleep(1.0)
      else:
        await asyncio.sleep(0.3)
      continue
  return None


class ProjectCreationState(StatesGroup):
  waiting_for_input = State()
  editing_field = State()


class QuickExpenseState(StatesGroup):
  waiting_for_details = State()


NAV_BUTTONS = [
    "📸 Как отправить чек",
    "🍔 Обеденные (2 500 ₸)",
    "⏱ Моя смена",
    "💰 Мои чеки",
    "💳 Мои реквизиты",
    "💼 Панель выплат (Admin)",
]


# --- ИСПРАВЛЕНИЕ ФОРМУЛ ---


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
    print(f"Ошибка при починке таблицы: {e}")
    return False


# --- СИНХРОНИЗАЦИЯ СОТРУДНИКОВ ---


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


# --- УМНАЯ ФИЛЬТРАЦИЯ ПРОЕКТОВ ---


def get_filtered_projects_sync(show_all: bool = False) -> list[str]:
  try:
    ws = spreadsheet.worksheet("Проекты")
    rows = ws.get_all_records()
    now = datetime.now()
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
          p_date = datetime(current_year, month, day)
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


def log_expense_sync(
    project: str,
    category: str,
    amount: float,
    user: types.User,
    comment: str,
    check_type: str = "Норматив (без чека)",
    payer: str = None,
    status: str = "К возмещению",
):
  employee = payer if payer else get_or_register_employee_sync(user)
  ws = spreadsheet.worksheet("Операции")
  tx_id = f"TX-{random.randint(10000, 99999)}"
  now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

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
          check_type,
          comment,
      ],
      value_input_option="USER_ENTERED",
  )


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


async def show_project_selection(
    target_msg, amount: float, merchant: str, data: dict, employee: str
):
  kb = await render_project_keyboard("proj", show_all=False)
  text = (
      f"🧾 <b>Чек:</b> {amount:,.0f} ₸ ({merchant})\n"
      f"📂 {data.get('category', 'Прочее')} — {data.get('description', '')}\n"
      f"👤 Сотрудник: <b>{employee}</b>\n\n"
      "👉 <b>К какому проекту привязать покупку?</b>\n"
      "<i>(Показаны актуальные на эту неделю)</i>"
  )

  if isinstance(target_msg, types.Message):
    await target_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
  else:
    await target_msg.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


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


# --- ОБРАБОТЧИКИ КОМАНД ---


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  name = await get_or_register_employee(message.from_user)
  kb = get_main_keyboard(message.from_user.id)
  await message.answer(
      f"Привет, {name}! 🎛️\n\n"
      "Я бот для учета расходов на площадках.\n"
      "• Отправь фото чека для распознавания\n"
      "• Жми «🍔 Обеденные» для суточных 2 500 ₸\n"
      "• Запускай «⏱ Моя смена» для учета времени.",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.message(Command("fix"))
async def cmd_fix_tables(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    return
  wait_m = await message.answer("⏳ Исправляю формулы в Google Таблице...")
  ok = await asyncio.to_thread(repair_spreadsheet_sync)
  if ok:
    await wait_m.edit_text("✅ Формулы в таблице восстановлены!", parse_mode="HTML")
  else:
    await wait_m.edit_text("⚠️ Ошибка при обновлении таблицы.")


@dp.message(F.text == "📸 Как отправить чек")
async def msg_how_to(message: types.Message):
  await message.answer(
      "📷 <b>Как отправить чек:</b>\n\n"
      "1. Нажмите на скрепку и отправьте фото чека (или скриншот Kaspi / Яндекс Go).\n"
      "2. Gemini автоматически определит сумму и категорию расхода.\n"
      "3. Выберите проект кнопкой.",
      parse_mode="HTML",
  )


@dp.message(F.text == "💰 Мои чеки")
async def msg_my_receipts(message: types.Message):
  status_wait = await message.answer("🔍 Проверяю ваши чеки...")
  name, total, items = await asyncio.to_thread(
      get_user_pending_receipts_sync, message.from_user
  )

  if not items:
    await status_wait.edit_text(
        f"👤 <b>{name}</b>\n\nУ вас нет активных чеков к возмещению.",
        parse_mode="HTML",
    )
    return

  text = f"👤 <b>Чеки к возмещению ({name})</b>\n\n💰 <b>Итого:</b> {total:,.0f} ₸\n\n"
  for it in items[:10]:
    text += f"• {it['amount']:,.0f} ₸ — <i>{it['project']}</i> ({it['comment']})\n"

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
      f"📋 <b>Данные:</b>\n<code>{reqs}</code>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_requisites(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  waiting_for_requisites[user_id] = True
  await callback.message.edit_text(
      "📝 <b>Введите ваши реквизиты (Kaspi номер, ИИН):</b>", parse_mode="HTML"
  )


# --- ОБЕДЕННЫЕ И СМЕНЫ ---


@dp.message(F.text == "🍔 Обеденные (2 500 ₸)")
async def cmd_quick_meal(message: types.Message):
  kb = await render_project_keyboard("meal", show_all=False)
  await message.answer(
      "🍔 <b>Обеденные (2 500 ₸)</b>\nВыберите проект на эту неделю:",
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
  await callback.answer("Записываю обеденные...")
  project_short = callback.data.replace("meal_", "")
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
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
      f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n🎯 Проект: {full_project}",
      parse_mode="HTML",
  )


@dp.message(F.text == "⏱ Моя смена")
async def cmd_shift_menu(message: types.Message):
  user_id = message.from_user.id
  if user_id not in active_shifts:
    kb = await render_project_keyboard("startshift", show_all=False)
    await message.answer(
        "⏱ <b>Учет смены</b>\nВыберите проект для старта:",
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
            [InlineKeyboardButton(text="🔴 Завершить смену", callback_data="end_shift")],
            [
                InlineKeyboardButton(
                    text="🍔 Взять обед сейчас (2 500 ₸)",
                    callback_data=f"meal_{shift['project'][:25]}",
                )
            ],
        ]
    )
    await message.answer(
        f"⏱ <b>Смена в процессе:</b> <b>{shift['project']}</b>\n"
        f"⏳ Прошло: <b>{hours} ч. {minutes} мин.</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "startshift_showall")
async def cb_startshift_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("startshift", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("startshift_") & (F.data != "startshift_showall"))
async def cb_start_shift(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  proj_prefix = callback.data.replace("startshift_", "")
  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
  full_project = next(
      (p for p in projects if p.startswith(proj_prefix)), "Склад / Общее"
  )

  active_shifts[user_id] = {
      "project": full_project,
      "start_time": datetime.now(),
      "claimed_meals": 0,
  }
  await callback.message.edit_text(
      f"🟢 <b>Смена начата!</b>\n🎯 Проект: <b>{full_project}</b>\n🕒 Время: {datetime.now().strftime('%H:%M')}",
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

  await callback.message.edit_text(
      f"🔴 <b>Смена завершена!</b>\n🎯 {shift['project']}\n⏱ {int(hours)} ч. {int((duration.total_seconds() % 3600) // 60)} мин.",
      parse_mode="HTML",
  )


# --- ПАНЕЛЬ ADMIN ---


@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  if message.from_user.id not in ADMIN_IDS:
    await message.answer("⛔️ Нет прав доступа.")
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
              text="➕ Создать проект (Голос/Текст)",
              callback_data="admin_create_project",
          ),
          InlineKeyboardButton(
              text="⚡️ Быстрый расход",
              callback_data="admin_fast_expense",
          ),
      ],
      [
          InlineKeyboardButton(
              text=f"💸 Выплаты команде ({len(debts)})",
              callback_data="admin_payouts_list",
          )
      ],
      [
          InlineKeyboardButton(
              text="🛠 Починить формулы в таблице",
              callback_data="admin_repair_tables",
          )
      ],
      [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_hub")],
  ]

  kb = InlineKeyboardMarkup(inline_keyboard=buttons)
  if isinstance(event_target, types.Message):
    await event_target.answer(text, reply_markup=kb, parse_mode="HTML")
  else:
    await event_target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin_repair_tables")
async def cb_admin_repair_tables(callback: types.CallbackQuery):
  await callback.answer("Исправляю формулы...")
  ok = await asyncio.to_thread(repair_spreadsheet_sync)
  if ok:
    await callback.message.answer("✅ Таблица успешно восстановлена!")
  else:
    await callback.message.answer("⚠️ Не удалось исправить таблицу.")
  await render_admin_menu(callback)


@dp.callback_query(F.data == "admin_hub")
async def cb_admin_hub(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return
  await state.clear()
  await render_admin_menu(callback)


# --- СОКРАЩЕННЫЙ РАСХОД: В 2 ШАГА ---


@dp.callback_query(F.data == "admin_fast_expense")
async def cb_fast_expense_start(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  await state.clear()
  kb = await render_project_keyboard("fastexp", show_all=False)
  await callback.message.edit_text(
      "⚡️ <b>Быстрый расход (Шаг 1 из 2)</b>\n\n"
      "Выберите проект, к которому относится расход:",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "fastexp_showall")
async def cb_fastexp_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("fastexp", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("fastexp_") & (F.data != "fastexp_showall"))
async def cb_fast_expense_choose_proj(callback: types.CallbackQuery, state: FSMContext):
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
      "<b>Шаг 2 из 2:</b> Напишите в чат сумму и суть одной строчкой.\n\n"
      "<i>Примеры:\n"
      "• <code>75000 субаренда микрофонов</code> (спишет с кассы/ИП)\n"
      "• <code>25000 газель доставка лично</code> (поставит к возмещению вам)</i>",
      parse_mode="HTML",
  )


@dp.message(QuickExpenseState.waiting_for_details, F.text)
async def process_fast_expense_details(message: types.Message, state: FSMContext):
  text_input = message.text.strip()
  data = await state.get_data()
  project = data.get("project", "Склад / Общее")
  await state.clear()

  status_msg = await message.answer("🤖 Разбираю расход через Gemini...")

  prompt = f"""
    Пользователь ввел расход по мероприятию: "{text_input}"
    Извлеки:
    - "amount": число (сумма расхода)
    - "category": одна из категорий ("Субаренда оборудования", "Гонорары наемным техникам", "Такси / Логистика / ГСМ", "Расходники (тейп, батарейки)", "Склад / Ремонт оборудования", "Питание команды", "Прочее")
    - "description": краткое описание покупки
    - "is_personal": true, если написано "лично", "из своих", "мне", иначе false (тогда касса ИП)
    Ответь ТОЛЬКО валидным JSON.
    """

  try:
    resp_text = await call_gemini_safe(prompt, json_mode=True)
    if not resp_text:
      await status_msg.edit_text("⚠️ Серверы временно перегружены. Попробуйте еще раз через секунду.")
      return

    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    exp_data = json.loads(raw[start:end])

    amount = float(exp_data.get("amount", 0))
    category = exp_data.get("category", "Прочее")
    desc = exp_data.get("description", text_input)
    is_personal = exp_data.get("is_personal", False)

    payer_name = (
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
        payer_name,
        status_str,
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ В панель управления", callback_data="admin_hub")
        ]]
    )
    await status_msg.edit_text(
        f"✅ <b>Расход внесен в 1 шаг!</b>\n\n"
        f"🎯 Проект: <b>{project}</b>\n"
        f"💵 Сумма: <b>{amount:,.0f} ₸</b>\n"
        f"📂 Категория: {category}\n"
        f"💳 Оплата: {payer_name} ({status_str})\n"
        f"📝 Детали: <i>{desc}</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")


# --- УНИВЕРСАЛЬНОЕ СОЗДАНИЕ ПРОЕКТА (ГОЛОС И ТЕКСТ) ---


@dp.callback_query(F.data == "admin_create_project")
async def cb_start_project_creation(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  await state.set_state(ProjectCreationState.waiting_for_input)
  await callback.message.edit_text(
      "➕ <b>Создание нового мероприятия</b>\n\n"
      "🎙 <b>Надиктуйте голосовое</b> ИЛИ ✍️ <b>напишите текстом:</b>\n\n"
      "<i>Пример текста:\n«25.09 Концерт Баста, Дворец Спорта, смета 1.5 млн, предоплата 50%»</i>\n\n"
      "Жду аудио или текст...",
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
          [InlineKeyboardButton(text="✅ Все верно, создать!", callback_data="confirm_voice_proj")],
          [
              InlineKeyboardButton(text="🔄 Заново", callback_data="admin_create_project"),
              InlineKeyboardButton(text="✏️ Исправить", callback_data="edit_voice_fields_menu"),
          ],
          [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hub")],
      ]
  )

  if isinstance(target, types.Message):
    await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
  else:
    await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


async def parse_project_with_gemini(message: types.Message, state: FSMContext, contents):
  status_msg = await message.answer("🤖 Распознаю проект (Gemini)...")
  prompt = """
    Ты финансовый ассистент компании аренды сценического оборудования.
    Извлеки данные о проекте и верни ТОЛЬКО валидный JSON:
    {
      "event_date": "дата ДД.ММ",
      "project_name": "название без даты (например: Концерт Баста)",
      "location": "площадка / отель",
      "price": общая сумма числом,
      "prepay_percent": 0, 50 или 100
    }
    """
  try:
    if isinstance(contents, bytes):
      parts = [genai_types.Part.from_bytes(data=contents, mime_type="audio/ogg"), prompt]
    else:
      parts = [contents, prompt]

    resp_text = await call_gemini_safe(parts, json_mode=True)
    if not resp_text:
      await status_msg.edit_text("⚠️ Серверы временно перегружены. Попробуйте еще раз.")
      return

    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    data = json.loads(raw[start:end])

    await state.set_state(ProjectCreationState.waiting_for_input)
    await state.update_data(
        event_date=data.get("event_date", datetime.now().strftime("%d.%m")),
        project_name=data.get("project_name", "Мероприятие"),
        location=data.get("location", "Не указана"),
        price=float(data.get("price", 0)),
        prepay_percent=int(data.get("prepay_percent", 0)),
    )
    await render_project_card(status_msg, await state.get_data())
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")


# Прием голоса для проекта
@dp.message(ProjectCreationState.waiting_for_input, F.voice | F.audio)
@dp.message(F.from_user.id.in_(ADMIN_IDS), F.voice | F.audio)
async def process_project_voice(message: types.Message, state: FSMContext):
  voice_obj = message.voice or message.audio
  file_io = io.BytesIO()
  await bot.download(voice_obj, destination=file_io)
  await parse_project_with_gemini(message, state, file_io.getvalue())


# Прием текста для проекта
@dp.message(ProjectCreationState.waiting_for_input, F.text)
async def process_project_text(message: types.Message, state: FSMContext):
  await parse_project_with_gemini(message, state, message.text.strip())


@dp.callback_query(F.data == "edit_voice_fields_menu")
async def cb_edit_fields_menu(callback: types.CallbackQuery):
  await callback.answer()
  kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(text="📅 Дату", callback_data="field_event_date"),
              InlineKeyboardButton(text="🎯 Название", callback_data="field_project_name"),
          ],
          [
              InlineKeyboardButton(text="📍 Локацию", callback_data="field_location"),
              InlineKeyboardButton(text="💵 Смету", callback_data="field_price"),
          ],
          [InlineKeyboardButton(text="💰 Предоплату (%)", callback_data="field_prepay_percent")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_card")],
      ]
  )
  await callback.message.edit_text("✏️ <b>Выберите поле для исправления:</b>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("field_"))
async def cb_select_field_to_edit(callback: types.CallbackQuery, state: FSMContext):
  await callback.answer()
  field_name = callback.data.replace("field_", "")
  await state.update_data(editing_target=field_name)
  await state.set_state(ProjectCreationState.editing_field)

  prompts = {
      "event_date": "Введите правильную <b>дату</b> (25.09):",
      "project_name": "Введите правильное <b>название</b>:",
      "location": "Введите <b>площадку</b>:",
      "price": "Введите <b>смету числом</b> (1500000):",
      "prepay_percent": "Введите <b>процент предоплаты (0, 50 или 100)</b>:",
  }
  await callback.message.edit_text(prompts.get(field_name, "Введите значение:"), parse_mode="HTML")


@dp.message(ProjectCreationState.editing_field, F.text)
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

  await state.set_state(ProjectCreationState.waiting_for_input)
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
  formula_profit = f"=D{next_row}-E{next_row}"
  formula_margin = f"=IF(D{next_row}>0; F{next_row}/D{next_row}; 0)"

  ws_proj.append_row(
      [
          full_proj_name,
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
    ws_ops.append_row(
        [
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
        ],
        value_input_option="USER_ENTERED",
    )

  return full_proj_name, total_price, paid_amount


@dp.callback_query(F.data == "confirm_voice_proj")
async def cb_confirm_voice_proj(callback: types.CallbackQuery, state: FSMContext):
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
            InlineKeyboardButton(text="⬅️ В панель управления", callback_data="admin_hub")
        ]]
    )

    await callback.message.edit_text(
        f"✅ <b>Мероприятие создано!</b>\n\n"
        f"🎯 <b>{full_proj_name}</b>\n"
        f"📍 Площадка: {data.get('location', 'Площадка')}\n"
        f"💵 Смета: {total_price:,.0f} ₸\n"
        f"💰 Предоплата: {paid_amount:,.0f} ₸",
        reply_markup=kb,
        parse_mode="HTML",
    )
  except Exception as e:
    await callback.message.edit_text(f"⚠️ Ошибка записи: {e}")


# --- ВЫПЛАТЫ КОМАНДЕ ---


@dp.callback_query(F.data == "admin_payouts_list")
async def cb_admin_payouts_list(callback: types.CallbackQuery):
  await callback.answer()
  if callback.from_user.id not in ADMIN_IDS:
    return

  debts = await asyncio.to_thread(get_debts_summary_sync)
  buttons = []

  if not debts:
    text = "💼 <b>Выплаты команде</b>\n\n🎉 Все долги закрыты!"
  else:
    total_debt = sum(debts.values())
    text = f"💼 <b>Выплаты команде</b>\n\n💵 Долг: {total_debt:,.0f} ₸\n\n"
    for person, sum_amt in debts.items():
      text += f"• <b>{person}</b>: {sum_amt:,.0f} ₸\n"
      buttons.append([
          InlineKeyboardButton(
              text=f"💸 Погасить: {person} ({sum_amt:,.0f} ₸)",
              callback_data=f"pay_{person[:25]}",
          )
      ])

  buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_hub")])
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
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_payouts_list")],
      ]
  )

  await callback.message.edit_text(
      f"💼 <b>Выплата:</b> {full_person_name}\n"
      f"💵 <b>Сумма:</b> <code>{amount:,.0f}</code> ₸\n\n"
      f"📋 <b>Реквизиты:</b>\n<code>{reqs}</code>",
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
    return
  await callback.answer("Списываю долг...")
  person_prefix = callback.data.replace("confirmpay_", "")
  await asyncio.to_thread(close_debt_sync, person_prefix)
  await render_admin_menu(callback)


# --- ПЕРЕХВАТ ТЕКСТА РЕКВИЗИТОВ ---


@dp.message(StateFilter(None), F.text & ~F.text.startswith("/") & ~F.text.in_(NAV_BUTTONS))
async def handle_user_text_input(message: types.Message):
  user_id = message.from_user.id
  if user_id in waiting_for_requisites:
    del waiting_for_requisites[user_id]
    employee = await get_or_register_employee(message.from_user)
    await asyncio.to_thread(
        save_employee_requisites_sync, employee, message.text.strip()
    )
    await message.answer(
        f"✅ <b>Реквизиты сохранены!</b>\n\n👤 {employee}\n📋 <code>{message.text.strip()}</code>",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="HTML",
    )


# --- РАСПОЗНАВАНИЕ ЧЕКОВ (ФОТО) С ЗАЩИТОЙ ОТ 503 ---


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  photo = message.photo[-1]
  if photo.file_unique_id in saved_receipt_file_ids:
    await message.answer("⚠️ Этот чек уже был успешно внесен ранее!")
    return

  status_msg = await message.answer("🔍 Распознаю чек...")
  file_io = io.BytesIO()
  await bot.download(photo, destination=file_io)

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
                data=file_io.getvalue(), mime_type="image/jpeg"
            ),
            prompt,
        ],
        json_mode=True,
    )

    if not resp_text:
      await status_msg.edit_text(
          "⚠️ Серверы Google перегружены. Пожалуйста, отправьте чек еще раз через секунду."
      )
      return

    raw = resp_text.strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    data = json.loads(raw[start:end])
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
              [InlineKeyboardButton(text="➕ Да, это отдельный чек", callback_data="confirm_duplicate_ok")],
              [InlineKeyboardButton(text="❌ Отмена (дубль)", callback_data="cancel_duplicate")],
          ]
      )
      await status_msg.edit_text(
          f"⚠️ <b>Похожий чек уже был сегодня!</b>\n💵 {duplicate['amount']:,.0f} ₸ ({merchant})\nЭто отдельная покупка?",
          reply_markup=kb_dup,
          parse_mode="HTML",
      )
      return

    await show_project_selection(status_msg, amount, merchant, data, employee)
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка разбора чека: {e}")


@dp.callback_query(F.data == "confirm_duplicate_ok")
async def cb_confirm_duplicate(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)
  if not receipt:
    await callback.answer("Данные устарели", show_alert=True)
    return

  await show_project_selection(
      callback, receipt["amount"], receipt["merchant"], receipt, receipt["user_name"]
  )


@dp.callback_query(F.data == "cancel_duplicate")
async def cb_cancel_duplicate(callback: types.CallbackQuery):
  await callback.answer()
  user_id = callback.from_user.id
  if user_id in pending_receipts:
    del pending_receipts[user_id]
  await callback.message.edit_text("❌ Загрузка отменена.")


@dp.callback_query(F.data == "proj_showall")
async def cb_proj_showall(callback: types.CallbackQuery):
  await callback.answer()
  kb = await render_project_keyboard("proj", show_all=True)
  await callback.message.edit_reply_markup(reply_markup=kb)


@dp.callback_query(F.data.startswith("proj_") & (F.data != "proj_showall"))
async def process_project_choice(callback: types.CallbackQuery):
  await callback.answer("Записываю чек...")
  project_short = callback.data.replace("proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)

  if not receipt:
    await callback.answer("Данные устарели.", show_alert=True)
    return

  projects = await asyncio.to_thread(get_filtered_projects_sync, True)
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
        f"✅ <b>Расход внесен в таблицу!</b>\n\n"
        f"🎯 Проект: {full_project}\n"
        f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
        f"👤 Сотрудник: {receipt['user_name']}",
        parse_mode="HTML",
    )
  except Exception as e:
    await status_update.edit_text(f"⚠️ Ошибка записи: {e}")


# --- ВЕБ-СЕРВЕР И ТОЧКА ВХОДА ---


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

  print("Синхронизируем формулы с таблицей...")
  await asyncio.to_thread(repair_spreadsheet_sync)

  print(f"Сервер слушает порт {port}, запускаем бота...")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())