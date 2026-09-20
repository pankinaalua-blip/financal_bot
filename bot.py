import asyncio
from datetime import datetime
import io
import json
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
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
from aiohttp import web

# 1. Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [
    int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()
]

SPREADSHEET_ID = "1IjR1yXggPyOiziDKMiQ7GJSijbc8bVbxuMiKCnUGYAA"

# 2. Инициализация Telegram и ИИ
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_KEY)

# 3. Подключение к Google Таблицам
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

pending_receipts = {}


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---


def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
  """Создает нижнее меню кнопок в зависимости от прав (админ или сотрудник)"""
  keyboard = [
      [
          KeyboardButton(text="📸 Как отправить чек"),
          KeyboardButton(text="💰 Мои чеки"),
      ]
  ]
  # Если пишет администратор — добавляем кнопку управления выплатами
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
        if str(r.get("Статус проекта")).strip().lower() == "в работе"
    ]
    return (
        active
        if active
        else ["25.09 Концерт Баста", "27.09 Свадьба Rixos", "Склад / Общее"]
    )
  except Exception:
    return ["25.09 Концерт Баста", "27.09 Свадьба Rixos", "Склад / Общее"]


def get_debts_summary():
  ws = spreadsheet.worksheet("Операции")
  rows = ws.get_all_records()
  debts = {}
  for r in rows:
    status = str(r.get("Статус выплат", "")).strip().lower()
    who = str(r.get("Кто оплатил", "")).strip()
    amount_raw = r.get("Сумма (₸)", 0)

    if status == "к возмещению" and who:
      try:
        amount = float(str(amount_raw).replace(" ", "").replace(",", "."))
      except ValueError:
        amount = 0.0
      debts[who] = debts.get(who, 0.0) + amount
  return debts


def get_user_pending_receipts(user_name: str):
  """Ищет невозмещенные чеки конкретного сотрудника"""
  ws = spreadsheet.worksheet("Операции")
  rows = ws.get_all_records()
  user_items = []
  total = 0.0

  for r in rows:
    who = str(r.get("Кто оплатил", "")).strip()
    status = str(r.get("Статус выплат", "")).strip().lower()
    if who == user_name and status == "к возмещению":
      try:
        amt = float(
            str(r.get("Сумма (₸)", 0)).replace(" ", "").replace(",", ".")
        )
      except ValueError:
        amt = 0.0
      total += amt
      user_items.append({
          "project": r.get("Проект", "-"),
          "amount": amt,
          "comment": r.get("Комментарий", "-"),
          "date": r.get("Дата и время", "-"),
      })
  return total, user_items


# --- ОБРАБОТЧИКИ МЕНЮ И КОМАНД ---


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
  kb = get_main_keyboard(message.from_user.id)
  await message.answer(
      f"Привет, {message.from_user.first_name}! 🎛️\n\n"
      "Я бот для учета расходов на площадках.\n"
      "Просто отправь мне фото чека (питание, такси, расходники), "
      "и я привяжу его к нужному проекту.",
      reply_markup=kb,
  )


@dp.message(F.text == "📸 Как отправить чек")
async def msg_how_to(message: types.Message):
  await message.answer(
      "📷 <b>Как отправить чек:</b>\n\n"
      "1. Нажмите на скрепку внизу и сделайте фото чека на камеру или выберите из галереи.\n"
      "2. ИИ автоматически распознает сумму и заведение.\n"
      "3. Выберите кнопкой проект, для которого была покупка.\n\n"
      "Готово! Чек сразу попадет в очередь на возмещение.",
      parse_mode="HTML",
  )


@dp.message(F.text == "💰 Мои чеки")
async def msg_my_receipts(message: types.Message):
  status_wait = await message.answer("🔍 Проверяю ваши чеки в таблице...")
  name = message.from_user.full_name
  total, items = get_user_pending_receipts(name)

  if not items:
    await status_wait.edit_text(
        f"👤 <b>{name}</b>\n\nУ вас нет активных чеков, ожидающих возмещения. Все выплачено! 🎉",
        parse_mode="HTML",
    )
    return

  text = (
      f"👤 <b>Ваши чеки к возмещению ({name})</b>\n\n"
      f"💰 <b>Общая сумма:</b> {total:,.0f} ₸\n\n"
      "<b>Список покупок в обработке:</b>\n"
  )
  for it in items[:10]:
    text += f"• {it['amount']:,.0f} ₸ — <i>{it['project']}</i> ({it['comment']})\n"

  await status_wait.edit_text(text, parse_mode="HTML")


@dp.message(F.text == "💼 Панель выплат (Admin)")
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message):
  user_id = message.from_user.id
  if user_id not in ADMIN_IDS:
    await message.answer(
        "⛔️ <b>Доступ ограничен</b>\n\n"
        f"Ваш Telegram ID: <code>{user_id}</code>\n"
        "Добавьте его в `.env` в `ADMIN_IDS`.",
        parse_mode="HTML",
    )
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
              callback_data=f"pay_{person[:30]}",
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
  await callback.answer("Данные обновлены")


