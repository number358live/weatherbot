import os
import json
import httpx
from datetime import time
from zoneinfo import ZoneInfo
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =======================
# НАСТРОЙКИ
# =======================

BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo("Europe/Moscow")

LOCATIONS = [
    ("Ельники (Мордовия)", 54.62348, 43.87309),
    ("Волхов (Ленинградская область)", 59.9258, 32.33819),
]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

CHATS_FILE = Path("chats.json")  # тут храним chat_id всех чатов


# =======================
# ХРАНЕНИЕ ЧАТОВ
# =======================

def load_chats() -> set[int]:
    if not CHATS_FILE.exists():
        return set()
    try:
        data = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(int(x) for x in data)
        return set()
    except Exception:
        return set()


def save_chats(chat_ids: set[int]) -> None:
    CHATS_FILE.write_text(json.dumps(sorted(chat_ids), ensure_ascii=False, indent=2), encoding="utf-8")


CHATS: set[int] = load_chats()


def register_chat(chat_id: int) -> bool:
    """Возвращает True, если чат был добавлен впервые."""
    if chat_id not in CHATS:
        CHATS.add(chat_id)
        save_chats(CHATS)
        return True
    return False


def unregister_chat(chat_id: int) -> bool:
    """Возвращает True, если чат был удалён."""
    if chat_id in CHATS:
        CHATS.remove(chat_id)
        save_chats(CHATS)
        return True
    return False


# =======================
# ОПИСАНИЯ ПОГОДЫ
# =======================

WMO_TEXT = {
    0: "Ясно",
    1: "Преимущественно ясно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Туман",
    51: "Морось",
    61: "Дождь",
    63: "Дождь",
    65: "Сильный дождь",
    71: "Снег",
    73: "Снег",
    75: "Сильный снег",
    80: "Ливень",
    95: "Гроза",
}


def weather_emoji(code: int) -> str:
    if code == 0:
        return "☀️"
    if code in (1, 2):
        return "⛅"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫"
    if code in (51,):
        return "🌦"
    if code in (61, 63, 65, 80):
        return "🌧"
    if code in (71, 73, 75):
        return "❄️"
    if code in (95,):
        return "⛈"
    return "🌡"


def precip_label(code: int, pop: float) -> str:
    if pop is None:
        return ""
    pop_i = int(round(pop))
    if pop_i < 10:
        return ""
    if code in (71, 73, 75):
        return f"снег ({pop_i}%)"
    if code in (61, 63, 65, 80):
        return f"дождь ({pop_i}%)"
    return f"осадки ({pop_i}%)"


# =======================
# ПОЛУЧЕНИЕ ПОГОДЫ
# =======================

async def fetch_weather(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,weathercode,precipitation_probability",
        "forecast_days": 2,
        "timezone": "Europe/Moscow",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(OPEN_METEO_URL, params=params)
        r.raise_for_status()
        return r.json()


def _target_date_from_hourly_times(times: list[str], day_index: int) -> str:
    # уникальные даты в порядке появления
    dates = []
    for t in times:
        d = t.split("T")[0]
        if not dates or dates[-1] != d:
            dates.append(d)
            if len(dates) >= 2:
                break
    if day_index == 0:
        return dates[0]
    return dates[1] if len(dates) > 1 else dates[0]


def get_hour_forecast(data, day_index: int, hour: str) -> str:
    hourly = data["hourly"]
    times = hourly["time"]

    target_date = _target_date_from_hourly_times(times, day_index)
    target_time = f"{target_date}T{hour}"

    for i, t in enumerate(times):
        if t == target_time:
            temp = hourly["temperature_2m"][i]
            code = hourly["weathercode"][i]
            pop = hourly["precipitation_probability"][i]

            emoji = weather_emoji(code)
            desc = WMO_TEXT.get(code, "Погода")

            precip = precip_label(code, pop)
            precip_part = f", {precip}" if precip else ""

            return f"{emoji} {temp:.0f}°C, {desc}{precip_part}"

    return "нет данных"


async def build_report(day_index: int) -> str:
    title = "🌤 Прогноз на сегодня\n" if day_index == 0 else "🌙 Прогноз на завтра\n"
    lines = [title]

    for name, lat, lon in LOCATIONS:
        data = await fetch_weather(lat, lon)

        morning = get_hour_forecast(data, day_index, "08:00")
        day = get_hour_forecast(data, day_index, "14:00")
        evening = get_hour_forecast(data, day_index, "20:00")

        lines.append(
            f"📍 {name}\n"
            f"• Утро:   {morning}\n"
            f"• День:   {day}\n"
            f"• Вечер:  {evening}\n"
        )

    return "\n".join(lines).strip()


# =======================
# РАССЫЛКИ ВО ВСЕ ЧАТЫ
# =======================

async def broadcast(app: Application, text: str) -> None:
    # чтобы не упало, если какой-то чат недоступен — пробуем всем по очереди
    dead = set()
    for chat_id in list(CHATS):
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_notification=True,
            )
        except Exception:
            # чат мог удалить бота/закрыть доступ — уберём из списка
            dead.add(chat_id)

    if dead:
        for cid in dead:
            unregister_chat(cid)


