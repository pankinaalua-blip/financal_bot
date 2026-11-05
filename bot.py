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
    InlineKeyboardButton as IBtn,
    InlineKeyboardMarkup as IMarkup,
    KeyboardButton as KBtn,
    ReplyKeyboardMarkup as RMarkup,
)
from aiohttp import web
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
import gspread

# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()

DEFAULT_SHEET = "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA"
DEFAULT_FOLDER = "13ze6D8nDnjGtZ7QDOQAr5iZnyrVnLcEH"

SPREADSHEET_ID = (os.getenv("SPREADSHEET_ID") or DEFAULT_SHEET).strip()
RECEIPTS_FOLDER_ID = (os.getenv("RECEIPTS_FOLDER_ID") or DEFAULT_FOLDER).strip()

ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "disa2026").strip()
MANAGER_PASSWORD = (os.getenv("MANAGER_PASSWORD") or "disa_manager").strip()

RAW_ADMINS = os.getenv("ADMIN_IDS") or "1642599250,5619982112"
INITIAL_ADMINS = [
    int(x.strip()) for x in RAW_ADMINS.split(",") if x.strip().isdigit()
]

TZ_ASTANA = ZoneInfo("Asia/Almaty")
SHIFT_INTERVAL_H = 6
SHIFT_TIMEOUT_M = 30

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_KEY)

gc = gspread.service_account(
    filename="credentials.json",
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

admins = set(INITIAL_ADMINS)
managers = set()
emp_cache = {}
active_shifts = {}
pending_receipts = {}
pending_shifts = {}
pending_meal_requests = {}
waiting_reqs = set()
saved_files = set()

MONTHS_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

NAV_BTNS = {
    "⏱ Моя смена", "🔴 Завершить смену", "🍔 Запросить обед", "🚕 Такси",
    "🧾 Прочие расходы", "💰 Мой баланс", "💳 Реквизиты", "⚙️ Управление",
}


class Form(StatesGroup):
  shift_comment = State()
  retro_time = State()
  retro_comment = State()
  retro_full_time = State()
  retro_full_comment = State()
  meal_comment = State()
  admin_topup = State()
  fast_expense = State()
  custom_payout = State()
  advance_amount = State()


def now_astana() -> datetime:
  return datetime.now(TZ_ASTANA)


def get_pay_period(dt: datetime | date) -> str:
  m_name = MONTHS_RU[dt.month]
  half = "01-15" if dt.day <= 15 else f"16-{calendar.monthrange(dt.year, dt.month)[1]}"
  return f"{m_name} {dt.year} ({half})"


def get_pay_period_from_str(date_str: str) -> str:
  try:
    dt = datetime.strptime(date_str.strip(), "%d.%m.%Y").date()
    return get_pay_period(dt)
  except ValueError:
    return get_pay_period(now_astana())


def is_admin(uid: int) -> bool:
  return uid in admins


def is_staff_lead(uid: int) -> bool:
  return uid in admins or uid in managers


def parse_kzt(val: str) -> float:
  try:
    cleaned = re.sub(r"[^\d.-]", "", str(val).replace(",", "."))
    return float(cleaned) if cleaned and cleaned not in ("-", ".", "-.") else 0.0
  except ValueError:
    return 0.0


# =====================================================================
# 2. ИИ И GOOGLE ДИСК
# =====================================================================


async def ask_gemini(contents) -> dict | None:
  models = ["gemini-2.8-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
  cfg = genai_types.GenerateContentConfig(
      response_mime_type="application/json",
      temperature=0.1,
  )
  for idx, m in enumerate(models):
    try:
      resp = await ai_client.aio.models.generate_content(
          model=m,
          contents=contents,
          config=cfg,
      )
      if resp and resp.text:
        raw = resp.text.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
          return json.loads(raw[start : end + 1])
    except Exception as e:
      print(f"Gemini OCR error on model {m}: {e}")
      await asyncio.sleep(0.5)
  return None


def upload_to_drive_sync(file_bytes: bytes, filename: str) -> str | None:
  try:
    creds = gc.auth
    if not creds.token:
      from google.auth.transport.requests import Request
      creds.refresh(Request())

    boundary = "===boundary==="
    metadata = {
        "name": filename,
        "mimeType": "image/jpeg",
    }
    if RECEIPTS_FOLDER_ID:
      metadata["parents"] = [RECEIPTS_FOLDER_ID]
    meta_bytes = json.dumps(metadata).encode()

    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta_bytes
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n\r\n".encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=webViewLink",
        data=body,
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
      return json.loads(r.read().decode()).get("webViewLink")
  except Exception as e:
    print(f"Drive upload error: {e}")
    return None


# =====================================================================
# 3. СИНХРОНИЗАЦИЯ, ИМЕНА И ТАБЛИЦА
# =====================================================================


def sync_roles_and_formulas():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    pay_rows = ws_pay.get_all_values()
    ws_ref = spreadsheet.worksheet("Справочники")
    ref_rows = ws_ref.get_all_records()

    mgr_names = set()
    cur_period = get_pay_period(now_astana())
    batch_data = []

    for idx, row in enumerate(pay_rows[1:], start=2):
      if not row or not row[0].strip():
        continue
      name = row[0].strip()
      role = row[1].strip().lower() if len(row) > 1 else ""
      if any(w in role for w in ["управл", "менедж", "manager"]):
        mgr_names.add(name.lower())

      batch_data.append({
          "range": f"D{idx}:K{idx}",
          "values": [[
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!E:E; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!F:F; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!G:G; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")',
              f"=E{idx}+F{idx}+G{idx}",
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{idx}; Операции!I:I; "Выплачено")',
              cur_period,
              f"=SUMIFS('Смены'!G:G; 'Смены'!D:D; A{idx}; 'Смены'!C:C; J{idx})",
          ]],
      })

    if batch_data:
      ws_pay.batch_update(batch_data, value_input_option="USER_ENTERED")

    managers.clear()
    for r in ref_rows:
      r_name = str(r.get("Имя", "")).strip().lower()
      cid = str(r.get("Chat ID", "")).strip()
      if cid.isdigit() and r_name in mgr_names:
        managers.add(int(cid))
  except Exception as e:
    print(f"Sync error: {e}")


def get_employee_sync(user: types.User) -> str:
  uid = str(user.id)
  if uid in emp_cache:
    return emp_cache[uid]

  uname = f"@{user.username.lower()}" if user.username else ""
  tg_display = (user.full_name or "Сотрудник").strip()
  matched_name = None

  try:
    ws_ref = spreadsheet.worksheet("Справочники")
    records = ws_ref.get_all_records()

    for r in records:
      if str(r.get("Chat ID", "")).strip() == uid:
        matched_name = str(r.get("Имя", "")).strip()
        break

    if not matched_name and uname:
      for r in records:
        if str(r.get("Telegram никнейм", "")).strip().lower() == uname:
          matched_name = str(r.get("Имя", "")).strip()
          idx = records.index(r) + 2
          ws_ref.update_cell(idx, 3, uid)
          break

    if not matched_name:
      matched_name = tg_display
      ws_ref.append_row([matched_name, f"@{user.username}" if user.username else "-", uid])

    ws_pay = spreadsheet.worksheet("Выплаты команде")
    existing_names = [x.strip().lower() for x in ws_pay.col_values(1)]
    if matched_name.lower() not in existing_names:
      r_idx = len(existing_names) + 1
      cur_period = get_pay_period(now_astana())
      ws_pay.append_row(
          [
              matched_name, "Команда", "-",
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!E:E; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!F:F; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!G:G; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f"=E{r_idx}+F{r_idx}+G{r_idx}",
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{r_idx}; Операции!I:I; "Выплачено")',
              cur_period,
              f"=SUMIFS('Смены'!G:G; 'Смены'!D:D; A{r_idx}; 'Смены'!C:C; J{r_idx})",
          ],
          value_input_option="USER_ENTERED",
      )
  except Exception as e:
    print(f"Emp sync error: {e}")
    matched_name = tg_display

  emp_cache[uid] = matched_name
  return matched_name


async def get_emp(user: types.User) -> str:
  return emp_cache.get(str(user.id)) or await asyncio.to_thread(get_employee_sync, user)


def log_shift_start_sync(shift_id: str, date_str: str, emp: str, start_dt: datetime, comment: str):
  try:
    ws = spreadsheet.worksheet("Смены")
    period = get_pay_period_from_str(date_str)
    ws.append_row(
        [
            shift_id, date_str, period, emp,
            start_dt.strftime("%H:%M"), "—", 0, comment, "В работе",
        ],
        value_input_option="USER_ENTERED",
    )
  except Exception as e:
    print(f"Shift start log error: {e}")


def log_shift_end_sync(shift_id: str, end_dt: datetime, hours_worked: float, auto: bool = False):
  try:
    ws = spreadsheet.worksheet("Смены")
    rows = ws.get_all_values()
    status = "Автозакрытие" if auto else "Завершена"
    h_val = round(hours_worked, 2)
    for idx, r in enumerate(rows[1:], start=2):
      if r and r[0].strip() == shift_id:
        ws.batch_update(
            [{
                "range": f"F{idx}:I{idx}",
                "values": [[end_dt.strftime("%H:%M"), h_val, r[7] if len(r) > 7 else "", status]],
            }],
            value_input_option="USER_ENTERED",
        )
        return
  except Exception as e:
    print(f"Shift end log error: {e}")


