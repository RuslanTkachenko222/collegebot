"""Бот-напоминалка о звонках и парах. Без внешних библиотек — только стандартный Python."""
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------- настройки
TOKEN = os.getenv("BOT_TOKEN")
# Сдвиг от UTC в часах: Москва = 3, Екатеринбург = 5, Новосибирск = 7 и т.д.
TZ = timezone(timedelta(hours=int(os.getenv("BOT_UTC_OFFSET", "3"))))
# За сколько минут предупреждать о начале пары
WARN_MINUTES = int(os.getenv("WARN_MINUTES", "5"))
DB_PATH = "subscribers.db"

# Расписание: 0 = понедельник ... 6 = воскресенье -> (название, начало, конец)
SCHEDULE = {
    0: [
        ("Разговоры о важном", "08:30", "09:00"),
        ("1 пара", "09:10", "10:40"),
        ("2 пара", "10:50", "12:20"),
        ("3 пара", "12:45", "14:15"),
        ("4 пара", "14:20", "15:50"),
    ],
    # 1: [...],  # вторник
    # 2: [...],  # среда
}

DAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

logging.basicConfig(level=logging.INFO)
API = f"https://api.telegram.org/bot{TOKEN}"


# ---------------------------------------------------------------- Telegram API
def api(method: str, http_timeout: int = 15, **params) -> dict:
    req = urllib.request.Request(
        f"{API}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False, "error_code": e.code}


def send(chat_id: int, text: str) -> None:
    res = api("sendMessage", chat_id=chat_id, text=text)
    if not res.get("ok") and res.get("error_code") == 403:
        remove_subscriber(chat_id)  # пользователь заблокировал бота


# ---------------------------------------------------------------- подписчики
def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)")


def add_subscriber(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))


def remove_subscriber(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))


def get_subscribers() -> list:
    with sqlite3.connect(DB_PATH) as con:
        return [row[0] for row in con.execute("SELECT chat_id FROM subscribers")]


# ---------------------------------------------------------------- расписание
def lessons_for(day: date) -> list:
    result = []
    for name, start, end in SCHEDULE.get(day.weekday(), []):
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        result.append((
            name,
            datetime(day.year, day.month, day.day, sh, sm, tzinfo=TZ),
            datetime(day.year, day.month, day.day, eh, em, tzinfo=TZ),
        ))
    return result


def events_for(day: date) -> list:
    """Все уведомления за день: предупреждение, звонок на пару, звонок с пары."""
    lessons = lessons_for(day)
    events = []
    for i, (name, start, end) in enumerate(lessons):
        s, e = start.strftime("%H:%M"), end.strftime("%H:%M")
        events.append((
            start - timedelta(minutes=WARN_MINUTES),
            f"⏰ Через {WARN_MINUTES} мин. начнётся: {name} ({s}–{e})",
        ))
        events.append((start, f"🔔 Звонок на {name}. Идёт до {e}."))
        if i + 1 < len(lessons):
            nxt_name, nxt_start, _ = lessons[i + 1]
            end_text = (
                f"🔔 Звонок с {name}. Перемена.\n"
                f"Следующее: {nxt_name} в {nxt_start.strftime('%H:%M')}."
            )
        else:
            end_text = f"🔔 Звонок с {name}. На сегодня всё, можно отдыхать! 🎉"
        events.append((end, end_text))
    return sorted(events)


def format_day(day: date) -> str:
    lessons = lessons_for(day)
    title = DAY_NAMES[day.weekday()].capitalize()
    if not lessons:
        return f"{title}: расписание не задано (или выходной)."
    lines = [f"📅 {title}:"]
    for name, start, end in lessons:
        lines.append(f"• {name}: {start.strftime('%H:%M')}–{end.strftime('%H:%M')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- рассылка (фоновый поток)
def scheduler() -> None:
    sent = set()
    current_day = datetime.now(TZ).date()
    while True:
        try:
            now = datetime.now(TZ)
            if now.date() != current_day:
                current_day = now.date()
                sent.clear()
            for when, text in events_for(now.date()):
                key = (when.isoformat(), text)
                if key not in sent and when <= now < when + timedelta(minutes=2):
                    sent.add(key)
                    for chat_id in get_subscribers():
                        send(chat_id, text)
                        time.sleep(0.05)
        except Exception:
            logging.exception("Ошибка в планировщике")
        time.sleep(10)


# ---------------------------------------------------------------- команды
def handle(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    parts = msg["text"].strip().split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    now = datetime.now(TZ)

    if cmd == "/start":
        add_subscriber(chat_id)
        send(chat_id,
             "Привет! Теперь я буду присылать уведомления о звонках и начале пар "
             f"(предупреждаю за {WARN_MINUTES} мин.).\n\n"
             "/today — расписание на сегодня\n"
             "/next — что дальше\n"
             "/stop — отключить уведомления")
    elif cmd == "/stop":
        remove_subscriber(chat_id)
        send(chat_id, "Уведомления отключены. Чтобы включить снова, отправьте /start.")
    elif cmd == "/today":
        send(chat_id, format_day(now.date()))
    elif cmd == "/next":
        for name, start, end in lessons_for(now.date()):
            if start <= now < end:
                send(chat_id, f"Сейчас идёт: {name} (до {end.strftime('%H:%M')}).")
                return
            if now < start:
                send(chat_id, f"Ближайшее: {name} в {start.strftime('%H:%M')}.")
                return
        send(chat_id, "На сегодня занятий больше нет.")


# ---------------------------------------------------------------- запуск
def main() -> None:
    if not TOKEN:
        raise SystemExit("Задайте токен: export BOT_TOKEN='123456:ABC...'")
    db_init()

    # Меню команд в Telegram (кнопка «Меню» рядом с полем ввода)
    api("setMyCommands", commands=[
        {"command": "today", "description": "Расписание на сегодня"},
        {"command": "next", "description": "Что дальше"},
        {"command": "start", "description": "Включить уведомления"},
        {"command": "stop", "description": "Отключить уведомления"},
    ])

    threading.Thread(target=scheduler, daemon=True).start()
    print("Бот запущен. Остановить: Ctrl+C")

    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            res = api("getUpdates", http_timeout=40, **params)
            if not res.get("ok"):
                logging.error("Ошибка Telegram: %s", res)
                time.sleep(5)
                continue
            for upd in res["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg and msg.get("text"):
                    handle(msg)
        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception("Ошибка соединения, повтор через 5 секунд")
            time.sleep(5)


if __name__ == "__main__":
    main()
