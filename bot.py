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
# 1. КОНФИГУРАЦИЯ И ПОДКЛЮЧЕНИЯ
# =====================================================================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA").strip()
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()

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

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
]
gc = gspread.service_account(filename="credentials.json", scopes=SCOPES)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# Кэш и состояния в памяти
admins = set(INITIAL_ADMINS)
managers = set()
emp_cache = {}
active_shifts = {}
pending_receipts = {}
pending_shifts = {}
waiting_reqs = set()
saved_files = set()

MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

NAV_BTNS = {
    "⏱ Моя смена", "🍔 Обед (2 500 ₸)", "🚕 Такси",
    "🧾 Отправить чек", "💰 Мой баланс", "💳 Реквизиты", "⚙️ Управление",
}


class Form(StatesGroup):
  fast_expense = State()
  retro_time = State()
  custom_payout = State()


def now_astana() -> datetime:
  return datetime.now(TZ_ASTANA)


def is_admin(uid: int) -> bool:
  return uid in admins


def is_staff_lead(uid: int) -> bool:
  return uid in admins or uid in managers


# =====================================================================
# 2. ИИ, GOOGLE ДИСК, КАЛЕНДАРЬ И ТАБЛИЦА
# =====================================================================


async def ask_gemini(contents) -> dict | None:
  models = ["gemini-3.6-flash", "gemini-3.6-flash", "gemini-2.5-flash", "gemini-2.0-flash"]
  cfg = genai_types.GenerateContentConfig(
      response_mime_type="application/json", temperature=0.1
  )
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
    meta = json.dumps({"name": filename, "mimeType": "image/jpeg"}).encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta
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
    print(f"Drive error: {e}")
    return None


def resolve_calendar_sync(date_str: str) -> str:
  if date_str == "Склад / Общее" or not GOOGLE_CALENDAR_ID:
    return date_str
  try:
    dt = datetime.strptime(date_str, "%d.%m.%Y").date()
    creds = gc.auth
    if not creds.token:
      from google.auth.transport.requests import Request
      creds.refresh(Request())

    t_min = datetime(dt.year, dt.month, dt.day, 0, 0, tzinfo=TZ_ASTANA).isoformat()
    t_max = datetime(dt.year, dt.month, dt.day, 23, 59, tzinfo=TZ_ASTANA).isoformat()
    cal_id = urllib.parse.quote(GOOGLE_CALENDAR_ID)
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events?timeMin={ urllib.parse.quote(t_min)}&timeMax={urllib.parse.quote(t_max)}&singleEvents=true"

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {creds.token}"})
    with urllib.request.urlopen(req, timeout=6) as r:
      items = json.loads(r.read().decode()).get("items", [])
      names = [i.get("summary", "").strip() for i in items if i.get("summary")]
      return f"{date_str} ({', '.join(names)[:35]})" if names else date_str
  except Exception:
    return date_str


def sync_roles_and_formulas():
  try:
    ws_pay = spreadsheet.worksheet("Выплаты команде")
    pay_rows = ws_pay.get_all_values()
    ws_ref = spreadsheet.worksheet("Справочники")
    ref_rows = ws_ref.get_all_records()

    mgr_names = set()
    for idx, row in enumerate(pay_rows[1:], start=2):
      if not row or not row[0].strip():
        continue
      name = row[0].strip()
      role = row[1].strip().lower() if len(row) > 1 else ""
      if any(w in role for w in ["управл", "менедж", "admin"]):
        mgr_names.add(name.lower())

      ws_pay.update_cell(idx, 4, f'=SUMIFS(Операции!F:F; Операции!G:G; A{idx}; Операции!H:H; "К возмещению")')
      ws_pay.update_cell(idx, 5, f'=SUMIFS(Операции!F:F; Операции!G:G; A{idx}; Операции!H:H; "Выплачено")')

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
      ws_ref.append_row(["", "", name, f"@{user.username}" if user.username else "-", uid])

    ws_pay = spreadsheet.worksheet("Выплаты команде")
    names = ws_pay.col_values(1)
    if name not in names:
      r_idx = len(names) + 1
      ws_pay.append_row(
          [
              name, "Команда", "-",
              f'=SUMIFS(Операции!F:F; Операции!G:G; A{r_idx}; Операции!H:H; "К возмещению")',
              f'=SUMIFS(Операции!F:F; Операции!G:G; A{r_idx}; Операции!H:H; "Выплачено")',
          ],
          value_input_option="USER_ENTERED",
      )
  except Exception as e:
    print(f"Emp error: {e}")

  emp_cache[uid] = name
  return name