def log_full_completed_shift_sync(shift_id: str, date_str: str, emp: str, start_dt: datetime, end_dt: datetime, comment: str):
  try:
    ws = spreadsheet.worksheet("Смены")
    period = get_pay_period_from_str(date_str)
    if end_dt <= start_dt:
      end_dt += timedelta(days=1)
    dur = max(timedelta(0), end_dt - start_dt)
    hours_worked = round(dur.total_seconds() / 3600.0, 2)
    ws.append_row(
        [
            shift_id, date_str, period, emp,
            start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"),
            hours_worked, comment, "Завершена",
        ],
        value_input_option="USER_ENTERED",
    )
  except Exception as e:
    print(f"Full retro shift log error: {e}")


def get_admin_cash_summary_sync() -> dict:
  try:
    ws = spreadsheet.worksheet("Касса управляющего")
    vals = ws.get("K2:K4")
    if len(vals) >= 3:
      inc = parse_kzt(vals[0][0]) if vals[0] else 0.0
      exp = parse_kzt(vals[1][0]) if vals[1] else 0.0
      bal = parse_kzt(vals[2][0]) if vals[2] else (inc - exp)
      return {"income": inc, "expense": exp, "balance": bal}
    rows = ws.get_all_values()[1:]
    inc = sum(parse_kzt(r[5]) for r in rows if len(r) > 5)
    exp = sum(parse_kzt(r[6]) for r in rows if len(r) > 6)
    return {"income": inc, "expense": exp, "balance": inc - exp}
  except Exception as e:
    return {"income": 0.0, "expense": 0.0, "balance": 0.0}


def add_admin_cash_entry_sync(
    calc_date: str, op_type: str, category: str, income: float,
    expense: float, comment: str, drive_link: str = None,
):
  try:
    ws = spreadsheet.worksheet("Касса управляющего")
    tx = f"ADM-{random.randint(10000, 99999)}"
    period = get_pay_period_from_str(calc_date)
    safe_comment = (comment or "").replace('"', "«").strip()
    if drive_link and safe_comment:
      cell_h = f'=HYPERLINK("{drive_link}"; "📎 Чек | {safe_comment}")'
    elif drive_link:
      cell_h = f'=HYPERLINK("{drive_link}"; "📎 Открыть чек")'
    else:
      cell_h = safe_comment if safe_comment else "—"

    col_a = ws.col_values(1)
    next_row = len(col_a) + 1
    ws.update(
        f"A{next_row}:H{next_row}",
        [[tx, calc_date, period, op_type, category, income, expense, cell_h]],
        value_input_option="USER_ENTERED",
    )
  except Exception as e:
    print(f"Admin cash write error: {e}")


def get_all_team_members_sync():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    rows = ws_pay.get_all_values()[1:]
    team = []
    for r in rows:
      if r and r[0].strip():
        name = r[0].strip()
        bal = parse_kzt(r[3]) if len(r) > 3 else 0.0
        hrs = parse_kzt(r[10]) if len(r) > 10 else 0.0
        team.append((name, bal, hrs))
    return team
  except Exception as e:
    return []


def add_operation_sync(
    calc_date: str, category: str, amount: float, employee: str,
    comment: str, status: str = "К возмещению", drive_link: str = None, tx_id: str = None,
):
  ws = spreadsheet.worksheet("Операции")
  tx = tx_id or f"TX-{random.randint(10000, 99999)}"
  period = get_pay_period_from_str(calc_date)
  next_row = len(ws.get_all_values()) + 1
  taxi_val = amount if category == "Такси" else 0
  meals_val = amount if category == "Обеденные" else 0
  other_val = amount if category == "Прочие расходы" else 0
  safe_comment = (comment or "").replace('"', "«").strip()
  if drive_link and safe_comment:
    check_cell = f'=HYPERLINK("{drive_link}"; "📎 Чек | {safe_comment}")'
  elif drive_link:
    check_cell = f'=HYPERLINK("{drive_link}"; "📎 Открыть чек")'
  else:
    check_cell = safe_comment if safe_comment else "Без чека"
  ws.append_row(
      [tx, calc_date, period, employee, taxi_val, meals_val, other_val, f"=E{next_row}+F{next_row}+G{next_row}", status, check_cell],
      value_input_option="USER_ENTERED",
  )


def get_debts_sync(filter_emp: str = None):
  rows = spreadsheet.worksheet("Операции").get_all_values()[1:]
  debts = {}
  items = []
  for r in rows:
    if len(r) < 9 or "к возмещению" not in r[8].strip().lower():
      continue
    who = r[3].strip()
    if not who:
      continue
    taxi_amt = parse_kzt(r[4])
    meal_amt = parse_kzt(r[5])
    other_amt = parse_kzt(r[6])
    total_row = parse_kzt(r[7]) if len(r) > 7 and str(r[7]).strip() else (taxi_amt + meal_amt + other_amt)
    debts[who] = debts.get(who, 0.0) + total_row
    if filter_emp and who.lower() == filter_emp.strip().lower():
      cat = "Такси" if taxi_amt != 0 else ("Обеденные" if meal_amt != 0 else "Прочие расходы")
      items.append({"date": r[1], "cat": cat, "amount": total_row, "comment": r[9] if len(r) > 9 else "-"})
  return debts, items


# =====================================================================
# 4. КЛАВИАТУРЫ И КАЛЕНДАРЬ
# =====================================================================


def main_kb(uid: int) -> RMarkup:
  shift_btn = KBtn(text="🔴 Завершить смену") if uid in active_shifts else KBtn(text="⏱ Моя смена")
  kb = [
      [shift_btn, KBtn(text="🍔 Запросить обед")],
      [KBtn(text="🚕 Такси"), KBtn(text="🧾 Прочие расходы")],
      [KBtn(text="💰 Мой баланс"), KBtn(text="💳 Реквизиты")],
  ]
  if is_staff_lead(uid):
    kb.append([KBtn(text="⚙️ Управление")])
  return RMarkup(keyboard=kb, resize_keyboard=True)


