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
# 1. КОНФИГУРАЦИЯ (ID ТАБЛИЦЫ И ПАПКИ ВСТРОЕНЫ ПО УМОЛЧАНИЮ)
# =====================================================================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Встроенные ID Google Таблицы и папки для чеков
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA").strip()
RECEIPTS_FOLDER_ID = os.getenv("RECEIPTS_FOLDER_ID", "13ze6D8nDnjGtZ7QDOQAr5iZnyrVnLcEH").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "disa2026").strip()
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "disa_manager").strip()
INITIAL_ADMINS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
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
    "⏱ Моя смена", "🍔 Запросить обед", "🚕 Такси",
    "🧾 Прочие расходы", "💰 Мой баланс", "💳 Реквизиты", "⚙️ Управление",
}


class Form(StatesGroup):
  shift_comment = State()
  retro_time = State()
  meal_comment = State()
  fast_expense = State()
  custom_payout = State()


def now_astana() -> datetime:
  return datetime.now(TZ_ASTANA)


def get_pay_period(dt: datetime) -> str:
  m_name = MONTHS_RU[dt.month]
  half = "01-15" if dt.day <= 15 else f"16-{calendar.monthrange(dt.year, dt.month)[1]}"
  return f"{m_name} {dt.year} ({half})"


def is_admin(uid: int) -> bool:
  return uid in admins


def is_staff_lead(uid: int) -> bool:
  return uid in admins or uid in managers


# =====================================================================
# 2. ЗАГРУЗКА В ЦЕЛЕВУЮ ПАПКУ «ЧЕКИ» И ИИ
# =====================================================================