@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay_person(callback: types.CallbackQuery):
  if callback.from_user.id not in ADMIN_IDS:
    await callback.answer("Нет прав доступа", show_alert=True)
    return

  person = callback.data.replace("pay_", "")
  await callback.answer(f"Погашаю: {person}...", show_alert=False)

  ws = spreadsheet.worksheet("Операции")
  all_values = ws.get_all_values()
  status_col_idx = 8
  who_col_idx = 7

  for row_idx, row in enumerate(all_values[1:], start=2):
    if len(row) >= status_col_idx:
      who_val = row[who_col_idx - 1].strip()
      status_val = row[status_col_idx - 1].strip().lower()

      if who_val == person and status_val == "к возмещению":
        ws.update_cell(row_idx, status_col_idx, "Выплачено")

  await render_admin_menu(callback)


# --- ОБРАБОТКА ЧЕКОВ (ФОТО) ---


@dp.message(F.photo)
async def handle_photo(message: types.Message):
  status_msg = await message.answer("🔍 Распознаю чек...")

  photo = message.photo[-2] if len(message.photo) > 1 else message.photo[-1]
  file_io = io.BytesIO()
  await bot.download(photo, destination=file_io)
  image_bytes = file_io.getvalue()

  prompt = """
    Ты финансовый сканер для компании по аренде сценического оборудования.
    Изучи фото чека и верни СТРОГО чистый JSON:
    {
      "amount": итоговая сумма числом (например: 4500),
      "merchant": продавец или сервис (Яндекс Go, Magnum, Додо Пицца, АЗС и т.п.),
      "category": одна из категорий: "Такси / Логистика / ГСМ", "Питание команды", "Расходники (тейп, батарейки)", "Субаренда оборудования", "Прочее",
      "description": суть покупки (2-4 слова)
    }
    Отвечай ТОЛЬКО валидным JSON без markdown.
    """

  try:
    response = ai_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            genai_types.Part.from_bytes(
                data=image_bytes, mime_type="image/jpeg"
            ),
            prompt,
        ],
    )
    raw = response.text.strip()
    if raw.startswith("```json"):
      raw = raw[7:]
    if raw.startswith("```"):
      raw = raw[3:]
    if raw.endswith("```"):
      raw = raw[:-3]

    data = json.loads(raw.strip())
    amount = data.get("amount", 0)

    pending_receipts[message.from_user.id] = {
        "amount": amount,
        "merchant": data.get("merchant", "Неизвестно"),
        "category": data.get("category", "Прочее"),
        "description": data.get("description", ""),
        "user_name": message.from_user.full_name,
    }

    projects = get_active_projects()
    kb_buttons = [
        [
            InlineKeyboardButton(
                text=f"📌 {p}", callback_data=f"proj_{p[:25]}"
            )
        ]
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
        f"📂 {data.get('category')} — {data.get('description')}\n\n"
        "👉 <b>К какому проекту привязать?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
        parse_mode="HTML",
    )
  except Exception as e:
    await status_msg.edit_text(f"⚠️ Ошибка обработки: {e}")


@dp.callback_query(F.data.startswith("proj_"))
async def process_project_choice(callback: types.CallbackQuery):
  project_name = callback.data.replace("proj_", "")
  user_id = callback.from_user.id
  receipt = pending_receipts.get(user_id)

  if not receipt:
    await callback.answer("Данные чека устарели.", show_alert=True)
    return

  await callback.message.edit_reply_markup(reply_markup=None)
  status_update = await callback.message.answer("⏳ Записываю в таблицу...")

  try:
    ws_ops = spreadsheet.worksheet("Операции")
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    tx_id = f"TX-{int(datetime.now().timestamp()) % 100000}"

    ws_ops.append_row([
        tx_id,
        now_str,
        "Расход",
        project_name,
        receipt["category"],
        float(receipt["amount"]),
        receipt["user_name"],
        "К возмещению",
        "Чек в Telegram",
        f"{receipt['merchant']}: {receipt['description']}",
    ])

    del pending_receipts[user_id]
    await status_update.edit_text(
        "✅ <b>Расход внесен!</b>\n\n"
        f"🎯 Проект: {project_name}\n"
        f"💵 Сумма: {receipt['amount']:,.0f} ₸\n"
        f"👤 Оплатил: {receipt['user_name']}",
        parse_mode="HTML",
    )
  except Exception as e:
    await status_update.edit_text(f"⚠️ Ошибка записи: {e}")


async def handle_ping(request):
  return web.Response(text="Bot is active 24/7!")


async def main():
  # Запуск фонового веб-сервера для облака
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


if __name__ == "__main__":
  asyncio.run(main())