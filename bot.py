import os
import json
import asyncio
from datetime import date, datetime
from dateutil.parser import isoparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

BOT_TOKEN = os.getenv("BOT_TOKEN")
TP_TOKEN = os.getenv("TP_TOKEN")

# По умолчанию: SVO -> HKT, RUB, 1 взрослый, эконом
DEFAULT_ORIGIN = "SVO"
DEFAULT_DEST = "HKT"

STATE_FILE = "state.json"

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
# Документация: prices_for_dates поддерживает departure_at YYYY-MM или YYYY-MM-DD, currency по умолчанию RUB
# (мы явно задаём currency=RUB)  :contentReference[oaicite:4]{index=4}

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def aviasales_deeplink(origin: str, dest: str, depart: str, ret: str | None) -> str:
    # Deeplink на форму поиска Aviasales (параметры origin_iata/destination_iata/depart_date/return_date)
    # Подобные параметры используются в ссылках белой метки/формы поиска :contentReference[oaicite:5]{index=5}
    base = "https://search.aviasales.com/flights/"
    # Часть Aviasales принимает /SVOHKT1002 (есть разные форматы),
    # но самый понятный и стабильный для людей — через query string:
    if ret:
        return (f"https://search.aviasales.com/flights/?origin_iata={origin}&destination_iata={dest}"
                f"&depart_date={depart}&return_date={ret}&adults=1&children=0&infants=0&trip_class=0&one_way=false&locale=ru")
    return (f"https://search.aviasales.com/flights/?origin_iata={origin}&destination_iata={dest}"
            f"&depart_date={depart}&adults=1&children=0&infants=0&trip_class=0&one_way=true&locale=ru")

async def fetch_prices(session: aiohttp.ClientSession, origin: str, dest: str, departure_at: str, return_at: str | None,
                       direct: bool, one_way: bool, limit: int = 100) -> list[dict]:
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": departure_at,     # YYYY-MM-DD (берём точный день)
        "one_way": "true" if one_way else "false",
        "direct": "true" if direct else "false",
        "sorting": "price",
        "limit": str(limit),
        "currency": "RUB",
        "token": TP_TOKEN,
        "market": "ru"
    }
    if return_at and not one_way:
        params["return_at"] = return_at

    async with session.get(API_URL, params=params, timeout=25) as r:
        data = await r.json()
        if not data.get("success"):
            return []
        # В data обычно список офферов с price, departure_at, return_at, transfers и т.п. :contentReference[oaicite:6]{index=6}
        return data.get("data", [])

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def date_range(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur = cur.fromordinal(cur.toordinal() + 1)

async def checker_loop(bot: Bot):
    while True:
        state = load_state()

        # Пробегаем по всем чатам, которые настроили бота
        for chat_id, cfg in state.items():
            try:
                origin = cfg.get("origin", DEFAULT_ORIGIN)
                dest = cfg.get("dest", DEFAULT_DEST)
                date_from = cfg.get("date_from")  # YYYY-MM-DD
                date_to = cfg.get("date_to")
                max_price = cfg.get("max_price")  # int
                direct = cfg.get("direct", False)
                one_way = cfg.get("one_way", True)
                enabled = cfg.get("enabled", False)
                last_sent = cfg.get("last_sent", {})  # key -> price

                if not enabled or not date_from or not date_to or not max_price:
                    continue

                df = parse_ymd(date_from)
                dt = parse_ymd(date_to)

                async with aiohttp.ClientSession() as session:
                    for d in date_range(df, dt):
                        depart = d.strftime("%Y-%m-%d")
                        offers = await fetch_prices(session, origin, dest, depart, None, direct=direct, one_way=True)

                        if not offers:
                            continue

                        # Берём самый дешёвый по этому дню
                        best = min(offers, key=lambda x: x.get("price", 10**18))
                        price = int(best.get("price", 10**18))
                        transfers = best.get("transfers", None)

                        if price <= int(max_price):
                            key = f"{origin}-{dest}-{depart}"
                            prev = last_sent.get(key)

                            # антиспам: отправляем, если раньше не отправляли или стало дешевле
                            if prev is None or price < prev:
                                last_sent[key] = price
                                cfg["last_sent"] = last_sent
                                state[str(chat_id)] = cfg
                                save_state(state)

                                link = ("https://search.aviasales.com" + str(best.get("link")).lstrip("/")) if best.get("link") else aviasales_deeplink(origin, dest, depart, None)
                                kb = InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(text="Открыть в Aviasales", url=link)
                                ]])

                                text = (
                                    f"🔥 Нашёл дешевле твоего лимита!\n\n"
                                    f"Маршрут: {origin} → {dest}\n"
                                    f"Дата вылета: {depart}\n"
                                    f"Цена: {price:,} ₽\n"
                                )
                                if transfers is not None:
                                    text += f"Пересадки: {transfers}\n"

                                await bot.send_message(chat_id=int(chat_id), text=text, reply_markup=kb)

                await asyncio.sleep(1)

            except Exception:
                # Ошибки по одному чату не валят весь цикл
                continue

        # Проверка раз в 60 минут
        await asyncio.sleep(60 * 60)