async def ask_gemini(contents) -> dict | None:
  models = ["gemini-3.6-flash", "gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
  cfg = genai_types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
  for idx, m in enumerate(models):
    try:
      resp = ai_client.models.generate_content(model=m, contents=contents, config=cfg)
      if resp and resp.text:
        raw = resp.text.strip()
        return json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except Exception:
      await asyncio.sleep(1.0 if idx == 0 else 0.3)
  return None


def upload_to_drive_sync(file_bytes: bytes, filename: str) -> str | None:
  try:
    creds = gc.auth
    if not creds.token:
      from google.auth.transport.requests import Request
      creds.refresh(Request())

    boundary = "===boundary==="

    # Файл помещается строго в папку «Чеки»
    metadata = {
        "name": filename,
        "mimeType": "image/jpeg",
        "parents": [RECEIPTS_FOLDER_ID],
    }
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
# 3. ТАБЛИЦА: 3 СТОЛБЦА РАСХОДОВ И РАСЧЕТНЫЕ ПЕРИОДЫ
# =====================================================================


def sync_roles_and_formulas():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    pay_rows = ws_pay.get_all_values()
    ws_ref = spreadsheet.worksheet("Справочники")
    ref_rows = ws_ref.get_all_records()

    mgr_names = set()
    cur_period = get_pay_period(now_astana())

    for idx, row in enumerate(pay_rows[1:], start=2):
      if not row or not row[0].strip():
        continue
      name = row[0].strip()
      role = row[1].strip().lower() if len(row) > 1 else ""
      if any(w in role for w in ["управл", "менедж", "admin"]):
        mgr_names.add(name.lower())

      # Лист «Выплаты команде»: D=Баланс, E=Такси, F=Обеды, G=Прочие, H=Всего, I=Архив, J=Период
      ws_pay.update_cell(idx, 4, f'=SUMIFS(Операции!H:H; Операции!D:D; A{idx}; Операции!I:I; "К возмещению")')
      ws_pay.update_cell(idx, 5, f'=SUMIFS(Операции!E:E; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")')
      ws_pay.update_cell(idx, 6, f'=SUMIFS(Операции!F:F; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")')
      ws_pay.update_cell(idx, 7, f'=SUMIFS(Операции!G:G; Операции!D:D; A{idx}; Операции!C:C; J{idx}; Операции!I:I; "К возмещению")')
      ws_pay.update_cell(idx, 8, f"=E{idx}+F{idx}+G{idx}")
      ws_pay.update_cell(idx, 9, f'=SUMIFS(Операции!H:H; Операции!D:D; A{idx}; Операции!I:I; "Выплачено")')
      ws_pay.update_cell(idx, 10, cur_period)

    for r in ref_rows:
      if str(r.get("Имя", "")).strip().lower() in mgr_names:
        cid = str(r.get("Chat ID", "")).strip()
        if cid.isdigit():
          managers.add(int(cid))
  except Exception as e:
    print(f"Sync error: {e}")


def get_employee_sync(user: types.User) -> str:
  uid = str(user.id)
  if uid in emp_cache:
    return emp_cache[uid]

  uname = f"@{user.username.lower()}" if user.username else ""
  name = (user.full_name or "Сотрудник").strip()

  try:
    ws_ref = spreadsheet.worksheet("Справочники")
    for r in ws_ref.get_all_records():
      if str(r.get("Chat ID", "")).strip() == uid or (
          uname and str(r.get("Telegram никнейм", "")).strip().lower() == uname
      ):
        name = str(r.get("Имя", "")).strip() or name
        break
    else:
      ws_ref.append_row([name, f"@{user.username}" if user.username else "-", uid])

    ws_pay = spreadsheet.worksheet("Выплаты команде")
    names = ws_pay.col_values(1)
    if name not in names:
      r_idx = len(names) + 1
      cur_period = get_pay_period(now_astana())
      ws_pay.append_row(
          [
              name, "Команда", "-",
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!E:E; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!F:F; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f'=SUMIFS(Операции!G:G; Операции!D:D; A{r_idx}; Операции!C:C; J{r_idx}; Операции!I:I; "К возмещению")',
              f"=E{r_idx}+F{r_idx}+G{r_idx}",
              f'=SUMIFS(Операции!H:H; Операции!D:D; A{r_idx}; Операции!I:I; "Выплачено")',
              cur_period,
          ],
          value_input_option="USER_ENTERED",
      )
  except Exception as e:
    print(f"Emp sync error: {e}")

  emp_cache[uid] = name
  return name


async def get_emp(user: types.User) -> str:
  return emp_cache.get(str(user.id)) or await asyncio.to_thread(get_employee_sync, user)


def add_operation_sync(
    calc_date: str, category: str, amount: float, employee: str,
    comment: str, status: str = "К возмещению", drive_link: str = None, tx_id: str = None,
):
  ws = spreadsheet.worksheet("Операции")
  tx = tx_id or f"TX-{random.randint(10000, 99999)}"
  period = get_pay_period(now_astana())
  next_row = len(ws.col_values(1)) + 1

  taxi_val = amount if category == "Такси" else 0
  meals_val = amount if category == "Обеденные" else 0
  other_val = amount if category == "Прочие расходы" else 0

  check_cell = f'=HYPERLINK("{drive_link}"; "📎 Чек")' if drive_link else (comment if comment else "Без чека")
  if drive_link and comment:
    check_cell = f'=HYPERLINK("{drive_link}"; "📎 Чек") & " | {comment}"'

  ws.append_row(
      [
          tx,
          calc_date,
          period,
          employee,
          taxi_val,
          meals_val,
          other_val,
          f"=E{next_row}+F{next_row}+G{next_row}",
          status,
          check_cell,
      ],
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

    taxi_amt = float(str(r[4]).replace(" ", "").replace(",", ".") or 0)
    meal_amt = float(str(r[5]).replace(" ", "").replace(",", ".") or 0)
    other_amt = float(str(r[6]).replace(" ", "").replace(",", ".") or 0)
    total_row = float(str(r[7]).replace(" ", "").replace(",", ".") or 0) if len(r) > 7 and r[7] else (taxi_amt + meal_amt + other_amt)

    debts[who] = debts.get(who, 0.0) + total_row

    if filter_emp and (filter_emp.lower() in who.lower() or who.lower() in filter_emp.lower()):
      cat = "Такси" if taxi_amt > 0 else ("Обеденные" if meal_amt > 0 else "Прочие расходы")
      items.append({"date": r[1], "cat": cat, "amount": total_row, "comment": r[9] if len(r) > 9 else "-"})
  return debts, items


# =====================================================================
# 4. КЛАВИАТУРЫ И КАЛЕНДАРЬ
# =====================================================================


def main_kb(uid: int) -> RMarkup:
  kb = [
      [KBtn(text="⏱ Моя смена"), KBtn(text="🍔 Запросить обед")],
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
  await m.answer("🔄 <b>Действие отменено.</b> Меню готово к работе!", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.callback_query(F.data == "cancel_fsm_action")
async def cb_cancel_fsm_action(c: types.CallbackQuery, state: FSMContext):
  await c.answer("Отменено")
  await state.clear()
  waiting_reqs.discard(c.from_user.id)
  await c.message.edit_text("❌ <b>Действие отменено.</b> Меню готово к работе.", parse_mode="HTML")


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
      reply_markup=main_kb(m.from_user.id),
      parse_mode="HTML",
  )


@dp.message(Command("login"))
async def cmd_login(m: types.Message):
  parts = m.text.split(maxsplit=1)
  if len(parts) > 1 and parts[1].strip() == ADMIN_PASSWORD:
    admins.add(m.from_user.id)
    await m.answer("🔓 <b>Права Главного Администратора активированы!</b>", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")
  else:
    await m.answer("⛔️ Неверный пароль. Формат: <code>/login пароль</code>", parse_mode="HTML")


@dp.message(Command("manager"))
async def cmd_manager(m: types.Message):
  parts = m.text.split(maxsplit=1)
  if len(parts) > 1 and parts[1].strip() == MANAGER_PASSWORD:
    managers.add(m.from_user.id)
    await m.answer("🔓 <b>Права Управляющего активированы!</b>", reply_markup=main_kb(m.from_user.id), parse_mode="HTML")
  else:
    await m.answer("⛔️ Неверный пароль.")


@dp.message(F.text == "💰 Мой баланс")
async def msg_balance(m: types.Message):
  wait = await m.answer("⏳ Загружаю баланс...")
  name = await get_emp(m.from_user)
  debts, items = await asyncio.to_thread(get_debts_sync, name)
  total = sum(x["amount"] for x in items)
  cur_period = get_pay_period(now_astana())

  taxi_sum = sum(x["amount"] for x in items if x["cat"] == "Такси")
  meals_sum = sum(x["amount"] for x in items if x["cat"] == "Обеденные")
  other_sum = sum(x["amount"] for x in items if x["cat"] == "Прочие расходы")

  status = f"💵 <b>К выплате вам: {total:,.0f} ₸</b>" if total > 0.01 else (
      f"🟢 <b>Аванс на балансе: +{abs(total):,.0f} ₸</b>" if total < -0.01 else "✨ <b>Все расчеты закрыты (0 ₸)</b>"
  )

  await wait.edit_text(
      f"👤 <b>Сотрудник: {name}</b>\n"
      f"📅 Период: <b>{cur_period}</b>\n━━━━━━━━━━━━━━\n"
      f"{status}\n\n"
      f"📊 <b>Ваши подтвержденные расходы:</b>\n"
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
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_reqs(c: types.CallbackQuery):
  await c.answer()
  waiting_reqs.add(c.from_user.id)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text("📝 <b>Отправьте номер Kaspi / ИИН одним сообщением:</b>", reply_markup=kb, parse_mode="HTML")


# =====================================================================
# 6. СМЕНЫ С КОММЕНТАРИЕМ И 6-ЧАСОВЫМ ТАЙМЕРОМ
# =====================================================================


async def finish_shift(uid: int, end_dt: datetime, auto: bool = False) -> str | None:
  shift = active_shifts.pop(uid, None)
  if not shift:
    return None

  dur = max(timedelta(0), end_dt - shift["start"])
  h_int, m_int = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)

  title = "⏰ <b>Смена закрыта автоматически (нет ответа)</b>" if auto else "🔴 <b>Смена завершена!</b>"
  return (
      f"{title}\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{shift['date']}</b>\n"
      f"📝 Примечание: <i>{shift['comment']}</i>\n"
      f"🕒 Время: {shift['start'].strftime('%H:%M')} ➔ {end_dt.strftime('%H:%M')}\n"
      f"⏱ Отработано: <b>{h_int} ч. {m_int} мин.</b>"
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
                await bot.send_message(uid, txt, parse_mode="HTML")
              except Exception:
                pass
              for aid in admins:
                try:
                  await bot.send_message(aid, f"ℹ️ <b>Автозакрытие:</b> {s['emp']} не ответил на 6ч таймер ({s['date']}).", parse_mode="HTML")
                except Exception:
                  pass
        elif now >= s["next_check"]:
          s["waiting"] = True
          s["prompt_at"] = now
          hrs = int((now - s["start"]).total_seconds() // 3600)
          kb = IMarkup(inline_keyboard=[
              [IBtn(text="🟢 Продолжаю работать (+6 ч)", callback_data="shift_extend")],
              [IBtn(text="🔴 Завершить смену", callback_data="shift_end")],
          ])
          try:
            await bot.send_message(
                uid,
                f"⏰ <b>Прошло {hrs} ч. смены!</b>\n━━━━━━━━━━━━━━\n"
                f"📅 Дата: <b>{s['date']}</b>\n📝 Примечание: {s['comment']}\nВы ещё на площадке?",
                reply_markup=kb,
                parse_mode="HTML",
            )
          except Exception:
            pass
    except Exception as e:
      print(f"Shift timer error: {e}")
    await asyncio.sleep(60)


@dp.message(F.text == "⏱ Моя смена")
async def msg_shift(m: types.Message):
  uid = m.from_user.id
  if uid not in active_shifts:
    kb = IMarkup(inline_keyboard=[
        [IBtn(text="🟢 Начать смену сейчас", callback_data="smode|now")],
        [IBtn(text="🕒 Начать задним числом (через Админа)", callback_data="smode|retro")],
    ])
    await m.answer("⏱ <b>Учет рабочего времени</b>\nУ вас нет активной смены. Выберите действие:", reply_markup=kb, parse_mode="HTML")
  else:
    s = active_shifts[uid]
    dur = now_astana() - s["start"]
    h, mn = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)
    btns = [[IBtn(text="🔴 Завершить смену", callback_data="shift_end")]]
    if s.get("waiting"):
      btns.insert(0, [IBtn(text="🟢 Продлить смену (+6 ч)", callback_data="shift_extend")])
    await m.answer(
        f"🟢 <b>Смена активна</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Дата: <b>{s['date']}</b>\n"
        f"📝 Примечание: <i>{s['comment']}</i>\n"
        f"🕒 Старт: <b>{s['start'].strftime('%H:%M')}</b> (в работе: {h} ч. {mn} мин.)\n"
        f"⏰ Следующая проверка: {s['next_check'].strftime('%H:%M')}",
        reply_markup=IMarkup(inline_keyboard=btns),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("smode|"))
async def cb_smode(c: types.CallbackQuery):
  await c.answer()
  mode = c.data.split("|")[1]
  act = "s_now" if mode == "now" else "s_retro"
  await c.message.edit_text("📅 <b>Выберите дату смены:</b>", reply_markup=date_picker_kb(act), parse_mode="HTML")


@dp.callback_query(F.data == "shift_extend")
async def cb_shift_extend(c: types.CallbackQuery):
  s = active_shifts.get(c.from_user.id)
  if not s:
    return await c.answer("Смена уже закрыта", show_alert=True)
  await c.answer("Продлено на 6 часов!")
  s["waiting"] = False
  s["next_check"] = now_astana() + timedelta(hours=SHIFT_INTERVAL_H)
  await c.message.edit_text(f"✅ <b>Смена продлена!</b>\nСледующая проверка в {s['next_check'].strftime('%H:%M')}.", parse_mode="HTML")


@dp.callback_query(F.data == "shift_end")
async def cb_shift_end(c: types.CallbackQuery):
  await c.answer()
  txt = await finish_shift(c.from_user.id, end_dt=now_astana(), auto=False)
  await c.message.edit_text(txt or "Смена уже завершена.", parse_mode="HTML")


@dp.message(Form.shift_comment, F.text)
async def process_shift_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  now = now_astana()
  nxt = now + timedelta(hours=SHIFT_INTERVAL_H)
  emp = await get_emp(m.from_user)

  active_shifts[m.from_user.id] = {
      "date": data["date"], "start": now, "emp": emp,
      "comment": m.text.strip(), "next_check": nxt, "waiting": False, "prompt_at": None,
  }
  await m.answer(
      f"🟢 <b>Смена начата!</b>\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{data['date']}</b>\n"
      f"📝 Примечание: <i>{m.text.strip()}</i>\n"
      f"🕒 Старт: <b>{now.strftime('%H:%M')}</b> (проверка в {nxt.strftime('%H:%M')})",
      reply_markup=main_kb(m.from_user.id),
      parse_mode="HTML",
  )


@dp.message(Form.retro_time, F.text)
async def process_retro_time(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  now = now_astana()
  target = now

  match = re.search(r"\b(\d{1,2})[:.](\d{2})\b", m.text)
  if match:
    target = now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0)
    if target > now:
      target -= timedelta(days=1)
  elif "час" in m.text:
    nums = re.findall(r"\d+", m.text)
    target = now - timedelta(hours=int(nums[0]) if nums else 2)

  rid = f"SR-{random.randint(1000, 9999)}"
  emp = await get_emp(m.from_user)
  pending_shifts[rid] = {"uid": m.from_user.id, "date": data["date"], "start": target, "emp": emp, "comment": "Задним числом"}

  kb = IMarkup(inline_keyboard=[[
      IBtn(text="✅ Одобрить", callback_data=f"s_appr|{rid}"),
      IBtn(text="❌ Отклонить", callback_data=f"s_rej|{rid}"),
  ]])
  for aid in admins:
    try:
      await bot.send_message(
          aid,
          f"⚠️ <b>Запрос смены задним числом</b>\n━━━━━━━━━━━━━━\n"
          f"👤 Сотрудник: <b>{emp}</b>\n📅 Дата: {data['date']}\n🕒 Старт: <b>{target.strftime('%H:%M')}</b>",
          reply_markup=kb,
          parse_mode="HTML",
      )
    except Exception:
      pass
  await m.answer(f"⏳ Запрос на старт с <b>{target.strftime('%H:%M')}</b> отправлен Администратору.", parse_mode="HTML")


@dp.callback_query(F.data.startswith(("s_appr|", "s_rej|")))
async def cb_retro_decision(c: types.CallbackQuery):
  if not is_admin(c.from_user.id):
    return await c.answer("Только для Главного Администратора!", show_alert=True)
  await c.answer()
  act, rid = c.data.split("|")
  req = pending_shifts.pop(rid, None)
  if not req:
    return await c.message.edit_text("Запрос уже обработан.")

  if act == "s_appr":
    active_shifts[req["uid"]] = {
        "date": req["date"], "start": req["start"], "emp": req["emp"],
        "comment": req["comment"], "next_check": now_astana() + timedelta(hours=SHIFT_INTERVAL_H),
        "waiting": False, "prompt_at": None,
    }
    await c.message.edit_text(f"✅ Смена <b>{req['emp']}</b> одобрена с {req['start'].strftime('%H:%M')}!", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], f"✅ <b>Администратор одобрил смену!</b>\n📅 {req['date']} ({req['start'].strftime('%H:%M')})", parse_mode="HTML")
    except Exception:
      pass
  else:
    await c.message.edit_text(f"❌ Запрос <b>{req['emp']}</b> отклонен.", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], "❌ Запрос на смену задним числом отклонен.", parse_mode="HTML")
    except Exception:
      pass


# =====================================================================
# 7. ЗАЯВИТЕЛЬНЫЙ ОБЕД (С ПОДТВЕРЖДЕНИЕМ УПРАВЛЯЮЩЕГО)
# =====================================================================


@dp.message(F.text == "🍔 Запросить обед")
async def msg_meal_request(m: types.Message):
  await m.answer(
      "🍔 <b>Запрос на обеденные (2 500 ₸)</b>\nВыберите дату смены, за которую запрашиваете обед:",
      reply_markup=date_picker_kb("req_meal"),
      parse_mode="HTML",
  )


@dp.message(Form.meal_comment, F.text)
async def process_meal_comment(m: types.Message, state: FSMContext):
  data = await state.get_data()
  await state.clear()
  emp = await get_emp(m.from_user)
  mid = f"ML-{random.randint(1000, 9999)}"

  pending_meal_requests[mid] = {
      "uid": m.from_user.id,
      "date": data["date"],
      "emp": emp,
      "comment": m.text.strip(),
  }

  kb = IMarkup(inline_keyboard=[[
      IBtn(text="✅ Одобрить 2 500 ₸", callback_data=f"ml_ok|{mid}"),
      IBtn(text="❌ Отклонить", callback_data=f"ml_no|{mid}"),
  ]])
  for lid in admins | managers:
    try:
      await bot.send_message(
          lid,
          f"🍔 <b>Запрос на обеденные (2 500 ₸)</b>\n━━━━━━━━━━━━━━\n"
          f"👤 Сотрудник: <b>{emp}</b>\n"
          f"📅 Дата: <b>{data['date']}</b>\n"
          f"📝 Примечание: <i>{m.text.strip()}</i>\n\nОдобрить начисление?",
          reply_markup=kb,
          parse_mode="HTML",
      )
    except Exception:
      pass

  await m.answer(f"⏳ <b>Запрос отправлен управляющему!</b>\n📅 Дата: {data['date']}\nПосле подтверждения 2 500 ₸ добавятся в баланс.", parse_mode="HTML")


@dp.callback_query(F.data.startswith(("ml_ok|", "ml_no|")))
async def cb_meal_decision(c: types.CallbackQuery):
  if not is_staff_lead(c.from_user.id):
    return await c.answer("Только для управляющих!", show_alert=True)
  await c.answer()
  act, mid = c.data.split("|")
  req = pending_meal_requests.pop(mid, None)
  if not req:
    return await c.message.edit_text("Запрос на обед уже обработан.")

  if act == "ml_ok":
    approver = c.from_user.full_name or "Управляющий"
    await asyncio.to_thread(
        add_operation_sync,
        req["date"],
        "Обеденные",
        2500,
        req["emp"],
        f"{req['comment']} (Одобрил: {approver})",
        "К возмещению",
    )
    await c.message.edit_text(f"✅ Обед для <b>{req['emp']}</b> (2 500 ₸) одобрен и зачислен в таблицу!", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], f"✅ <b>Ваш обед (2 500 ₸) за {req['date']} одобрен!</b> Сумма добавлена в баланс.", parse_mode="HTML")
    except Exception:
      pass
  else:
    await c.message.edit_text(f"❌ Запрос обеда для <b>{req['emp']}</b> отклонен.", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], f"❌ Запрос на обеденные за {req['date']} отклонен управляющим.", parse_mode="HTML")
    except Exception:
      pass


# =====================================================================
# 8. ТАКСИ, ЧЕКИ И МАРШРУТИЗАЦИЯ
# =====================================================================


@dp.message(F.text == "🚕 Такси")
async def msg_taxi_prompt(m: types.Message):
  now = now_astana()
  night = now.hour >= 22 or now.hour < 7
  mode_txt = "🌙 Ночной тариф (22:00–07:00) — начисление сразу!" if night else "☀️ Дневной тариф — через согласование."
  await m.answer(
      f"🚕 <b>Учет поездок на такси</b>\n━━━━━━━━━━━━━━\n"
      f"Отправьте скриншот поездки из Яндекс Go в этот чат.\n"
      f"<blockquote>{mode_txt}</blockquote>",
      parse_mode="HTML",
  )


@dp.message(F.text == "🧾 Прочие расходы")
async def msg_other_expenses_prompt(m: types.Message):
  await m.answer(
      "🧾 <b>Прочие расходы (закупки, расходники, субаренда)</b>\n━━━━━━━━━━━━━━\n"
      "Отправьте фото чека в этот чат. Бот автоматически сохранит его в папку «Чеки» на Диске и внесет в таблицу.",
      parse_mode="HTML",
  )


@dp.message(F.photo)
async def handle_receipt_photo(m: types.Message):
  photo = m.photo[-1]
  if photo.file_unique_id in saved_files:
    return await m.answer("⚠️ Этот чек уже был загружен ранее!")

  wait = await m.answer("🔍 <b>Сканирую чек и сохраняю в папку «Чеки»...</b>", parse_mode="HTML")
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
      reply_markup=date_picker_kb("check"),
      parse_mode="HTML",
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
        f"📅 Дата: <b>{label}</b>\n\n📝 <b>Напишите комментарий к запросу:</b>\n"
        f"<blockquote>Например: <code>Монтаж сцены левый берег</code></blockquote>",
        reply_markup=kb,
        parse_mode="HTML",
    )

  elif act == "s_now":
    await state.update_data(date=label)
    await state.set_state(Form.shift_comment)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n📝 <b>Укажите короткий комментарий к смене:</b>\n"
        f"<blockquote>Например: <code>Склад разгрузка</code> или <code>Концертный зал</code></blockquote>",
        reply_markup=kb,
        parse_mode="HTML",
    )

  elif act == "s_retro":
    await state.update_data(date=label)
    await state.set_state(Form.retro_time)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n🕒 <b>Во сколько фактически началась смена?</b>\n"
        f"<blockquote>Пример: <code>14:00</code> или <code>2 часа назад</code></blockquote>",
        reply_markup=kb,
        parse_mode="HTML",
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
      for lid in admins | managers:
        try:
          await bot.send_message(
              lid,
              f"🚕 <b>Дневное такси на согласовании</b>\n━━━━━━━━━━━━━━\n"
              f"👤 Сотрудник: <b>{rec['emp']}</b>\n💵 Сумма: <b>{rec['amount']:,.0f} ₸</b>\n📅 Дата: {label}",
              reply_markup=kb,
              parse_mode="HTML",
          )
        except Exception:
          pass
      return await c.message.edit_text(
          f"⏳ <b>Дневное такси отправлено на согласование!</b>\n📅 {label} — <b>{rec['amount']:,.0f} ₸</b>",
          parse_mode="HTML",
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
        f"📁 <i>Файл сохранен в папку «Чеки» на Google Диске.</i>",
        parse_mode="HTML",
    )

  elif act == "fastexp":
    await state.update_data(date=label)
    await state.set_state(Form.fast_expense)
    kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n✍️ <b>Напишите сумму и описание одной строкой:</b>\n"
        f"<blockquote>• <code>75000 субаренда</code> (с кассы ИП)\n• <code>15000 газель лично</code> (к возмещению вам)</blockquote>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith(("tx_ok|", "tx_no|")))