async def get_emp(user: types.User) -> str:
  return emp_cache.get(str(user.id)) or await asyncio.to_thread(get_employee_sync, user)


def add_operation_sync(
    date_label: str, category: str, amount: float, employee: str,
    comment: str, status: str = "К возмещению", check_col: str = "Без чека", tx_id: str = None,
):
  ws = spreadsheet.worksheet("Операции")
  tx = tx_id or f"TX-{random.randint(10000, 99999)}"
  dt_str = now_astana().strftime("%d.%m.%Y %H:%M")
  ws.append_row(
      [tx, dt_str, "Расход", date_label, category, amount, employee, status, check_col, comment],
      value_input_option="USER_ENTERED",
  )


def get_debts_sync(filter_emp: str = None):
  """Читает лист Операции (F=Сумма, G=Кто, H=Статус, D=Дата, J=Комментарий)"""
  rows = spreadsheet.worksheet("Операции").get_all_values()[1:]
  debts = {}
  items = []
  for r in rows:
    if len(r) < 8 or "к возмещению" not in r[7].strip().lower():
      continue
    who = r[6].strip()
    if not who:
      continue
    try:
      amt = float(str(r[5]).replace(" ", "").replace(",", "."))
    except ValueError:
      amt = 0.0

    debts[who] = debts.get(who, 0.0) + amt
    if filter_emp and (filter_emp.lower() in who.lower() or who.lower() in filter_emp.lower()):
      items.append({"date": r[3], "amount": amt, "comment": r[9] if len(r) > 9 else "-"})
  return debts, items


# =====================================================================
# 3. КЛАВИАТУРЫ И УНИВЕРСАЛЬНЫЙ КАЛЕНДАРЬ
# =====================================================================


def main_kb(uid: int) -> RMarkup:
  kb = [
      [KBtn(text="⏱ Моя смена"), KBtn(text="🍔 Обед (2 500 ₸)")],
      [KBtn(text="🚕 Такси"), KBtn(text="🧾 Отправить чек")],
      [KBtn(text="💰 Мой баланс"), KBtn(text="💳 Реквизиты")],
  ]
  if is_staff_lead(uid):
    kb.append([KBtn(text="⚙️ Управление")])
  return RMarkup(keyboard=kb, resize_keyboard=True)


def date_picker_kb(action: str) -> IMarkup:
  now = now_astana()
  d0 = now.strftime("%d.%m.%Y")
  d_prev = (now - timedelta(days=1)).strftime("%d.%m.%Y")
  d_next = (now + timedelta(days=1)).strftime("%d.%m.%Y")
  return IMarkup(inline_keyboard=[
      [IBtn(text=f"📍 Сегодня ({d0})", callback_data=f"pdate|{action}|{d0}")],
      [
          IBtn(text=f"⏪ Вчера ({d_prev[:5]})", callback_data=f"pdate|{action}|{d_prev}"),
          IBtn(text=f"⏩ Завтра ({d_next[:5]})", callback_data=f"pdate|{action}|{d_next}"),
      ],
      [IBtn(text="🗓 Выбрать день в календаре", callback_data=f"cal|{action}|{now.year}|{now.month}")],
      [IBtn(text="🏢 Склад / Общее", callback_data=f"pdate|{action}|Склад / Общее")],
  ])


def month_grid_kb(action: str, year: int, month: int) -> IMarkup:
  kb = [[IBtn(text=f"🗓 {MONTHS[month]} {year}", callback_data="noop")]]
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
# 4. БАЗОВОЕ МЕНЮ, БАЛАНС И РЕКВИЗИТЫ
# =====================================================================