async def main():
    if not BOT_TOKEN or not TP_TOKEN:
        raise RuntimeError("Нужно задать переменные окружения BOT_TOKEN и TP_TOKEN")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(m: Message):
        state = load_state()
        chat_id = str(m.chat.id)
        if chat_id not in state:
            state[chat_id] = {
                "origin": DEFAULT_ORIGIN,
                "dest": DEFAULT_DEST,
                "date_from": None,
                "date_to": None,
                "max_price": None,
                "direct": False,
                "one_way": True,
                "enabled": False,
                "last_sent": {}
            }
            save_state(state)

        await m.answer(
            "Я бот для отслеживания дешёвых билетов SVO → HKT.\n\n"
            "Команды:\n"
            "/setdates YYYY-MM-DD YYYY-MM-DD — диапазон дат вылета\n"
            "/setprice 60000 — лимит в рублях\n"
            "/direct on|off — только прямые или любые\n"
            "/on — включить мониторинг\n"
            "/off — выключить\n"
            "/status — показать настройки"
        )

    @dp.message(Command("setdates"))
    async def setdates(m: Message):
        parts = m.text.split()
        if len(parts) != 3:
            return await m.answer("Пример: /setdates 2026-02-01 2026-03-31")
        try:
            _ = parse_ymd(parts[1]); _ = parse_ymd(parts[2])
        except Exception:
            return await m.answer("Формат дат должен быть YYYY-MM-DD. Пример: 2026-02-01")

        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        cfg["date_from"] = parts[1]
        cfg["date_to"] = parts[2]
        state[str(m.chat.id)] = cfg
        save_state(state)
        await m.answer(f"Ок. Диапазон дат: {parts[1]} — {parts[2]}")

    @dp.message(Command("setprice"))
    async def setprice(m: Message):
        parts = m.text.split()
        if len(parts) != 2:
            return await m.answer("Пример: /setprice 60000")
        try:
            price = int(parts[1])
            if price <= 0:
                raise ValueError()
        except Exception:
            return await m.answer("Цена должна быть числом. Пример: /setprice 60000")

        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        cfg["max_price"] = price
        state[str(m.chat.id)] = cfg
        save_state(state)
        await m.answer(f"Ок. Лимит: {price:,} ₽")

    @dp.message(Command("direct"))
    async def direct(m: Message):
        parts = m.text.split()
        if len(parts) != 2 or parts[1] not in ("on", "off"):
            return await m.answer("Пример: /direct on (или /direct off)")
        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        cfg["direct"] = (parts[1] == "on")
        state[str(m.chat.id)] = cfg
        save_state(state)
        await m.answer("Ок. Прямые: " + ("включено" if cfg["direct"] else "выключено"))

    @dp.message(Command("on"))
    async def on(m: Message):
        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        cfg["enabled"] = True
        state[str(m.chat.id)] = cfg
        save_state(state)
        await m.answer("✅ Мониторинг включён. Проверяю раз в час.")

    @dp.message(Command("off"))
    async def off(m: Message):
        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        cfg["enabled"] = False
        state[str(m.chat.id)] = cfg
        save_state(state)
        await m.answer("⏸ Мониторинг выключен.")

    @dp.message(Command("status"))
    async def status(m: Message):
        state = load_state()
        cfg = state.get(str(m.chat.id), {})
        await m.answer(
            "Текущие настройки:\n"
            f"Маршрут: {cfg.get('origin', DEFAULT_ORIGIN)} → {cfg.get('dest', DEFAULT_DEST)}\n"
            f"Даты: {cfg.get('date_from')} — {cfg.get('date_to')}\n"
            f"Лимит: {cfg.get('max_price')} ₽\n"
            f"Прямые: {cfg.get('direct')}\n"
            f"Включено: {cfg.get('enabled')}\n"
        )

    # Запускаем проверку в фоне
    asyncio.create_task(checker_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