async def cb_taxi_decision(c: types.CallbackQuery):
  if not is_staff_lead(c.from_user.id):
    return await c.answer("Только для управляющих!", show_alert=True)
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
# 9. ПАНЕЛЬ УПРАВЛЕНИЯ И ВЫПЛАТЫ
# =====================================================================


async def render_control_panel(target, uid: int):
  btns = [
      [IBtn(text="⚡️ Внести быстрый расход", callback_data="adm_fast")],
      [IBtn(text=f"⏱ Смены онлайн ({len(active_shifts)})", callback_data="adm_shifts")],
  ]
  cur_period = get_pay_period(now_astana())

  if is_admin(uid):
    debts, _ = await asyncio.to_thread(get_debts_sync)
    pos_debts = {k: v for k, v in debts.items() if v > 0}
    tot = sum(pos_debts.values())
    header = (
        f"⚙️ <b>Панель Главного Администратора</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Текущий период: <b>{cur_period}</b>\n"
        f"💵 Общий долг перед командой: <b>{tot:,.0f} ₸</b>\n"
        f"🟢 Активных смен сейчас: <b>{len(active_shifts)}</b>"
    )
    btns.extend([
        [IBtn(text=f"💸 Выплаты команде ({len(pos_debts)})", callback_data="adm_paylist")],
        [IBtn(text="👥 Роли команды", callback_data="adm_roles"), IBtn(text="🔄 Формулы", callback_data="adm_fix")],
    ])
  else:
    header = (
        f"⚙️ <b>Панель Управляющего</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Текущий период: <b>{cur_period}</b>\n"
        f"🟢 Активных смен сейчас: <b>{len(active_shifts)}</b>"
    )

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
  await c.answer("Синхронизирую формулы...")
  await asyncio.to_thread(sync_roles_and_formulas)
  await c.message.answer("✅ Формулы и расчетные периоды обновлены!")


