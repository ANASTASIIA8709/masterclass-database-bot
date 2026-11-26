"""
Бот-интеграция с внешними данными.
Три модуля — подключи нужный, остальные игнорируются если переменные не заданы.

  Модуль 1 — Google Sheets: вытащить и поискать данные в таблице
  Модуль 2 — Почта (IMAP): пересылать новые письма от нужных отправителей
  Модуль 3 — CRM / REST API: найти клиента или заявку по имени

Переменные окружения:
  BOT_TOKEN                — токен от @BotFather (обязательно)
  OWNER_ID                 — твой Telegram ID (обязательно)

  SHEET_ID                 — ID Google Sheets (из адресной строки)
  GOOGLE_CREDENTIALS_JSON  — JSON сервисного аккаунта одной строкой

  EMAIL_HOST               — IMAP-сервер (imap.yandex.ru / imap.gmail.com)
  EMAIL_USER               — логин почты
  EMAIL_PASS               — пароль или app-пароль
  EMAIL_FROM_FILTER        — пересылать только от (напр. @turop.ru), пусто = все

  CRM_API_URL              — базовый URL API твоей CRM
  CRM_API_KEY              — API-ключ или токен
"""

import asyncio, os, json, imaplib, email as email_lib
from email.header import decode_header
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
import httpx

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()


# ══════════════════════════════════════════════════════════════
# МОДУЛЬ 1 — Google Sheets
# Активируется если заданы SHEET_ID + GOOGLE_CREDENTIALS_JSON
# Команды: /sheet (первые 10 строк), /search <слово>
# ══════════════════════════════════════════════════════════════

SHEET_ID   = os.environ.get("SHEET_ID", "")
CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

def _sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

def _rows_to_text(rows, limit=10) -> str:
    return "\n".join(" | ".join(str(c) for c in r) for r in rows[:limit])

@dp.message(Command("sheet"))
async def cmd_sheet(msg: types.Message):
    """Первые 10 строк таблицы."""
    if not SHEET_ID or not CREDS_JSON:
        await msg.answer("⚙️ Задай SHEET_ID и GOOGLE_CREDENTIALS_JSON в переменных Railway.")
        return
    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="A1:F10"  # TODO: поменяй диапазон
        ).execute()
        rows = result.get("values", [])
        if not rows:
            await msg.answer("Таблица пустая или диапазон не найден.")
            return
        await msg.answer("<pre>" + _rows_to_text(rows) + "</pre>")
    except Exception as e:
        await msg.answer(f"Ошибка Sheets: {e}")

@dp.message(Command("search"))
async def cmd_search(msg: types.Message):
    """/search Иванов — найти строки по слову."""
    query = msg.text.removeprefix("/search").strip()
    if not query:
        await msg.answer("Укажи что искать: /search Иванов")
        return
    if not SHEET_ID or not CREDS_JSON:
        await msg.answer("⚙️ Задай SHEET_ID и GOOGLE_CREDENTIALS_JSON в переменных Railway.")
        return
    try:
        result = _sheets_service().spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range="A:Z"
        ).execute()
        rows  = result.get("values", [])
        found = [r for r in rows if any(query.lower() in str(c).lower() for c in r)]
        if not found:
            await msg.answer(f"По запросу «{query}» ничего не найдено.")
            return
        await msg.answer(
            f"Найдено строк: {len(found)} (показываю до 10)\n"
            "<pre>" + _rows_to_text(found) + "</pre>"
        )
    except Exception as e:
        await msg.answer(f"Ошибка Sheets: {e}")


# ══════════════════════════════════════════════════════════════
# МОДУЛЬ 2 — Почта (IMAP)
# Активируется если заданы EMAIL_USER + EMAIL_PASS
# Проверяет входящие каждые 5 минут, пересылает новые письма в Telegram
# ══════════════════════════════════════════════════════════════

EMAIL_HOST        = os.environ.get("EMAIL_HOST", "imap.yandex.ru")
EMAIL_USER        = os.environ.get("EMAIL_USER", "")
EMAIL_PASS        = os.environ.get("EMAIL_PASS", "")
EMAIL_FROM_FILTER = os.environ.get("EMAIL_FROM_FILTER", "")

def _decode_header(s) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)

def _get_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode("utf-8", errors="replace")
    return msg.get_payload(decode=True).decode("utf-8", errors="replace") if msg.get_payload(decode=True) else ""