@dp.message(CommandStart())
async def cmd_start(m: types.Message):
  name = await get_emp(m.from_user)
  await m.answer(
      f"👋 <b>Привет, {name}!</b>\n"
      f"━━━━━━━━━━━━━━\n"
      f"🕒 Часовой пояс: <b>Астана ({now_astana().strftime('%H:%M')})</b>\n\n"
      f"<blockquote>💡 <b>Быстрый совет:</b> Вы можете просто отправить фото чека или скриншот такси в этот чат в любой момент — бот сам распознает сумму и предложит дату!</blockquote>",
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

  if total < -0.01:
    status = f"🟢 <b>Аванс на балансе: +{abs(total):,.0f} ₸</b>\n<i>Сумма автоматически покроет ваши следующие чеки.</i>"
  elif total > 0.01:
    status = f"💵 <b>К выплате вам: {total:,.0f} ₸</b>"
  else:
    status = "✨ <b>Все расчеты закрыты (0 ₸)</b>"

  history = "\n".join([f"• <b>{i['amount']:,.0f} ₸</b> — {i['date']} <i>({i['comment']})</i>" for i in items[-8:]])
  hist_block = f"\n\n📋 <b>Последние записи:</b>\n{history}" if items else ""

  await wait.edit_text(
      f"👤 <b>Сотрудник: {name}</b>\n━━━━━━━━━━━━━━\n{status}{hist_block}",
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
      f"💳 <b>Ваши реквизиты для переводов</b>\n━━━━━━━━━━━━━━\n"
      f"👤 <b>{name}</b>\n<blockquote><code>{reqs}</code></blockquote>",
      reply_markup=kb,
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "edit_reqs")
async def cb_edit_reqs(c: types.CallbackQuery):
  await c.answer()
  waiting_reqs.add(c.from_user.id)
  await c.message.edit_text(
      "📝 <b>Отправьте ваши реквизиты одним сообщением</b>\n"
      "<blockquote>Пример: +7 777 123 45 67 Kaspi / ИИН</blockquote>",
      parse_mode="HTML",
  )


# =====================================================================
# 5. СМЕНЫ И 6-ЧАСОВОЙ ТАЙМЕР
# =====================================================================


def next_6h_mark(start: datetime, now: datetime) -> datetime:
  elapsed_h = max(0, (now - start).total_seconds()) / 3600.0
  blocks = (int(elapsed_h // SHIFT_INTERVAL_H) + 1) * SHIFT_INTERVAL_H
  return start + timedelta(hours=blocks)


async def finish_shift(uid: int, end_dt: datetime, auto: bool = False) -> str | None:
  shift = active_shifts.pop(uid, None)
  if not shift:
    return None

  dur = max(timedelta(0), end_dt - shift["start"])
  hours = dur.total_seconds() / 3600.0
  h_int, m_int = int(dur.total_seconds() // 3600), int((dur.total_seconds() % 3600) // 60)

  earned_meals = int(hours // 6)
  unclaimed = max(0, earned_meals - shift["meals"])
  payout = unclaimed * 2500

  if payout > 0:
    note = "автозакрытие" if auto else "остаток за смену"
    await asyncio.to_thread(
        add_operation_sync, shift["date"], "Питание команды", payout,
        shift["emp"], f"Обеденные ({h_int} ч, {note})",
    )

  title = "⏰ <b>Смена закрыта автоматически (нет ответа)</b>" if auto else "🔴 <b>Смена успешно завершена!</b>"
  pay_line = f"\n💰 Начислено за обеды: <b>+{payout:,.0f} ₸</b>" if payout > 0 else "\n👌 Обеденные уже учтены."

  return (
      f"{title}\n━━━━━━━━━━━━━━\n"
      f"📅 Дата: <b>{shift['date']}</b>\n"
      f"🕒 Время: {shift['start'].strftime('%H:%M')} ➔ {end_dt.strftime('%H:%M')}\n"
      f"⏱ Отработано: <b>{h_int} ч. {m_int} мин.</b>{pay_line}"
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
                  await bot.send_message(
                      aid, f"ℹ️ <b>Автозакрытие смены:</b> {s['emp']} не ответил на 6ч таймер ({s['date']}).",
                      parse_mode="HTML",
                  )
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
                f"⏰ <b>Прошло {hrs} ч. вашей смены!</b>\n━━━━━━━━━━━━━━\n"
                f"📅 Дата: <b>{s['date']}</b>\nВы ещё на площадке?\n"
                f"<blockquote>Подтвердите в течение {SHIFT_TIMEOUT_M} мин, иначе смена закроется автоматически.</blockquote>",
                reply_markup=kb,
                parse_mode="HTML",
            )
          except Exception:
            pass
    except Exception as e:
      print(f"Timer error: {e}")
    await asyncio.sleep(60)


@dp.message(F.text == "⏱ Моя смена")
async def msg_shift(m: types.Message):
  uid = m.from_user.id
  if uid not in active_shifts:
    kb = IMarkup(inline_keyboard=[
        [IBtn(text="🟢 Начать смену сейчас", callback_data="smode|now")],
        [IBtn(text="🕒 Начать задним числом (через Админа)", callback_data="smode|retro")],
    ])
    await m.answer(
        "⏱ <b>Управление рабочей сменой</b>\n━━━━━━━━━━━━━━\n"
        "Сейчас у вас нет активной смены. Каждые 6 часов смены автоматически начисляют <b>2 500 ₸</b> обеденных.",
        reply_markup=kb,
        parse_mode="HTML",
    )
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
        f"🕒 Старт: <b>{s['start'].strftime('%H:%M')}</b> (в работе: {h} ч. {mn} мин.)\n"
        f"⏰ Проверка таймера: {s['next_check'].strftime('%H:%M')}",
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
  await c.message.edit_text(
      f"✅ <b>Смена продлена!</b>\nСледующая проверка в {s['next_check'].strftime('%H:%M')}.",
      parse_mode="HTML",
  )


@dp.callback_query(F.data == "shift_end")
async def cb_shift_end(c: types.CallbackQuery):
  await c.answer()
  txt = await finish_shift(c.from_user.id, end_dt=now_astana(), auto=False)
  await c.message.edit_text(txt or "Смена уже завершена.", parse_mode="HTML")


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
  pending_shifts[rid] = {"uid": m.from_user.id, "date": data["date"], "start": target, "emp": emp}

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
    now = now_astana()
    nxt = next_6h_mark(req["start"], now)
    active_shifts[req["uid"]] = {
        "date": req["date"], "start": req["start"], "emp": req["emp"],
        "meals": 0, "next_check": nxt, "waiting": False, "prompt_at": None,
    }
    await c.message.edit_text(f"✅ Смена <b>{req['emp']}</b> одобрена с {req['start'].strftime('%H:%M')}!", parse_mode="HTML")
    try:
      await bot.send_message(
          req["uid"],
          f"✅ <b>Администратор одобрил вашу смену!</b>\n📅 {req['date']} (старт: {req['start'].strftime('%H:%M')})",
          parse_mode="HTML",
      )
    except Exception:
      pass
  else:
    await c.message.edit_text(f"❌ Запрос <b>{req['emp']}</b> отклонен.", parse_mode="HTML")
    try:
      await bot.send_message(req["uid"], "❌ Запрос на смену задним числом отклонен.", parse_mode="HTML")
    except Exception:
      pass


# =====================================================================
# 6. ОБЕДЫ, ТАКСИ, ЧЕКИ И ЕДИНЫЙ ОБРАБОТЧИК ДАТ
# =====================================================================


@dp.message(F.text == "🍔 Обед (2 500 ₸)")
async def msg_meal(m: types.Message):
  await m.answer(
      "🍔 <b>Начисление обеденных (2 500 ₸)</b>\nВыберите дату вашей смены:",
      reply_markup=date_picker_kb("meal"),
      parse_mode="HTML",
  )


@dp.message(F.text.in_({"🚕 Такси", "🧾 Отправить чек"}))
async def msg_send_receipt_prompt(m: types.Message):
  now = now_astana()
  night = now.hour >= 22 or now.hour < 7
  mode_txt = "🌙 Ночной тариф (22:00–07:00) — автоодобрение!" if night else "☀️ Дневное такси — через согласование."
  await m.answer(
      f"📸 <b>Отправьте фото чека или скриншот</b>\n━━━━━━━━━━━━━━\n"
      f"Прикрепите изображение прямо в этот чат. ИИ сам считает сумму и категорию.\n"
      f"<blockquote>🚕 Для такси сейчас: {mode_txt}</blockquote>",
      parse_mode="HTML",
  )


@dp.message(F.photo)
async def handle_receipt_photo(m: types.Message):
  photo = m.photo[-1]
  if photo.file_unique_id in saved_files:
    return await m.answer("⚠️ Этот чек уже был загружен ранее!")

  wait = await m.answer("🔍 <b>Сканирую чек и загружаю в облако...</b>", parse_mode="HTML")
  buf = io.BytesIO()
  await bot.download(photo, destination=buf)
  img_bytes = buf.getvalue()

  fname = f"check_{m.from_user.id}_{now_astana().strftime('%d%m_%H%M%S')}.jpg"
  drive_url = await asyncio.to_thread(upload_to_drive_sync, img_bytes, fname)

  prompt = (
      "Изучи чек и верни ТОЛЬКО JSON: "
      '{"amount": число, "merchant": "продавец", '
      '"category": "Такси / Логистика / ГСМ" | "Питание команды" | "Расходники (тейп, батарейки)" | "Субаренда оборудования" | "Прочее", '
      '"description": "2-4 слова"}'
  )
  data = await ask_gemini([genai_types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt])
  if not data:
    return await wait.edit_text("⚠️ Не удалось распознать изображение. Попробуйте отправить фото еще раз.")

  amt = float(data.get("amount", 0))
  merch = data.get("merchant", "Чек")
  cat = data.get("category", "Прочее")
  desc = data.get("description", "")
  emp = await get_emp(m.from_user)

  pending_receipts[m.from_user.id] = {
      "amount": amt, "merchant": merch, "category": cat,
      "desc": desc, "emp": emp, "link": drive_url, "fid": photo.file_unique_id,
  }

  now = now_astana()
  is_taxi = "такси" in cat.lower() or "яндекс" in merch.lower()
  badge = ""
  if is_taxi:
    badge = "\n🌙 <i>Ночное такси (автоодобрение)</i>" if (now.hour >= 22 or now.hour < 7) else "\n☀️ <i>Дневное такси (на согласование)</i>"

  await wait.edit_text(
      f"🧾 <b>Распознан чек: {amt:,.0f} ₸</b>\n━━━━━━━━━━━━━━\n"
      f"🏪 Продавец: <b>{merch}</b>\n"
      f"📂 Категория: {cat} <i>({desc})</i>{badge}\n\n"
      f"📅 <b>Выберите дату расхода:</b>",
      reply_markup=date_picker_kb("check"),
      parse_mode="HTML",
  )


# Единый маршрутизатор выбора даты для всех функций
@dp.callback_query(F.data.startswith("pdate|"))
async def cb_universal_date_picked(c: types.CallbackQuery, state: FSMContext):
  await c.answer()
  _, act, raw_date = c.data.split("|")
  label = await asyncio.to_thread(resolve_calendar_sync, raw_date)
  uid = c.from_user.id

  # 1. Обед
  if act == "meal":
    emp = await get_emp(c.from_user)
    if uid in active_shifts:
      active_shifts[uid]["meals"] += 1
    await asyncio.to_thread(add_operation_sync, label, "Питание команды", 2500, emp, "Обеденные (норматив)")
    await c.message.edit_text(f"✅ <b>Обеденные 2 500 ₸ начислены!</b>\n📅 Дата: <b>{label}</b>", parse_mode="HTML")

  # 2. Старт смены сейчас
  elif act == "s_now":
    emp = await get_emp(c.from_user)
    now = now_astana()
    nxt = now + timedelta(hours=SHIFT_INTERVAL_H)
    active_shifts[uid] = {
        "date": label, "start": now, "emp": emp,
        "meals": 0, "next_check": nxt, "waiting": False, "prompt_at": None,
    }
    await c.message.edit_text(
        f"🟢 <b>Смена запущена!</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Дата: <b>{label}</b>\n🕒 Старт: <b>{now.strftime('%H:%M')}</b>\n"
        f"⏰ Проверка таймера (6 ч): {nxt.strftime('%H:%M')}",
        parse_mode="HTML",
    )

  # 3. Старт смены задним числом
  elif act == "s_retro":
    await state.update_data(date=label)
    await state.set_state(Form.retro_time)
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n🕒 <b>Введите фактическое время начала смены:</b>\n"
        f"<blockquote>Например: <code>14:00</code> или <code>3 часа назад</code></blockquote>",
        parse_mode="HTML",
    )

  # 4. Сохранение фото-чека
  elif act == "check":
    rec = pending_receipts.pop(uid, None)
    if not rec:
      return await c.message.edit_text("⚠️ Данные чека устарели.")

    now = now_astana()
    is_taxi = "такси" in rec["category"].lower() or "яндекс" in rec["merchant"].lower()
    check_col = f'=HYPERLINK("{rec["link"]}"; "📎 Открыть чек")' if rec["link"] else "Чек в TG"

    if is_taxi and not (now.hour >= 22 or now.hour < 7):
      tx_id = f"TX-{random.randint(10000, 99999)}"
      await asyncio.to_thread(
          add_operation_sync, label, rec["category"], rec["amount"], rec["emp"],
          f"{rec['merchant']}: {rec['desc']} (Дневное такси)", "На согласовании", check_col, tx_id,
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
              f"👤 <b>{rec['emp']}</b>\n💵 Сумма: <b>{rec['amount']:,.0f} ₸</b>\n📅 Дата: {label}",
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
        add_operation_sync, label, rec["category"], rec["amount"], rec["emp"],
        f"{rec['merchant']}: {rec['desc']}", "К возмещению", check_col,
    )
    saved_files.add(rec["fid"])
    await c.message.edit_text(
        f"✅ <b>Чек сохранен в таблицу!</b>\n━━━━━━━━━━━━━━\n"
        f"📅 Дата: <b>{label}</b>\n💵 Сумма: <b>{rec['amount']:,.0f} ₸</b>\n👤 {rec['emp']}",
        parse_mode="HTML",
    )

  # 5. Быстрый расход админа
  elif act == "fastexp":
    await state.update_data(date=label)
    await state.set_state(Form.fast_expense)
    await c.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\n✍️ <b>Напишите сумму и описание одной строкой:</b>\n"
        f"<blockquote>• <code>75000 субаренда микрофонов</code> (с кассы ИП)\n"
        f"• <code>15000 газель лично</code> (к возмещению вам)</blockquote>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith(("tx_ok|", "tx_no|")))
async def cb_taxi_decision(c: types.CallbackQuery):
  await c.answer()
  act, tx_id = c.data.split("|")
  new_st = "К возмещению" if act == "tx_ok" else "Отклонено"

  def upd():
    ws = spreadsheet.worksheet("Операции")
    for idx, r in enumerate(ws.get_all_values()[1:], start=2):
      if r and r[0].strip() == tx_id:
        ws.update_cell(idx, 8, new_st)
        return True
    return False

  await asyncio.to_thread(upd)
  icon = "✅ Одобрено" if act == "tx_ok" else "❌ Отклонено"
  await c.message.edit_text(f"{icon}: статус транзакции {tx_id} обновлен на <b>{new_st}</b>.", parse_mode="HTML")


# =====================================================================
# 7. ПАНЕЛЬ УПРАВЛЕНИЯ И ВЫПЛАТЫ
# =====================================================================


async def render_control_panel(target, uid: int):
  btns = [
      [IBtn(text="⚡️ Внести быстрый расход", callback_data="adm_fast")],
      [IBtn(text=f"⏱ Смены онлайн ({len(active_shifts)})", callback_data="adm_shifts")],
  ]
  if is_admin(uid):
    debts, _ = await asyncio.to_thread(get_debts_sync)
    pos_debts = {k: v for k, v in debts.items() if v > 0}
    tot = sum(pos_debts.values())
    header = (
        f"⚙️ <b>Панель Главного Администратора</b>\n━━━━━━━━━━━━━━\n"
        f"💵 Долг перед командой: <b>{tot:,.0f} ₸</b>\n"
        f"🟢 Активных смен сейчас: <b>{len(active_shifts)}</b>"
    )
    btns.extend([
        [IBtn(text=f"💸 Выплаты команде ({len(pos_debts)})", callback_data="adm_paylist")],
        [IBtn(text="👥 Роли управляющих", callback_data="adm_roles"), IBtn(text="🔄 Формулы", callback_data="adm_fix")],
    ])
  else:
    header = f"⚙️ <b>Панель Управляющего</b>\n━━━━━━━━━━━━━━\n🟢 Активных смен сейчас: <b>{len(active_shifts)}</b>"

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
  await c.answer("Синхронизирую...")
  await asyncio.to_thread(sync_roles_and_formulas)
  await c.message.answer("✅ Формулы и роли синхронизированы!")


@dp.callback_query(F.data == "adm_fast")
async def cb_adm_fast(c: types.CallbackQuery):
  await c.answer()
  await c.message.edit_text("⚡️ <b>Выберите дату расхода:</b>", reply_markup=date_picker_kb("fastexp"), parse_mode="HTML")


@dp.message(Form.fast_expense, F.text)
async def process_fast_expense(m: types.Message, state: FSMContext):
  d = await state.get_data()
  await state.clear()
  wait = await m.answer("🤖 Записываю расход...")

  prompt = (
      f'Разбери расход: "{m.text}". Верни JSON: '
      '{"amount": число, "category": "Субаренда оборудования"|"Гонорары наемным техникам"|"Такси / Логистика / ГСМ"|"Расходники (тейп, батарейки)"|"Прочее", '
      '"desc": "суть", "personal": true если написано лично/свои иначе false}'
  )
  res = await ask_gemini(prompt) or {"amount": 0, "category": "Прочее", "desc": m.text, "personal": False}
  amt = float(res.get("amount", 0))
  personal = res.get("personal", False)
  payer = await get_emp(m.from_user) if personal else "Касса / ИП"
  st = "К возмещению" if personal else "Не требуется"

  await asyncio.to_thread(add_operation_sync, d["date"], res.get("category", "Прочее"), amt, payer, res.get("desc", m.text), st, "Вручную")
  await wait.edit_text(
      f"✅ <b>Расход добавлен!</b>\n━━━━━━━━━━━━━━\n"
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
      lines.append(f"• <b>{s['emp']}</b> — {s['date']}\n  🕒 с {s['start'].strftime('%H:%M')} ({h} ч. {mn} мин.){w}")
    txt = "⏱ <b>Сотрудники на смене:</b>\n━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
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
  await c.message.edit_text(f"💸 <b>Взаиморасчеты с командой</b>\n━━━━━━━━━━━━━━\n{body}", reply_markup=IMarkup(inline_keyboard=kb), parse_mode="HTML")


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
      f"💵 Долг: <b>{debt:,.0f} ₸</b>\n📋 Реквизиты:\n<blockquote><code>{reqs}</code></blockquote>",
      reply_markup=kb,
      parse_mode="HTML",
  )


def settle_payout_sync(emp: str, paid: float, debt: float):
  ws = spreadsheet.worksheet("Операции")
  for idx, r in enumerate(ws.get_all_values()[1:], start=2):
    if len(r) >= 8 and emp.lower() in r[6].lower() and "к возмещению" in r[7].lower():
      ws.update_cell(idx, 8, "Выплачено")

  diff = paid - debt
  if abs(diff) > 0.01:
    amt = -diff if diff > 0 else abs(diff)
    note = f"Аванс на баланс (выплачено {paid:,.0f} при долге {debt:,.0f})" if diff > 0 else f"Остаток долга (выплачено {paid:,.0f} из {debt:,.0f})"
    add_operation_sync("Склад / Общее", "Взаиморасчеты", amt, emp, note, "К возмещению", "Перевод Kaspi")


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
  await c.message.edit_text(
      f"👤 <b>{d['emp']}</b> (долг: {d['debt']:,.0f} ₸)\n\n"
      f"💸 <b>Введите фактически переведенную сумму числом:</b>\n"
      f"<blockquote>Если сумма больше долга, разница станет авансом сотрудника.</blockquote>",
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


# Перехват ввода реквизитов
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
# 8. ЗАПУСК СЕРВЕРА И БОТА
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