@dp.callback_query(F.data == "adm_fast")
async def cb_adm_fast(c: types.CallbackQuery):
  await c.answer()
  await c.message.edit_text("⚡️ <b>Выберите дату расхода:</b>", reply_markup=date_picker_kb("fastexp"), parse_mode="HTML")


@dp.message(Form.fast_expense, F.text)
async def process_fast_expense(m: types.Message, state: FSMContext):
  d = await state.get_data()
  await state.clear()
  wait = await m.answer("🤖 Разбираю расход...")

  prompt = (
      f'Разбери расход: "{m.text}". Верни JSON: '
      '{"amount": число, "category": "Такси" | "Прочие расходы", '
      '"desc": "суть", "personal": true если написано лично/свои иначе false}'
  )
  res = await ask_gemini(prompt) or {"amount": 0, "category": "Прочие расходы", "desc": m.text, "personal": False}
  amt = float(res.get("amount", 0))
  personal = res.get("personal", False)
  payer = await get_emp(m.from_user) if personal else "Касса / ИП"
  st = "К возмещению" if personal else "Не требуется"

  await asyncio.to_thread(
      add_operation_sync,
      d["date"], res.get("category", "Прочие расходы"), amt, payer,
      res.get("desc", m.text), st,
  )
  await wait.edit_text(
      f"✅ <b>Расход внесен!</b>\n━━━━━━━━━━━━━━\n"
      f"📅 {d['date']} | 💵 <b>{amt:,.0f} ₸</b>\n📂 {res.get('category')} ({payer})",
      reply_markup=IMarkup(inline_keyboard=[[IBtn(text="⬅️ В панель", callback_data="adm_back")]]),
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "adm_shifts")
async def cb_adm_shifts(c: types.CallbackQuery):
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
  await c.answer()
  rows = await asyncio.to_thread(lambda: spreadsheet.worksheet("Выплаты команде").get_all_values()[1:])
  kb = []
  for r in rows:
    if r and r[0].strip():
      name, role = r[0].strip(), (r[1].strip() if len(r) > 1 else "Команда")
      icon = "⭐️" if "управл" in role.lower() else "👤"
      kb.append([IBtn(text=f"{icon} {name} ({role})", callback_data=f"trole|{name[:20]}")])
  kb.append([IBtn(text="⬅️ Назад", callback_data="adm_back")])
  await c.message.edit_text("👥 <b>Нажмите на сотрудника для смены роли:</b>", reply_markup=IMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("trole|"))