def date_picker_kb(action: str) -> IMarkup:
  now = now_astana()
  d0 = now.strftime("%d.%m.%Y")
  d_prev = (now - timedelta(days=1)).strftime("%d.%m.%Y")
  return IMarkup(inline_keyboard=[
      [IBtn(text=f"📍 Сегодня ({d0})", callback_data=f"pdate|{action}|{d0}")],
      [IBtn(text=f"⏪ Вчера ({d_prev[:5]})", callback_data=f"pdate|{action}|{d_prev}")],
      [IBtn(text="🗓 Календарь", callback_data=f"cal|{action}|{now.year}|{now.month}")],
      [IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")],
  ])


def month_grid_kb(action: str, year: int, month: int) -> IMarkup:
  kb = [[IBtn(text=f"🗓 {MONTHS_RU[month]} {year}", callback_data="noop")]]
  kb.append([IBtn(text=d, callback_data="noop") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
  today = now_astana().date()
  for week in calendar.monthcalendar(year, month):
    row = []
    for day in week:
      if day == 0:
        row.append(IBtn(text=" ", callback_data="noop"))
      else:
        ds = f"{day:02d}.{month:02d}.{year}"
        lbl = f"•{day}•" if (year, month, day) == (today.year, today.month, today.day) else str(day)
        row.append(IBtn(text=lbl, callback_data=f"pdate|{action}|{ds}"))
    kb.append(row)
  py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
  ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
  kb.append([
      IBtn(text="⬅️", callback_data=f"cal|{action}|{py}|{pm}"),
      IBtn(text="🔙 Быстрые даты", callback_data=f"qdate|{action}"),
      IBtn(text="➡️", callback_data=f"cal|{action}|{ny}|{nm}"),
  ])
  kb.append([IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")])
  return IMarkup(inline_keyboard=kb)


@dp.callback_query(F.data == "noop")
async def cb_noop(c: types.CallbackQuery):
  await c.answer()


@dp.callback_query(F.data.startswith("cal|"))
async def cb_cal_nav(c: types.CallbackQuery):
  await c.answer()
  _, act, y, m = c.data.split("|")
  await c.message.edit_reply_markup(reply_markup=month_grid_kb(act, int(y), int(m)))


@dp.callback_query(F.data.startswith("qdate|"))
async def cb_qdate_nav(c: types.CallbackQuery):
  await c.answer()
  _, act = c.data.split("|")
  await c.message.edit_reply_markup(reply_markup=date_picker_kb(act))


# =====================================================================
# 5. СБРОС FSM И ОСНОВНОЕ МЕНЮ
# =====================================================================


@dp.message(Command("cancel"))
@dp.message(StateFilter("*"), F.text.in_(NAV_BTNS))
async def cancel_active_fsm_on_nav(m: types.Message, state: FSMContext):
  await state.clear()
  waiting_reqs.discard(m.from_user.id)
  if m.text in NAV_BTNS:
    if m.text == "⏱ Моя смена":
      await msg_shift(m)
    elif m.text == "🔴 Завершить смену":
      await msg_end_shift_direct(m)
    elif m.text == "🍔 Запросить обед":
      await msg_meal_request(m)
    elif m.text == "🚕 Такси":
      await msg_taxi_prompt(m)
    elif m.text == "🧾 Прочие расходы":
      await msg_other_expenses_prompt(m)
    elif m.text == "💰 Мой баланс":
      await msg_balance(m)
    elif m.text == "💳 Реквизиты":
      await msg_reqs(m)
    elif m.text == "⚙️ Управление":
      await cmd_panel(m)
    return
  await m.answer("🔄 <b>Действие отменено.</b>", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.callback_query(F.data == "cancel_fsm_action")
async def cb_cancel_fsm_action(c: types.CallbackQuery, state: FSMContext):
  await c.answer("Отменено")
  await state.clear()
  waiting_reqs.discard(c.from_user.id)
  await c.message.edit_text("❌ <b>Действие отменено.</b>", parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
  await state.clear()
  waiting_reqs.discard(m.from_user.id)
  name = await get_emp(m.from_user)
  cur_period = get_pay_period(now_astana())
  await m.answer(
      f"👋 <b>Привет, {name}!</b>\n━━━━━━━━━━━━━━\n"
      f"🕒 Время Астаны: <b>{now_astana().strftime('%H:%M')}</b>\n"
      f"📅 Расчетный период: <b>{cur_period}</b>\n\n"
      f"<blockquote>Вы можете просто отправить фото чека или скриншот поездки в этот чат.</blockquote>",
      reply_markup=main_kb(m.from_user.id), parse_mode="HTML",
  )


@dp.message(Command("login"))
async def cmd_login(m: types.Message):
  parts = m.text.split(maxsplit=1)
  if len(parts) > 1 and parts[1].strip() == ADMIN_PASSWORD:
    admins.add(m.from_user.id)
    await m.answer("🔓 <b>Права Главного Администратора активированы!</b>", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.message(Command("manager"))
async def cmd_manager(m: types.Message):
  parts = m.text.split(maxsplit=1)
  if len(parts) > 1 and parts[1].strip() == MANAGER_PASSWORD:
    managers.add(m.from_user.id)
    await m.answer("🔓 <b>Права Управляющего (контроль смен) активированы!</b>", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.message(F.text == "💰 Мой баланс")
async def msg_balance(m: types.Message):
  wait = await m.answer("⏳ Загружаю баланс...")
  name = await get_emp(m.from_user)
  debts, items = await asyncio.to_thread(get_debts_sync, name)
  team_members = await asyncio.to_thread(get_all_team_members_sync)
  hrs = next((h for k, _, h in team_members if k.lower() == name.lower()), 0.0)

  total = sum(x["amount"] for x in items)
  cur_period = get_pay_period(now_astana())

  taxi_sum = sum(x["amount"] for x in items if x["cat"] == "Такси" and x["amount"] > 0)
  meals_sum = sum(x["amount"] for x in items if x["cat"] == "Обеденные" and x["amount"] > 0)
  other_sum = sum(x["amount"] for x in items if x["cat"] == "Прочие расходы" and x["amount"] > 0)
  advance_sum = sum(abs(x["amount"]) for x in items if x["amount"] < 0)

  if total > 0.01:
    status = f"💵 <b>К выплате вам: {total:,.0f} ₸</b>"
  elif total < -0.01:
    status = f"🟢 <b>Остаток подотчетного баланса: +{abs(total):,.0f} ₸</b>"
  else:
    status = "✨ <b>Все расчеты закрыты (0 ₸)</b>"

  adv_line = f"\n💳 <i>Выдано в подотчет: {advance_sum:,.0f} ₸</i>" if advance_sum > 0 else ""

  await wait.edit_text(
      f"👤 <b>Сотрудник: {name}</b>\n"
      f"📅 Период: <b>{cur_period}</b>\n"
      f"⏱ Отработано за период: <b>{hrs:g} ч.</b>\n━━━━━━━━━━━━━━\n"
      f"{status}{adv_line}\n\n"
      f"📊 <b>Ваши расходы в текущем расчете:</b>\n"
      f"• 🚕 Такси: <b>{taxi_sum:,.0f} ₸</b>\n"
      f"• 🍔 Обеды: <b>{meals_sum:,.0f} ₸</b>\n"
      f"• 🧾 Прочее: <b>{other_sum:,.0f} ₸</b>",
      parse_mode="HTML",
  )


@dp.message(F.text == "💳 Реквизиты")
async def msg_reqs(m: types.Message):
  name = await get_emp(m.from_user)
  def read_req():
    for r in spreadsheet.worksheet("Выплаты команде").get_all_values()[1:]:
      if len(r) >= 3 and r[0].strip().lower() == name.lower():
        return r[2].strip().lstrip("'") if r[2].strip() not in ["-", ""] else "Не указаны"
    return "Не указаны"
  reqs = await asyncio.to_thread(read_req)
  kb = IMarkup(inline_keyboard=[[IBtn(text="✏️ Изменить реквизиты", callback_data="edit_reqs")]])
  await m.answer(
      f"💳 <b>Реквизиты для выплат</b>\n━━━━━━━━━━━━━━\n👤 <b>{name}</b>\n<blockquote><code>{reqs}</code></blockquote>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_reqs(c: types.CallbackQuery):
  await c.answer()
  waiting_reqs.add(c.from_user.id)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text("📝 <b>Отправьте номер Kaspi / ИИН одним сообщением:</b>", reply_markup=kb, parse_mode="HTML")


# =====================================================================
# 6. СМЕНЫ: СТАРТ СЕЙЧАС, ЗАДНИМ ЧИСЛОМ И ПОЛНАЯ СМЕНА
# =====================================================================


async def finish_shift(uid: int, end_dt: datetime, auto: bool = False) -> str | None:
  shift = active_shifts.pop(uid, None)
  if not shift:
    return None
  dur = max(timedelta(0), end_dt - shift["start"])
  hours_float = dur.total_seconds() / 3600.0
  h_int, m_int = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)
  
  await asyncio.to_thread(log_shift_end_sync, shift["id"], end_dt, hours_float, auto)

  title = "⏰ <b>Смена закрыта автоматически (нет ответа)</b>" if auto else "🔴 <b>Смена завершена и записана в табель!</b>"
  return (
      f"{title}\n━━━━━━━━━━━━━━\n"
      f"📅 Дата старта: <b>{shift['date']}</b>\n"
      f"📝 Площадка: <i>{shift['comment']}</i>\n"
      f"🕒 Время: {shift['start'].strftime('%H:%M')} ➔ {end_dt.strftime('%H:%M')}\n"
      f"⏱ Отработано: <b>{h_int} ч. {m_int} мин. ({hours_float:.1f} ч.)</b>"
  )


async def shift_timer_loop():
  while True:
    try:
      now = now_astana()
      for uid, s in list(active_shifts.items()):
        if s.get("waiting"):
          if now >= s["prompt_at"] + timedelta(minutes=SHIFT_TIMEOUT_M):
            txt = await finish_shift(uid, end_dt=s["prompt_at"], auto=True)
            if txt:
              try:
                await bot.send_message(uid, txt, reply_markup=main_kb(uid), parse_mode="HTML")
              except Exception:
                pass
        elif now >= s["next_check"]:
          s["waiting"] = True
          s["prompt_at"] = now
          hrs = int((now - s["start"]).total_seconds() // 3600)
          kb = IMarkup(inline_keyboard=[
              [IBtn(text="🟢 Продолжаю работать (+6 ч)", callback_data="shift_extend")],
              [IBtn(text="🔴 Завершить смену", callback_data="shift_end_inline")],
          ])
          try:
            await bot.send_message(
                uid,
                f"⏰ <b>Прошло {hrs} ч. смены!</b>\n━━━━━━━━━━━━━━\n"
                f"📅 Дата: <b>{s['date']}</b>\n📝 Площадка: {s['comment']}\nВы ещё работаете?",
                reply_markup=kb, parse_mode="HTML",
            )
          except Exception:
            pass
    except Exception as e:
      print(f"Shift timer error: {e}")
    await asyncio.sleep(60)


@dp.message(F.text == "🔴 Завершить смену")
async def msg_end_shift_direct(m: types.Message):
  uid = m.from_user.id
  if uid not in active_shifts:
    return await m.answer("У вас нет активной смены.", reply_markup=main_kb(uid))
  txt = await finish_shift(uid, end_dt=now_astana(), auto=False)
  await m.answer(txt or "Смена уже завершена.", reply_markup=main_kb(uid), parse_mode="HTML")


@dp.callback_query(F.data == "shift_end_inline")
async def cb_shift_end_inline(c: types.CallbackQuery):
  await c.answer()
  uid = c.from_user.id
  txt = await finish_shift(uid, end_dt=now_astana(), auto=False)
  await c.message.edit_text(txt or "Смена уже завершена.", parse_mode="HTML")
  await bot.send_message(uid, "✅ <b>Смена закрыта. Главное меню обновлено.</b>", reply_markup=main_kb(uid), parse_mode="HTML")


@dp.message(F.text == "⏱ Моя смена")
async def msg_shift(m: types.Message):
  uid = m.from_user.id
  if uid not in active_shifts:
    kb = IMarkup(inline_keyboard=[
        [IBtn(text="🟢 Начать смену прямо сейчас", callback_data="smode|now")],
        [IBtn(text="🕒 Начать смену (задним числом)", callback_data="smode|retro")],
        [IBtn(text="✅ Внести полностью отработанную смену", callback_data="smode|retro_full")],
    ])
    await m.answer("⏱ <b>Учет рабочего времени</b>\nУ вас нет активной смены. Выберите действие:", reply_markup=kb, parse_mode="HTML")
  else:
    s = active_shifts[uid]
    dur = now_astana() - s["start"]
    h, mn = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)
    btns = [[IBtn(text="🔴 Завершить смену", callback_data="shift_end_inline")]]
    if s.get("waiting"):
      btns.insert(0, [IBtn(text="🟢 Продлить смену (+6 ч)", callback_data="shift_extend")])
    await m.answer(
        f"🟢 <b>Смена активна ({s['id']})</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Дата: <b>{s['date']}</b>\n"
        f"📝 Площадка: <i>{s['comment']}</i>\n"
        f"🕒 Старт: <b>{s['start'].strftime('%H:%M')}</b> (в работе: {h} ч. {mn} мин.)\n"
        f"⏰ Следующая проверка: {s['next_check'].strftime('%H:%M')}",
        reply_markup=IMarkup(inline_keyboard=btns), parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("smode|"))
async def cb_smode(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  mode = c.data.split("|")[1]
  
  if mode == "now":
    today_str = now_astana().strftime("%d.%m.%Y")
    await state.update_data(date=today_str)
    await state.set_state(Form.shift_comment)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"🟢 <b>Старт смены: Сегодня ({today_str})</b>\n\n"
        f"📝 <b>Напишите короткий комментарий (площадка / задача):</b>\n"
        f"<blockquote>Например: <code>Склад погрузка</code> или <code>Концертный зал</code></blockquote>",
        reply_markup=kb, parse_mode="HTML",
    )
  elif mode == "retro_full":
    await c.message.edit_text("📅 <b>Выберите дату завершенной смены:</b>", reply_markup=date_picker_kb("s_retro_full"), parse_mode="HTML")
  else:
    await c.message.edit_text("📅 <b>Выберите дату, когда началась смена:</b>", reply_markup=date_picker_kb("s_retro"), parse_mode="HTML")


@dp.callback_query(F.data == "shift_extend")
async def cb_shift_extend(c: types.CallbackQuery):
  s = active_shifts.get(c.from_user.id)
  if not s:
    return await c.answer("Смена уже закрыта", show_alert=True)
  await c.answer("Продлено на 6 часов!")
  s["waiting"] = False
  s["next_check"] = now_astana() + timedelta(hours=SHIFT_INTERVAL_H)
  await c.message.edit_text(f"✅ <b>Смена продлена!</b>\nСледующая проверка в {s['next_check'].strftime('%H:%M')}.", parse_mode="HTML")


@dp.message(Form.shift_comment, F.text)
async def process_shift_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  now = now_astana()
  nxt = now + timedelta(hours=SHIFT_INTERVAL_H)
  emp = await get_emp(m.from_user)
  shift_id = f"SH-{random.randint(10000, 99999)}"
  comment = m.text.strip()

  active_shifts[m.from_user.id] = {
      "id": shift_id, "date": data["date"], "start": now, "emp": emp,
      "comment": comment, "next_check": nxt, "waiting": False, "prompt_at": None,
  }
  await asyncio.to_thread(log_shift_start_sync, shift_id, data["date"], emp, now, comment)

  await m.answer(
      f"🟢 <b>Смена начата и внесена в таблицу!</b>\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{data['date']}</b>\n📝 Площадка: <i>{comment}</i>\n"
      f"🕒 Старт: <b>{now.strftime('%H:%M')}</b> (проверка в {nxt.strftime('%H:%M')})",
      reply_markup=main_kb(m.from_user.id), parse_mode="HTML",
  )


# --- СТАРТ СМЕНЫ ЗАДНИМ ЧИСЛОМ ---
@dp.message(Form.retro_time, F.text)
async def process_retro_time(m: types.Message, state: FSMContext):
  data = await state.get_data()
  date_str = data.get("date", now_astana().strftime("%d.%m.%Y"))
  text = m.text.strip().lower()

  try:
    base_date = datetime.strptime(date_str, "%d.%m.%Y").date()
  except ValueError:
    base_date = now_astana().date()

  now = now_astana()
  target = None

  match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
  if match:
    h, mn = int(match.group(1)), int(match.group(2))
    if 0 <= h <= 23 and 0 <= mn <= 59:
      target = datetime(base_date.year, base_date.month, base_date.day, h, mn, 0, tzinfo=TZ_ASTANA)

  if not target:
    match_4d = re.search(r"\b(\d{2})(\d{2})\b", text)
    if match_4d:
      h, mn = int(match_4d.group(1)), int(match_4d.group(2))
      if 0 <= h <= 23 and 0 <= mn <= 59:
        target = datetime(base_date.year, base_date.month, base_date.day, h, mn, 0, tzinfo=TZ_ASTANA)

  if not target:
    match_hour_only = re.search(r"\b(\d{1,2})\b", text)
    if match_hour_only and "назад" not in text:
      h = int(match_hour_only.group(1))
      if 0 <= h <= 23:
        target = datetime(base_date.year, base_date.month, base_date.day, h, 0, 0, tzinfo=TZ_ASTANA)

  if not target and "назад" in text:
    nums = re.findall(r"\d+", text)
    if nums:
      target = now - timedelta(hours=int(nums[0]))
      date_str = target.strftime("%d.%m.%Y")

  if not target:
    return await m.answer(
        "⚠️ <b>Не удалось распознать время.</b>\n"
        "Укажите время начала смены в формате <code>14:00</code>:", parse_mode="HTML"
    )

  if target > now:
    return await m.answer(
        f"⚠️ Время начала (<b>{target.strftime('%d.%m.%Y %H:%M')}</b>) не может быть в будущем!\n"
        "Введите фактическое время начала:", parse_mode="HTML"
    )

  await state.update_data(date=date_str, retro_iso=target.isoformat())
  await state.set_state(Form.retro_comment)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await m.answer(
      f"🕒 Время старта: <b>{target.strftime('%d.%m.%Y в %H:%M')}</b>\n\n"
      f"📝 <b>Напишите короткий комментарий к смене (площадка / задача):</b>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.message(Form.retro_comment, F.text)
async def process_retro_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  target = datetime.fromisoformat(data["retro_iso"])
  date_str = data["date"]
  comment = m.text.strip()
  rid = f"SR-{random.randint(1000, 9999)}"
  emp = await get_emp(m.from_user)

  pending_shifts[rid] = {
      "uid": m.from_user.id, "type": "start",
      "date": date_str, "start": target,
      "emp": emp, "comment": comment,
  }

  kb = IMarkup(inline_keyboard=[[
      IBtn(text="✅ Одобрить", callback_data=f"s_appr|{rid}"),
      IBtn(text="❌ Отклонить", callback_data=f"s_rej|{rid}"),
  ]])
  for lid in admins | managers:
    try:
      await bot.send_message(
          lid,
          f"⚠️ <b>Запрос старта смены (задним числом)</b>\n━━━━━━━━━━━━━━\n"
          f"👤 Сотрудник: <b>{emp}</b>\n📅 Дата: <b>{date_str}</b>\n"
          f"🕒 Фактический старт: <b>{target.strftime('%H:%M')}</b>\n"
          f"📝 Площадка: <i>{comment}</i>",
          reply_markup=kb, parse_mode="HTML",
      )
    except Exception:
      pass

  await m.answer(
      f"⏳ Запрос на начало смены с <b>{target.strftime('%d.%m.%Y в %H:%M')}</b> ({comment}) отправлен на согласование.",
      reply_markup=main_kb(m.from_user.id), parse_mode="HTML",
  )


# --- ПОЛНАЯ ЗАВЕРШЕННАЯ СМЕНА ЗАДНИМ ЧИСЛОМ ---
@dp.message(Form.retro_full_time, F.text)
async def process_retro_full_time(m: types.Message, state: FSMContext):
  data = await state.get_data()
  date_str = data.get("date", now_astana().strftime("%d.%m.%Y"))
  text = m.text.strip().lower()

  try:
    base_date = datetime.strptime(date_str, "%d.%m.%Y").date()
  except ValueError:
    base_date = now_astana().date()

  times = re.findall(r"\b(\d{1,2})[:.]?(\d{2})?\b", text.replace("до", "-"))
  if len(times) < 2:
    return await m.answer(
        "⚠️ <b>Укажите ДВА времени (начало и конец).</b>\n"
        "Пример: <code>14:00 - 22:00</code> или <code>с 10 до 18</code>:", parse_mode="HTML"
    )

  try:
    h1, m1 = int(times[0][0]), int(times[0][1] if times[0][1] else 0)
    h2, m2 = int(times[1][0]), int(times[1][1] if times[1][1] else 0)
    start_dt = datetime(base_date.year, base_date.month, base_date.day, h1, m1, 0, tzinfo=TZ_ASTANA)
    end_dt = datetime(base_date.year, base_date.month, base_date.day, h2, m2, 0, tzinfo=TZ_ASTANA)
    if end_dt <= start_dt:
      end_dt += timedelta(days=1)
  except ValueError:
    return await m.answer("⚠️ Ошибка формата времени. Попробуйте: <code>14:00 - 22:00</code>", parse_mode="HTML")

  await state.update_data(date=date_str, start_iso=start_dt.isoformat(), end_iso=end_dt.isoformat())
  await state.set_state(Form.retro_full_comment)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  
  dur = max(timedelta(0), end_dt - start_dt)
  await m.answer(
      f"🕒 Время: <b>{start_dt.strftime('%H:%M')} — {end_dt.strftime('%H:%M')}</b> ({dur.total_seconds()/3600:.1f} ч.)\n\n"
      f"📝 <b>Напишите комментарий к этой смене (площадка / задача):</b>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.message(Form.retro_full_comment, F.text)
async def process_retro_full_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()

  start_dt = datetime.fromisoformat(data["start_iso"])
  end_dt = datetime.fromisoformat(data["end_iso"])
  date_str = data["date"]
  comment = m.text.strip()
  rid = f"SRF-{random.randint(1000, 9999)}"
  emp = await get_emp(m.from_user)

  pending_shifts[rid] = {
      "uid": m.from_user.id, "type": "full",
      "date": date_str, "start": start_dt, "end": end_dt,
      "emp": emp, "comment": comment,
  }

  dur = max(timedelta(0), end_dt - start_dt)
  kb = IMarkup(inline_keyboard=[[
      IBtn(text="✅ Одобрить смену", callback_data=f"s_appr_full|{rid}"),
      IBtn(text="❌ Отклонить", callback_data=f"s_rej|{rid}"),
  ]])
  for lid in admins | managers:
    try:
      await bot.send_message(
          lid,
          f"⚠️ <b>Запрос ПОЛНОЙ смены (задним числом)</b>\n━━━━━━━━━━━━━━\n"
          f"👤 Сотрудник: <b>{emp}</b>\n📅 Дата: <b>{date_str}</b>\n"
          f"🕒 Время: <b>{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}</b> ({dur.total_seconds()/3600:.1f} ч.)\n"
          f"📝 Площадка: <i>{comment}</i>",
          reply_markup=kb, parse_mode="HTML",
      )
    except Exception:
      pass

  await m.answer(
      f"⏳ Запрос на полную смену отправлен на согласование.",
      reply_markup=main_kb(m.from_user.id), parse_mode="HTML",
  )


# --- ОБРАБОТЧИКИ ОДОБРЕНИЯ СМЕН ---
@dp.callback_query(F.data.startswith(("s_appr|", "s_appr_full|", "s_rej|")))
async def cb_retro_decision(c: types.CallbackQuery):
  if not is_staff_lead(c.from_user.id):
    return await c.answer("Только для Управляющих!", show_alert=True)
  await c.answer()
  
  action_parts = c.data.split("|")
  act, rid = action_parts[0], action_parts[1]
  req = pending_shifts.pop(rid, None)
  if not req:
    return await c.message.edit_text("Запрос уже обработан.")

  if act == "s_appr":
    start_time = req["start"]
    now = now_astana()
    shift_id = f"SH-{random.randint(10000, 99999)}"
    
    elapsed_h = max(0, (now - start_time).total_seconds()) / 3600.0
    blocks = (int(elapsed_h // SHIFT_INTERVAL_H) + 1) * SHIFT_INTERVAL_H
    next_check = start_time + timedelta(hours=blocks)

    active_shifts[req["uid"]] = {
        "id": shift_id, "date": req["date"], "start": start_time, "emp": req["emp"],
        "comment": req["comment"], "next_check": next_check,
        "waiting": False, "prompt_at": None,
    }
    await asyncio.to_thread(log_shift_start_sync, shift_id, req["date"], req["emp"], start_time, req["comment"])

    await c.message.edit_text(f"✅ Старт смены <b>{req['emp']}</b> одобрен!", parse_mode="HTML")
    try:
      await bot.send_message(
          req["uid"],
          f"✅ <b>Старт смены задним числом одобрен!</b>\n📅 {req['date']} в {start_time.strftime('%H:%M')}",
          reply_markup=main_kb(req["uid"]), parse_mode="HTML",
      )
    except Exception:
      pass

  elif act == "s_appr_full":
    shift_id = f"SH-{random.randint(10000, 99999)}"
    await asyncio.to_thread(log_full_completed_shift_sync, shift_id, req["date"], req["emp"], req["start"], req["end"], req["comment"])
    
    dur = max(timedelta(0), req["end"] - req["start"])
    hrs = dur.total_seconds() / 3600.0

    await c.message.edit_text(f"✅ Завершенная смена <b>{req['emp']}</b> ({hrs:.1f} ч.) зачислена в табель!", parse_mode="HTML")
    try:
      await bot.send_message(
          req["uid"],
          f"✅ <b>Ваша завершенная смена одобрена и добавлена в баланс часов!</b>\n📅 {req['date']} ({hrs:.1f} ч.)",
          reply_markup=main_kb(req["uid"]), parse_mode="HTML",
      )
    except Exception:
      pass

  else:
    await c.message.edit_text(f"❌ Запрос <b>{req['emp']}</b> отклонен.", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], "❌ Запрос по смене отклонен управляющим.", parse_mode="HTML")
    except Exception:
      pass


# =====================================================================
# 7. ЗАЯВИТЕЛЬНЫЙ ОБЕД
# =====================================================================


@dp.message(F.text == "🍔 Запросить обед")
async def msg_meal_request(m: types.Message):
  await m.answer(
      "🍔 <b>Запрос на обеденные (2 500 ₸)</b>\nВыберите дату смены, за которую запрашиваете обед:",
      reply_markup=date_picker_kb("req_meal"), parse_mode="HTML",
  )


@dp.message(Form.meal_comment, F.text)
async def process_meal_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  emp = await get_emp(m.from_user)
  mid = f"ML-{random.randint(1000, 9999)}"
  pending_meal_requests[mid] = {
      "uid": m.from_user.id, "date": data["date"], "emp": emp, "comment": m.text.strip(),
  }

  kb = IMarkup(inline_keyboard=[[
      IBtn(text="✅ Одобрить 2 500 ₸", callback_data=f"ml_ok|{mid}"),
      IBtn(text="❌ Отклонить", callback_data=f"ml_no|{mid}"),
  ]])
  for aid in admins:
    try:
      await bot.send_message(
          aid,
          f"🍔 <b>Запрос на обеденные (2 500 ₸)</b>\n━━━━━━━━━━━━━━\n"
          f"👤 Сотрудник: <b>{emp}</b>\n📅 Дата: <b>{data['date']}</b>\n"
          f"📝 Примечание: <i>{m.text.strip()}</i>\n\nОдобрить начисление?",
          reply_markup=kb, parse_mode="HTML",
      )
    except Exception:
      pass
  await m.answer(f"⏳ <b>Запрос отправлен Администратору!</b>\n📅 Дата: {data['date']}", parse_mode="HTML")


@dp.callback_query(F.data.startswith(("ml_ok|", "ml_no|")))
async def cb_meal_decision(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Финансовые запросы одобряет только Администратор!", show_alert=True)
  await c.answer()
  act, mid = c.data.split("|")
  req = pending_meal_requests.pop(mid, None)
  if not req:
    return await c.message.edit_text("Запрос на обед уже обработан.")

  if act == "ml_ok":
    approver = c.from_user.full_name or "Администратор"
    await asyncio.to_thread(
        add_operation_sync,
        req["date"], "Обеденные", 2500, req["emp"],
        f"{req['comment']} (Одобрил: {approver})", "К возмещению",
    )
    await c.message.edit_text(f"✅ Обед для <b>{req['emp']}</b> (2 500 ₸) одобрен и зачислен!", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], f"✅ <b>Ваш обед (2 500 ₸) за {req['date']} одобрен!</b>", parse_mode="HTML")
    except Exception:
      pass
  else:
    await c.message.edit_text(f"❌ Запрос обеда для <b>{req['emp']}</b> отклонен.", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], f"❌ Запрос на обеденные за {req['date']} отклонен.", parse_mode="HTML")
    except Exception:
      pass


# =====================================================================
# 8. ТАКСИ, ЧЕКИ СОТРУДНИКОВ И МАРШРУТИЗАЦИЯ
# =====================================================================


@dp.message(F.text == "🚕 Такси")
async def msg_taxi_prompt(m: types.Message):
  now = now_astana()
  night = now.hour >= 22 or now.hour < 7
  mode_txt = "🌙 Ночной тариф (22:00–07:00) — начисление сразу!" if night else "☀️ Дневной тариф — через согласование с Администратором."
  await m.answer(
      f"🚕 <b>Учет поездок на такси</b>\n━━━━━━━━━━━━━━\nОтправьте скриншот поездки из Яндекс Go.\n<blockquote>{mode_txt}</blockquote>",
      parse_mode="HTML",
  )


@dp.message(F.text == "🧾 Прочие расходы")
async def msg_other_expenses_prompt(m: types.Message):
  await m.answer(
      "🧾 <b>Прочие расходы</b>\n━━━━━━━━━━━━━━\nОтправьте фото чека в этот чат. Бот внесет его в таблицу.",
      parse_mode="HTML",
  )


@dp.message(StateFilter(None), F.photo)
async def handle_receipt_photo(m: types.Message):
  photo = m.photo[-1]
  if photo.file_unique_id in saved_files:
    return await m.answer("⚠️ Этот чек уже был загружен ранее!")

  wait = await m.answer("🔍 <b>Сканирую чек и сохраняю в папку на Диске...</b>", parse_mode="HTML")
  buf = io.BytesIO()
  await bot.download(photo, destination=buf)
  img_bytes = buf.getvalue()

  fname = f"check_{m.from_user.id}_{now_astana().strftime('%d%m_%H%M%S')}.jpg"
  drive_url = await asyncio.to_thread(upload_to_drive_sync, img_bytes, fname)

  prompt = (
      "Изучи чек и верни ТОЛЬКО JSON: "
      '{"amount": число, "merchant": "продавец", '
      '"category": "Такси" | "Прочие расходы", '
      '"description": "2-4 слова"}'
  )
  data = await ask_gemini([genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt])
  if not data:
    return await wait.edit_text("⚠️ Не удалось распознать изображение. Отправьте фото еще раз.")

  amt = float(data.get("amount", 0))
  merch = data.get("merchant", "Чек")
  cat = data.get("category", "Прочие расходы")
  desc = data.get("description", "")
  emp = await get_emp(m.from_user)

  pending_receipts[m.from_user.id] = {
      "amount": amt, "merchant": merch, "category": cat,
      "desc": desc, "emp": emp, "link": drive_url, "fid": photo.file_unique_id,
  }

  now = now_astana()
  is_taxi = cat == "Такси" or "яндекс" in merch.lower()
  badge = ""
  if is_taxi:
    pending_receipts[m.from_user.id]["category"] = "Такси"
    badge = "\n🌙 <i>Ночное такси (автоодобрение)</i>" if (now.hour >= 22 or now.hour < 7) else "\n☀️ <i>Дневное такси (на согласование)</i>"

  await wait.edit_text(
      f"🧾 <b>Распознан чек: {amt:,.0f} ₸</b>\n━━━━━━━━━━━━━━\n"
      f"📂 Категория: <b>{pending_receipts[m.from_user.id]['category']}</b>\n"
      f"🏪 Поставщик: <i>{merch} ({desc})</i>{badge}\n\n"
      f"📅 <b>Выберите дату расхода:</b>",
      reply_markup=date_picker_kb("check"), parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("pdate|"))
async def cb_universal_date_picked(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  _, act, label = c.data.split("|")
  uid = c.from_user.id

  if act == "req_meal":
    await state.update_data(date=label)
    await state.set_state(Form.meal_comment)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n📝 <b>Напишите комментарий к запросу:</b>",
        reply_markup=kb, parse_mode="HTML",
    )

  elif act == "s_retro":
    await state.update_data(date=label)
    await state.set_state(Form.retro_time)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n🕒 <b>Во сколько фактически началась смена?</b>\n"
        f"<blockquote>Пример: <code>14:00</code> или <code>11.30</code></blockquote>",
        reply_markup=kb, parse_mode="HTML",
    )

  elif act == "s_retro_full":
    await state.update_data(date=label)
    await state.set_state(Form.retro_full_time)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n🕒 <b>Введите время начала и окончания смены:</b>\n"
        f"<blockquote>Например: <code>14:00 - 22:00</code> или <code>с 10 до 18</code></blockquote>",
        reply_markup=kb, parse_mode="HTML",
    )

  elif act == "check":
    rec = pending_receipts.pop(uid, None)
    if not rec:
      return await c.message.edit_text("⚠️ Данные чека устарели.")

    now = now_astana()
    is_taxi = rec["category"] == "Такси"

    if is_taxi and not (now.hour >= 22 or now.hour < 7):
      tx_id = f"TX-{random.randint(10000, 99999)}"
      await asyncio.to_thread(
          add_operation_sync,
          label, "Такси", rec["amount"], rec["emp"],
          f"{rec['merchant']}: {rec['desc']} (Дневное такси)",
          "На согласовании", rec["link"], tx_id,
      )
      kb = IMarkup(inline_keyboard=[[
          IBtn(text="✅ Одобрить", callback_data=f"tx_ok|{tx_id}"),
          IBtn(text="❌ Отклонить", callback_data=f"tx_no|{tx_id}"),
      ]])
      for aid in admins:
        try:
          await bot.send_message(
              aid,
              f"🚕 <b>Дневное такси на согласовании</b>\n━━━━━━━━━━━━━━\n"
              f"👤 Сотрудник: <b>{rec['emp']}</b>\n💵 Сумма: <b>{rec['amount']:,.0f} ₸</b>\n📅 Дата: {label}",
              reply_markup=kb, parse_mode="HTML",
          )
        except Exception:
          pass
      return await c.message.edit_text(
          f"⏳ <b>Дневное такси отправлено Администратору на согласование!</b>\n📅 {label} — <b>{rec['amount']:,.0f} ₸</b>", parse_mode="HTML",
      )

    await asyncio.to_thread(
        add_operation_sync,
        label, rec["category"], rec["amount"], rec["emp"],
        f"{rec['merchant']}: {rec['desc']}", "К возмещению", rec["link"],
    )
    saved_files.add(rec["fid"])
    await c.message.edit_text(
        f"✅ <b>Расход внесен в таблицу!</b>\n━━━━━━━━━━━━━━\n"
        f"📂 Категория: <b>{rec['category']}</b>\n📅 Дата: <b>{label}</b>\n"
        f"💵 Сумма: <b>{rec['amount']:,.0f} ₸</b>\n👤 {rec['emp']}\n"
        f"📁 <i>Файл сохранен в папку чеков на Диске.</i>",
        parse_mode="HTML",
    )

  elif act == "fastexp":
    if not is_admin(uid):
      return await c.message.edit_text("⛔️ Доступ только для Администратора.")
    await state.update_data(date=label)
    await state.set_state(Form.fast_expense)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата расхода: <b>{label}</b>\n\n"
        f"📸 <b>Отправьте ФОТО чека</b> (ИИ сам считает сумму и категорию, комментарий можно дописать в подписи)\n"
        f"✍️ ИЛИ напишите расход <b>просто текстом</b> (например: <code>25000 микрофоны</code>)\n\n"
        f"<blockquote>По умолчанию расход спишется с вашего баланса «Касса управляющего».</blockquote>",
        reply_markup=kb, parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith(("tx_ok|", "tx_no|")))
async def cb_taxi_decision(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Финансовые операции подтверждает только Администратор!", show_alert=True)
  await c.answer()
  act, tx_id = c.data.split("|")
  new_st = "К возмещению" if act == "tx_ok" else "Отклонено"

  def upd():
    ws = spreadsheet.worksheet("Операции")
    for idx, r in enumerate(ws.get_all_values()[1:], start=2):
      if r and r[0].strip() == tx_id:
        ws.update_cell(idx, 9, new_st)
        return True
    return False

  await asyncio.to_thread(upd)
  icon = "✅ Одобрено" if act == "tx_ok" else "❌ Отклонено"
  await c.message.edit_text(f"{icon}: статус поездки изменен на <b>{new_st}</b>.", parse_mode="HTML")


# =====================================================================
# 9. ПАНЕЛЬ УПРАВЛЕНИЯ И ЛИЧНАЯ КАССА ГЛАВНОГО УПРАВЛЯЮЩЕГО
# =====================================================================


async def render_control_panel(target, uid: int):
  cur_period = get_pay_period(now_astana())

  if is_admin(uid):
    team_members, cash = await asyncio.gather(
        asyncio.to_thread(get_all_team_members_sync),
        asyncio.to_thread(get_admin_cash_summary_sync),
    )
    tot_debt = sum(bal for _, bal, _ in team_members if bal > 0)
    c_bal = cash["balance"]
    bal_icon = "🟢" if c_bal >= 0 else "🔴"

    header = (
        f"⚙️ <b>Панель Главного Управляющего</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Период: <b>{cur_period}</b>\n\n"
        f"💼 <b>Касса управляющего (Ваш баланс):</b>\n"
        f"• Текущий остаток: {bal_icon} <b>{c_bal:,.0f} ₸</b>\n"
        f"• Пополнено: <code>+{cash['income']:,.0f} ₸</code> | Потрачено: <code>-{cash['expense']:,.0f} ₸</code>\n\n"
        f"👥 <b>Команда:</b>\n"
        f"• Долг перед командой: <b>{tot_debt:,.0f} ₸</b>\n"
        f"• Активных смен сейчас: <b>{len(active_shifts)}</b>"
    )
    btns = [
        [
            IBtn(text="➕ Пополнить свой баланс", callback_data="adm_topup"),
            IBtn(text="📸 Расход кассы (Чек/Текст)", callback_data="adm_fast"),
        ],
        [IBtn(text=f"💸 Взаиморасчеты с командой ({len(team_members)})", callback_data="adm_paylist")],
        [IBtn(text=f"⏱ Смены онлайн ({len(active_shifts)})", callback_data="adm_shifts")],
        [IBtn(text="👥 Роли команды", callback_data="adm_roles"), IBtn(text="🔄 Формулы", callback_data="adm_fix")],
    ]
  else:
    header = (
        f"⚙️ <b>Панель Управляющего (Контроль смен)</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Текущий период: <b>{cur_period}</b>\n"
        f"🟢 Активных смен сейчас: <b>{len(active_shifts)}</b>"
    )
    btns = [
        [IBtn(text=f"⏱ Смены онлайн ({len(active_shifts)})", callback_data="adm_shifts")],
    ]

  kb = IMarkup(inline_keyboard=btns)
  if isinstance(target, types.Message):
    await target.answer(header, reply_markup=kb, parse_mode="HTML")
  else:
    await target.message.edit_text(header, reply_markup=kb, parse_mode="HTML")


@dp.message(F.text == "⚙️ Управление")
@dp.message(Command("admin"))
async def cmd_panel(m: types.Message):
  if not is_staff_lead(m.from_user.id):
    return await m.answer("⛔️ Нет прав доступа.")
  await render_control_panel(m, m.from_user.id)


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  await state.clear()
  await render_control_panel(c, c.from_user.id)


@dp.callback_query(F.data == "adm_fix")
async def cb_adm_fix(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer("Синхронизирую формулы...")
  await asyncio.to_thread(sync_roles_and_formulas)
  await c.message.answer("✅ Формулы, часы смен и расчетные периоды обновлены!")


@dp.callback_query(F.data == "adm_topup")
async def cb_adm_topup(c: types.CallbackQuery, state: FSMContext):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer()
  await state.set_state(Form.admin_topup)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text(
      "➕ <b>Пополнение баланса управляющего</b>\n━━━━━━━━━━━━━━\n"
      "Напишите сумму пополнения и комментарий (по желанию):\n"
      "<blockquote>Например: <code>200000 на текущие расходы</code> или просто <code>150000</code></blockquote>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.message(Form.admin_topup, F.text)
async def process_admin_topup(m: types.Message, state: FSMContext):
  if not is_admin(m.from_user.id):
    return await state.clear()

  nums = re.findall(r"\d+(?:[.,]\d+)?", m.text.replace(" ", ""))
  if not nums:
    return await m.answer("⚠️ Укажите сумму числом (например: <code>150000 пополнение кассы</code>):", parse_mode="HTML")

  amt = float(nums[0].replace(",", "."))
  comment = re.sub(r"\d+(?:[.,]\d+)?", "", m.text, count=1).strip() or "Пополнение рабочей кассы"
  await state.clear()

  today_str = now_astana().strftime("%d.%m.%Y")
  await asyncio.to_thread(
      add_admin_cash_entry_sync,
      today_str, "Пополнение баланса", "Пополнение", amt, 0.0, comment, None,
  )
  cash = await asyncio.to_thread(get_admin_cash_summary_sync)

  await m.answer(
      f"✅ <b>Баланс управляющего пополнен!</b>\n━━━━━━━━━━━━━━\n"
      f"➕ Сумма: <b>+{amt:,.0f} ₸</b>\n"
      f"📝 Примечание: <i>{comment}</i>\n"
      f"💼 Текущий остаток кассы: <b>{cash['balance']:,.0f} ₸</b>",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель управления", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "adm_fast")
async def cb_adm_fast(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer()
  await c.message.edit_text("📸 <b>Выберите дату расхода кассы:</b>", reply_markup=date_picker_kb("fastexp"), parse_mode="HTML")


@dp.message(Form.fast_expense, F.photo)
async def process_fast_expense_photo(m: types.Message, state: FSMContext):
  if not is_admin(m.from_user.id):
    return await state.clear()
  d = await state.get_data()
  await state.clear()
  wait = await m.answer("🔍 <b>Сканирую чек кассы и сохраняю в папку «Чеки»...</b>", parse_mode="HTML")

  photo = m.photo[-1]
  buf = io.BytesIO()
  await bot.download(photo, destination=buf)
  img_bytes = buf.getvalue()

  fname = f"admin_check_{m.from_user.id}_{now_astana().strftime('%d%m_%H%M%S')}.jpg"
  drive_url = await asyncio.to_thread(upload_to_drive_sync, img_bytes, fname)

  caption = m.caption or ""
  prompt_addon = f' Учти комментарий: "{caption}".' if caption else ""
  prompt = (
      "Изучи чек." + prompt_addon + " Верни ТОЛЬКО JSON: "
      '{"amount": число, "category": "Такси" | "Оборудование и расходники" | "Питание" | "Прочее", '
      '"desc": "продавец и суть (2-5 слов)"}'
  )

  res = await ask_gemini([genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt])
  if not res:
    res = {"amount": 0, "category": "Прочее", "desc": caption or "Чек управляющего"}

  amt = float(res.get("amount", 0))
  cat = res.get("category", "Прочее")
  if cat not in ("Такси", "Оборудование и расходники", "Питание", "Прочее"):
    cat = "Прочее"
  desc = res.get("desc", caption or "Чек управляющего")

  await asyncio.to_thread(
      add_admin_cash_entry_sync,
      d["date"], "Расход по чеку", cat, 0.0, amt, desc, drive_url,
  )
  cash = await asyncio.to_thread(get_admin_cash_summary_sync)

  await wait.edit_text(
      f"✅ <b>Расход кассы по чеку записан!</b>\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{d['date']}</b> | 💵 Списано: <b>-{amt:,.0f} ₸</b>\n"
      f"📂 Категория: <b>{cat}</b> (<i>{desc}</i>)\n"
      f"💼 Остаток баланса кассы: <b>{cash['balance']:,.0f} ₸</b>\n"
      f"📁 <i>Чек сохранен на Диске и прикреплен в лист «Касса управляющего».</i>",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


@dp.message(Form.fast_expense, F.text)
async def process_fast_expense(m: types.Message, state: FSMContext):
  if not is_admin(m.from_user.id):
    return await state.clear()
  d = await state.get_data()
  await state.clear()
  wait = await m.answer("🤖 Записываю расход в кассу управляющего...")

  prompt = (
      f'Разбери расход: "{m.text}". Верни JSON: '
      '{"amount": число, "category": "Такси" | "Оборудование и расходники" | "Питание" | "Прочее", '
      '"desc": "суть"}'
  )
  res = await ask_gemini(prompt) or {"amount": 0, "category": "Прочее", "desc": m.text}
  amt = float(res.get("amount", 0))
  cat = res.get("category", "Прочее")
  if cat not in ("Такси", "Оборудование и расходники", "Питание", "Прочее"):
    cat = "Прочее"
  desc = res.get("desc", m.text)

  await asyncio.to_thread(
      add_admin_cash_entry_sync,
      d["date"], "Расход без чека", cat, 0.0, amt, desc, None,
  )
  cash = await asyncio.to_thread(get_admin_cash_summary_sync)

  await wait.edit_text(
      f"✅ <b>Расход списан с кассы управляющего!</b>\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{d['date']}</b> | 💵 Списано: <b>-{amt:,.0f} ₸</b>\n"
      f"📂 Категория: <b>{cat}</b> (<i>{desc}</i>)\n"
      f"💼 Остаток баланса кассы: <b>{cash['balance']:,.0f} ₸</b>",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "adm_shifts")
async def cb_adm_shifts(c: types.CallbackQuery):
  if not is_staff_lead(c.from_user.id):
    return await c.answer("Нет доступа!", show_alert=True)
  await c.answer()
  if not active_shifts:
    txt = "⏱ <b>Сейчас никто не находится на смене.</b>"
  else:
    now = now_astana()
    lines = []
    for s in active_shifts.values():
      dur = now - s["start"]
      h, mn = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)
      w = " ⚠️ <i>(ждём ответ)</i>" if s.get("waiting") else ""
      lines.append(f"• <b>{s['emp']}</b> ({s['date']})\n  📝 {s['comment']}\n  🕒 с {s['start'].strftime('%H:%M')} ({h} ч. {mn} мин.){w}")
    txt = "⏱ <b>Смены команды прямо сейчас:</b>\n━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
  await c.message.edit_text(txt, reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ Назад", callback_data="adm_back")]]), parse_mode="HTML")


@dp.callback_query(F.data == "adm_roles")
async def cb_adm_roles(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Только для Главного Администратора!", show_alert=True)
  await c.answer()
  rows = await asyncio.to_thread(lambda: spreadsheet.worksheet("Выплаты команде").get_all_values()[1:])
  kb = []
  for r in rows:
    if r and r[0].strip():
      name, role = r[0].strip(), (r[1].strip() if len(r) > 1 else "Команда")
      icon = "⭐️" if "управл" in role.lower() else "👤"
      kb.append([IBtn(text=f"{icon} {name} ({role})", callback_data=f"trole|{name[:20]}")])
  kb.append([IBtn(text="⬅️ Назад", callback_data="adm_back")])
  await c.message.edit_text("👥 <b>Назначение Управляющих (контроль смен):</b>", reply_markup=IMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("trole|"))
async def cb_toggle_role(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Только для Главного Администратора!", show_alert=True)
  await c.answer()
  prefix = c.data.split("|")[1]
  def toggle():
    ws = spreadsheet.worksheet("Выплаты команде")
    for idx, r in enumerate(ws.get_all_values()[1:], start=2):
      if r and r[0].strip().startswith(prefix):
        cur = r[1].strip().lower() if len(r) > 1 else ""
        ws.update_cell(idx, 2, "Команда" if "управл" in cur else "Управляющий")
        break
    sync_roles_and_formulas()
  await asyncio.to_thread(toggle)
  await cb_adm_roles(c)


@dp.callback_query(F.data == "adm_paylist")
async def cb_adm_paylist(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступ к финансам только у Администратора!", show_alert=True)
  await c.answer()
  team = await asyncio.to_thread(get_all_team_members_sync)
  cur_period = get_pay_period(now_astana())

  kb = []
  lines = []

  for emp, bal, hrs in team:
    h_str = f" | ⏱ {hrs:g} ч." if hrs > 0 else ""
    if bal > 0:
      status_txt = f"🔴 <b>{emp}</b>: <b>{bal:,.0f} ₸</b> (долг){h_str}"
      btn_txt = f"💸 Выплатить: {emp} ({bal:,.0f} ₸)"
    elif bal < 0:
      status_txt = f"🟢 <b>{emp}</b>: <b>+{abs(bal):,.0f} ₸</b> (подотчет){h_str}"
      btn_txt = f"➕ Подотчет: {emp}"
    else:
      status_txt = f"⚪️ <b>{emp}</b>: <b>0 ₸</b>{h_str}"
      btn_txt = f"➕ Подотчет: {emp}"

    lines.append(status_txt)
    kb.append([IBtn(text=btn_txt, callback_data=f"pay|{emp[:20]}")])

  kb.append([IBtn(text="⬅️ Назад", callback_data="adm_back")])
  body = "\n".join(lines) if lines else "Сотрудники в таблице не найдены."
  await c.message.edit_text(
      f"💸 <b>Взаиморасчеты со всеми сотрудниками</b>\n"
      f"📅 Текущий период: <b>{cur_period}</b>\n━━━━━━━━━━━━━━\n"
      f"{body}\n\n<i>Нажмите на сотрудника, чтобы провести выплату или выдать подотчет:</i>",
      reply_markup=IMarkup(inline_keyboard=kb),
      parse_mode="HTML",
  )


@dp.callback_query(F.data.startswith("pay|"))
async def cb_pay_prep(c: types.CallbackQuery, state: FSMContext):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступ к финансам только у Администратора!", show_alert=True)
  await c.answer()
  prefix = c.data.split("|")[1]
  team = await asyncio.to_thread(get_all_team_members_sync)
  emp = next((k for k, _, _ in team if k.startswith(prefix)), prefix)
  bal = next((v for k, v, _ in team if k == emp), 0.0)
  hrs = next((h for k, _, h in team if k == emp), 0.0)

  def get_req():
    for r in spreadsheet.worksheet("Выплаты команде").get_all_values()[1:]:
      if len(r) >= 3 and r[0].strip() == emp:
        return r[2].strip().lstrip("'")
    return "Не указаны"

  reqs = await asyncio.to_thread(get_req)
  await state.update_data(emp=emp, debt=bal)

  btns = []
  if bal > 0:
    btns.append([IBtn(text=f"✅ Погасить долг ровно ({bal:,.0f} ₸)", callback_data="pay_exact")])
    btns.append([IBtn(text="✏️ Частичная выплата / с переплатой", callback_data="pay_custom")])

  btns.append([IBtn(text="🟢 ➕ Пополнить подотчетный баланс", callback_data="give_advance")])
  btns.append([IBtn(text="⬅️ Назад к списку", callback_data="adm_paylist")])

  bal_info = (
      f"💵 <b>Текущий долг к выплате:</b> <code>{bal:,.0f} ₸</code>"
      if bal > 0
      else (f"🟢 <b>Подотчетный баланс:</b> <code>+{abs(bal):,.0f} ₸</code>" if bal < 0 else "⚪️ <b>Баланс нулевой (0 ₸)</b>")
  )

  await c.message.edit_text(
      f"👤 <b>Карточка сотрудника: {emp}</b>\n━━━━━━━━━━━━━━\n"
      f"{bal_info}\n"
      f"⏱ <b>Отработано за период:</b> {hrs:g} ч.\n\n"
      f"📋 <b>Реквизиты Kaspi:</b>\n<blockquote><code>{reqs}</code></blockquote>\n"
      "<i>Выберите действие:</i>",
      reply_markup=IMarkup(inline_keyboard=btns),
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "give_advance")
async def cb_give_advance_start(c: types.CallbackQuery, state: FSMContext):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer()
  d = await state.get_data()
  await state.set_state(Form.advance_amount)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text(
      f"➕ <b>Выдача средств на расходы (подотчет): {d['emp']}</b>\n━━━━━━━━━━━━━━\n"
      f"Введите сумму числом (например: <code>50000</code>):\n\n"
      f"<i>Эти деньги будут списываться автоматически, когда сотрудник будет присылать чеки в бота.</i>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.message(Form.advance_amount, F.text)
async def process_advance_amount(m: types.Message, state: FSMContext):
  if not is_admin(m.from_user.id):
    return await state.clear()
  try:
    advance_val = float(m.text.replace(" ", "").replace(",", ".").strip())
  except ValueError:
    return await m.answer("⚠️ Введите корректную сумму числом (например: 50000):")

  if advance_val <= 0:
    return await m.answer("⚠️ Сумма выдачи должна быть больше 0:")

  d = await state.get_data()
  emp = d["emp"]
  await state.clear()

  now_str = now_astana().strftime("%d.%m.%Y")
  await asyncio.to_thread(
      add_operation_sync,
      now_str, "Прочие расходы", -advance_val, emp,
      f"Средства на расходы (выдал: {m.from_user.full_name or 'Администратор'})",
      "К возмещению",
  )

  await m.answer(
      f"✅ <b>Средства выданы!</b>\n━━━━━━━━━━━━━━\n"
      f"👤 Сотрудник: <b>{emp}</b>\n"
      f"🟢 Подотчетный баланс: <b>+{advance_val:,.0f} ₸</b>\n\n"
      f"<i>Загруженные чеки автоматически погасят эту сумму.</i>",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель управления", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


def settle_payout_sync(emp: str, paid: float, debt: float):
  ws = spreadsheet.worksheet("Операции")
  for idx, r in enumerate(ws.get_all_values()[1:], start=2):
    if len(r) >= 9 and r[3].strip().lower() == emp.strip().lower() and "к возмещению" in r[8].lower():
      ws.update_cell(idx, 9, "Выплачено")

  diff = paid - debt
  if abs(diff) > 0.01:
    amt = -diff if diff > 0 else abs(diff)
    note = (
        f"Подотчет / переплата (выплачено {paid:,.0f} при долге {debt:,.0f})"
        if diff > 0 else f"Остаток долга (выплачено {paid:,.0f} из {debt:,.0f})"
    )
    add_operation_sync(now_astana().strftime("%d.%m.%Y"), "Прочие расходы", amt, emp, note, "К возмещению")


@dp.callback_query(F.data == "pay_exact")
async def cb_pay_exact(c: types.CallbackQuery, state: FSMContext):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer("Списываю долг...")
  d = await state.get_data()
  await state.clear()
  await asyncio.to_thread(settle_payout_sync, d["emp"], d["debt"], d["debt"])
  await c.message.edit_text(f"✅ Долг перед <b>{d['emp']}</b> на сумму <b>{d['debt']:,.0f} ₸</b> закрыт ровно в ноль!", parse_mode="HTML")
  await render_control_panel(c, c.from_user.id)


@dp.callback_query(F.data == "pay_custom")
async def cb_pay_custom(c: types.CallbackQuery, state: FSMContext):
  if not is_admin(c.from_user.id):
    return await c.answer("Доступно только Администратору!", show_alert=True)
  await c.answer()
  d = await state.get_data()
  await state.set_state(Form.custom_payout)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text(
      f"👤 <b>{d['emp']}</b> (текущий долг: {d['debt']:,.0f} ₸)\n\n"
      f"💸 <b>Введите фактически переведенную сумму:</b>\n"
      f"<blockquote>Если сумма превысит долг, остаток автоматически станет подотчетным балансом.</blockquote>",
      reply_markup=kb, parse_mode="HTML",
  )


@dp.message(Form.custom_payout, F.text)
async def process_custom_pay(m: types.Message, state: FSMContext):
  if not is_admin(m.from_user.id):
    return await state.clear()
  try:
    paid = float(m.text.replace(" ", "").replace(",", ".").strip())
  except ValueError:
    return await m.answer("⚠️ Введите число:")

  d = await state.get_data()
  await state.clear()
  await asyncio.to_thread(settle_payout_sync, d["emp"], paid, d["debt"])
  diff = paid - d["debt"]
  extra = f"\n🟢 Подотчетный баланс: <b>+{diff:,.0f} ₸</b>" if diff > 0 else (f"\n⚠️ Остаток долга: <b>{abs(diff):,.0f} ₸</b>" if diff < 0 else "")
  await m.answer(
      f"✅ <b>Выплата проведена!</b>\n👤 {d['emp']} | Переведено: <b>{paid:,.0f} ₸</b>{extra}",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


@dp.message(StateFilter(None), F.text & ~F.text.startswith("/") & ~F.text.in_(NAV_BTNS))
async def catch_requisites_input(m: types.Message):
  if m.from_user.id not in waiting_reqs:
    return
  waiting_reqs.discard(m.from_user.id)
  name = await get_emp(m.from_user)

  def save_req():
    ws = spreadsheet.worksheet("Выплаты команде")
    for idx, val in enumerate(ws.col_values(1), start=1):
      if val.strip().lower() == name.lower():
        txt = m.text.strip()
        ws.update_cell(idx, 3, f"'{txt}" if txt.startswith(("+", "=")) else txt)
        break

  await asyncio.to_thread(save_req)
  await m.answer(f"✅ <b>Реквизиты сохранены!</b>\n<blockquote><code>{m.text.strip()}</code></blockquote>", parse_mode="HTML")


# =====================================================================
# 10. ТОЧКА ВХОДА И ЗАПУСК
# =====================================================================


async def main():
  app = web.Application()
  app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT") or 8080)
  await web.TCPSite(runner, "0.0.0.0", port).start()

  try:
    await bot.delete_webhook(drop_pending_updates=True)
  except Exception as e:
    print(f"Webhook drop warning: {e}")

  await asyncio.to_thread(sync_roles_and_formulas)
  asyncio.create_task(shift_timer_loop())
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())