async def send_today(context: ContextTypes.DEFAULT_TYPE):
    if not CHATS:
        return
    text = await build_report(day_index=0)
    await broadcast(context.application, text)


async def send_tomorrow(context: ContextTypes.DEFAULT_TYPE):
    if not CHATS:
        return
    text = await build_report(day_index=1)
    await broadcast(context.application, text)


# =======================
# КОМАНДЫ
# =======================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если бот написал в группе — зарегистрируем чат тоже
    if update.effective_chat:
        register_chat(update.effective_chat.id)

    await update.message.reply_text(
        "Я бот погоды.\n"
        "Команды: /today /tomorrow /now /chatid /stop\n"
        "Если меня добавить в группу — я начну рассылку автоматически (06:00 и 19:00).",
        disable_notification=True
    )


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    await update.message.reply_text(
        f"chat_id = {update.effective_chat.id}",
        disable_notification=True
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    text = await build_report(day_index=0)
    await update.message.reply_text(text, disable_notification=True)


async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_chat(update.effective_chat.id)
    text = await build_report(day_index=1)
    await update.message.reply_text(text, disable_notification=True)


async def now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await today_cmd(update, context)


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    removed = unregister_chat(update.effective_chat.id)
    msg = "✅ Рассылка для этого чата отключена." if removed else "ℹ️ Этот чат и так не был в списке рассылки."
    await update.message.reply_text(msg, disable_notification=True)


# =======================
# АВТО-РЕГИСТРАЦИЯ ПРИ ДОБАВЛЕНИИ В ЧАТ
# =======================

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Когда в чат добавляют новых участников — проверим, не добавили ли бота."""
    if not update.message or not update.message.new_chat_members:
        return

    me = context.bot.id  # id текущего бота
    for member in update.message.new_chat_members:
        if member.id == me:
            added = register_chat(update.effective_chat.id)
            if added:
                await update.message.reply_text(
                    "✅ Принято! Буду присылать погоду сюда:\n"
                    "• 06:00 — прогноз на сегодня\n"
                    "• 19:00 — прогноз на завтра\n"
                    "Команды: /today /tomorrow /stop",
                    disable_notification=True
                )
            break


# =======================
# ЗАПУСК
# =======================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("now", now_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # авто-детект добавления бота в чат
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # 06:00 — прогноз на сегодня
    app.job_queue.run_daily(send_today, time=time(6, 0, tzinfo=TZ))

    # 19:00 — прогноз на завтра
    app.job_queue.run_daily(send_tomorrow, time=time(19, 0, tzinfo=TZ))

    print(f"✅ Бот запущен. Чатов в рассылке: {len(CHATS)} (файл: {CHATS_FILE})")
    app.run_polling()


if __name__ == "__main__":
    main()