async def cb_toggle_role(c: types.CallbackQuery):
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
  await c.answer()
  debts, _ = await asyncio.to_thread(get_debts_sync)
  cur_period = get_pay_period(now_astana())
  kb = []
  lines = []
  for emp, amt in debts.items():
    if amt > 0:
      lines.append(f"• <b>{emp}</b>: {amt:,.0f} ₸")
      kb.append([IBtn(text=f"💸 Выплатить: {emp} ({amt:,.0f} ₸)", callback_data=f"pay|{emp[:20]}")])
    elif amt < 0:
      lines.append(f"• <b>{emp}</b>: 🟢 аванс +{abs(amt):,.0f} ₸")
  kb.append([IBtn(text="⬅️ Назад", callback_data="adm_back")])
  body = "\n".join(lines) if lines else "🎉 Все долги перед командой закрыты!"
  await c.message.edit_text(f"💸 <b>Выплаты за период: {cur_period}</b>\n━━━━━━━━━━━━━━\n{body}", reply_markup=IMarkup(inline_keyboard=kb), parse_mode="HTML")


@dp.callback_query(F.data.startswith("pay|"))
async def cb_pay_prep(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  prefix = c.data.split("|")[1]
  debts, _ = await asyncio.to_thread(get_debts_sync)
  emp = next((k for k in debts if k.startswith(prefix)), prefix)
  debt = debts.get(emp, 0.0)

  def get_req():
    for r in spreadsheet.worksheet("Выплаты команде").get_all_values()[1:]:
      if len(r) >= 3 and r[0].strip() == emp:
        return r[2].strip().lstrip("'")
    return "Не указаны"

  reqs = await asyncio.to_thread(get_req)
  await state.update_data(emp=emp, debt=debt)
  kb = IMarkup(inline_keyboard=[
      [IBtn(text=f"✅ Выплатить ровно {debt:,.0f} ₸", callback_data="pay_exact")],
      [IBtn(text="✏️ Ввести другую сумму (с авансом)", callback_data="pay_custom")],
      [IBtn(text="⬅️ Назад", callback_data="adm_paylist")],
  ])
  await c.message.edit_text(
      f"💸 <b>Выплата сотруднику: {emp}</b>\n━━━━━━━━━━━━━━\n"
      f"💵 Долг к выплате: <b>{debt:,.0f} ₸</b>\n📋 Реквизиты:\n<blockquote><code>{reqs}</code></blockquote>",
      reply_markup=kb,
      parse_mode="HTML",
  )


def settle_payout_sync(emp: str, paid: float, debt: float):
  ws = spreadsheet.worksheet("Операции")
  for idx, r in enumerate(ws.get_all_values()[1:], start=2):
    if len(r) >= 9 and emp.lower() in r[3].lower() and "к возмещению" in r[8].lower():
      ws.update_cell(idx, 9, "Выплачено")

  diff = paid - debt
  if abs(diff) > 0.01:
    amt = -diff if diff > 0 else abs(diff)
    note = (
        f"Аванс на баланс (выплачено {paid:,.0f} при долге {debt:,.0f})"
        if diff > 0
        else f"Остаток долга (выплачено {paid:,.0f} из {debt:,.0f})"
    )
    add_operation_sync(now_astana().strftime("%d.%m.%Y"), "Прочие расходы", amt, emp, note, "К возмещению")


@dp.callback_query(F.data == "pay_exact")
async def cb_pay_exact(c: types.CallbackQuery, state: FSMContext):
  await c.answer("Провожу выплату...")
  d = await state.get_data()
  await state.clear()
  await asyncio.to_thread(settle_payout_sync, d["emp"], d["debt"], d["debt"])
  await c.message.edit_text(f"✅ Выплата <b>{d['debt']:,.0f} ₸</b> для <b>{d['emp']}</b> закрыта!", parse_mode="HTML")
  await render_control_panel(c, c.from_user.id)


@dp.callback_query(F.data == "pay_custom")
async def cb_pay_custom(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  d = await state.get_data()
  await state.set_state(Form.custom_payout)
  kb = IMarkup(inline_keyboard=[[IBtn(text="❌ Отмена", callback_data="cancel_fsm_action")]])
  await c.message.edit_text(
      f"👤 <b>{d['emp']}</b> (долг: {d['debt']:,.0f} ₸)\n\n"
      f"💸 <b>Введите фактически переведенную сумму:</b>\n"
      f"<blockquote>Если сумма больше долга, разница перейдет в аванс.</blockquote>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.message(Form.custom_payout, F.text)
async def process_custom_pay(m: types.Message, state: FSMContext):
  try:
    paid = float(m.text.replace(" ", "").replace(",", "."))
  except ValueError:
    return await m.answer("⚠️ Введите сумму числом:")

  d = await state.get_data()
  await state.clear()
  await asyncio.to_thread(settle_payout_sync, d["emp"], paid, d["debt"])
  diff = paid - d["debt"]
  extra = f"\n🟢 Аванс на баланс: <b>+{diff:,.0f} ₸</b>" if diff > 0 else (f"\n⚠️ Остаток долга: <b>{abs(diff):,.0f} ₸</b>" if diff < 0 else "")
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
# 10. ЗАПУСК
# =====================================================================


async def main():
  app = web.Application()
  app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
  runner = web.AppRunner(app)
  await runner.setup()
  await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

  await asyncio.to_thread(sync_roles_and_formulas)
  asyncio.create_task(shift_timer_loop())
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())