async def _email_watcher():
    """Фоновая задача: каждые 5 минут проверяет почту и пересылает новые письма."""
    if not EMAIL_USER or not EMAIL_PASS or not OWNER_ID:
        return
    seen: set = set()
    while True:
        try:
            imap = imaplib.IMAP4_SSL(EMAIL_HOST)
            imap.login(EMAIL_USER, EMAIL_PASS)
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            for uid in data[0].split():
                if uid in seen:
                    continue
                seen.add(uid)
                _, raw = imap.fetch(uid, "(RFC822)")
                msg      = email_lib.message_from_bytes(raw[0][1])
                from_hdr = _decode_header(msg.get("From", ""))
                subject  = _decode_header(msg.get("Subject", "(без темы)"))
                if EMAIL_FROM_FILTER and EMAIL_FROM_FILTER.lower() not in from_hdr.lower():
                    continue
                body = _get_body(msg)[:600].strip()
                await bot.send_message(
                    OWNER_ID,
                    f"📧 <b>Новое письмо</b>\nОт: {from_hdr}\nТема: {subject}\n\n{body}"
                )
            imap.logout()
        except Exception as e:
            if OWNER_ID:
                await bot.send_message(OWNER_ID, f"⚠️ Ошибка почты: {e}")
        await asyncio.sleep(300)


# ══════════════════════════════════════════════════════════════
# МОДУЛЬ 3 — CRM / REST API
# Активируется если заданы CRM_API_URL + CRM_API_KEY
# Команда: /crm <имя или номер заявки>
# Пример запроса написан под U-ON — адаптируй endpoint под свою CRM
# ══════════════════════════════════════════════════════════════

CRM_API_URL = os.environ.get("CRM_API_URL", "")
CRM_API_KEY = os.environ.get("CRM_API_KEY", "")

@dp.message(Command("crm"))
async def cmd_crm(msg: types.Message):
    """/crm Иванов — найти клиента или заявку в CRM."""
    query = msg.text.removeprefix("/crm").strip()
    if not query:
        await msg.answer("Укажи имя или номер заявки: /crm Иванов")
        return
    if not CRM_API_URL or not CRM_API_KEY:
        await msg.answer("⚙️ Задай CRM_API_URL и CRM_API_KEY в переменных Railway.")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # TODO: поменяй endpoint и формат под свою CRM
            resp = await client.post(
                f"{CRM_API_URL}/request/search",
                headers={"x-token": CRM_API_KEY},
                json={"query": query, "limit": 5}
            )
            resp.raise_for_status()
            data  = resp.json()
        items = data if isinstance(data, list) else data.get("result", [])
        if not items:
            await msg.answer(f"По запросу «{query}» ничего не найдено.")
            return
        lines = []
        for item in items[:5]:
            # TODO: поменяй поля под структуру ответа своей CRM
            name   = item.get("tourist_name") or item.get("name", "—")
            status = item.get("status", "")
            ref    = item.get("id") or item.get("request_id", "")
            lines.append(f"• {name} | {status} | #{ref}")
        await msg.answer("Найдено в CRM:\n" + "\n".join(lines))
    except Exception as e:
        await msg.answer(f"Ошибка запроса к CRM: {e}")


# ══════════════════════════════════════════════════════════════
# Старт и справка
# ══════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    modules = []
    if SHEET_ID and CREDS_JSON:
        modules.append("📊 Google Sheets → /sheet, /search <i>слово</i>")
    if EMAIL_USER and EMAIL_PASS:
        modules.append("📧 Почта → уведомления приходят автоматически")
    if CRM_API_URL and CRM_API_KEY:
        modules.append("🗂 CRM → /crm <i>имя или номер</i>")

    if modules:
        text = "👋 Привет! Активные модули:\n\n" + "\n".join(modules)
    else:
        text = (
            "👋 Привет! Ни один модуль не подключён.\n\n"
            "Задай переменные в Railway → Variables:\n"
            "• Sheets: SHEET_ID + GOOGLE_CREDENTIALS_JSON\n"
            "• Почта: EMAIL_USER + EMAIL_PASS\n"
            "• CRM: CRM_API_URL + CRM_API_KEY"
        )
    await msg.answer(text)


async def main():
    if EMAIL_USER and EMAIL_PASS:
        asyncio.create_task(_email_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
