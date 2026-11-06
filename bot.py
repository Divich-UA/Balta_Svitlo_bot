"""
Бот графіків відключень ДТЕК Одеські електромережі.

Працює всередині GitHub Actions без сервера:
  * один запуск триває ~5 год 40 хв, потім його змінює наступний;
  * кожні CHECK_EVERY_SEC секунд перевіряє сайт ДТЕК;
  * одразу відповідає людям у Telegram: /start -> вибір черги кнопками;
  * шле графік підписникам і в основний чат (TG_CHAT_ID, черга GROUP),
    коли змінюється дата, з'являється графік на завтра або графік змінюється;
  * з 00:00 до 07:00 повідомлення приходять без звуку.

Стан (підписники, останні графіки) зберігається в репозиторії
у зашифрованому файлі state.enc, бо репозиторій публічний.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- налаштування
URL = "https://www.dtek-oem.com.ua/ua/shutdowns"
TZ = ZoneInfo("Europe/Kyiv")
TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
MAIN_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()   # основний чат (група)
MAIN_GROUP = os.environ.get("GROUP", "2.1").strip()        # черга основного чату
FORCE = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
NEW_DAY_HOUR = int(os.environ.get("NEW_DAY_HOUR", "0") or 0)
CHECK_EVERY = int(os.environ.get("CHECK_EVERY_SEC", "180") or 180)   # 3 хв
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "340") or 340)       # 5 год 40 хв
QUIET_FROM, QUIET_TO = 0, 7          # тихі години (без звуку)
FAIL_ALERT_MIN = 60                  # попередження, якщо сайт недоступний годину
STATE_FILE = "state.enc"
OLD_STATE_FILE = "state.json"
DEFAULT_GROUPS = [f"{a}.{b}" for a in range(1, 7) for b in (1, 2)]

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
STATUS_MAP = {
    "yes": ("on", "on"),
    "no": ("off", "off"),
    "first": ("off", "on"),
    "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"),
    "msecond": ("on", "maybe"),
}


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- сайт ДТЕК
def extract_fact_from_html(html: str):
    """Запасний спосіб: шукає 'DisconSchedule.fact = {...}' прямо в HTML."""
    m = re.search(r"DisconSchedule\.fact\s*=\s*", html)
    if not m:
        return None
    i = html.find("{", m.end())
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:j + 1])
                except json.JSONDecodeError:
                    return None
    return None


JS_GET_FACT = """() => {
  try {
    const f = window.DisconSchedule && window.DisconSchedule.fact;
    if (f && f.data) return JSON.parse(JSON.stringify(f));
  } catch (e) {}
  return null;
}"""


class Fetcher:
    """Тримає один браузер відкритим і щоразу перезавантажує сторінку ДТЕК."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = None
        self.page = None
        self._open()

    def _open(self):
        self.close_browser()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = self.browser.new_context(
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self.page = ctx.new_page()

    def close_browser(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        self.browser = None

    def close(self):
        self.close_browser()
        try:
            self.pw.stop()
        except Exception:  # noqa: BLE001
            pass

    def _save_debug(self, attempt: int):
        os.makedirs("debug", exist_ok=True)
        try:
            self.page.screenshot(path=f"debug/attempt{attempt}.png", full_page=True)
            html = self.page.content()
            with open(f"debug/attempt{attempt}.html", "w", encoding="utf-8") as f:
                f.write(html)
            log(f"  URL: {self.page.url}; заголовок: {self.page.title()!r}; "
                f"HTML {len(html)} символів")
        except Exception as e:  # noqa: BLE001
            log(f"  (діагностика не збереглась: {e})")

    def get(self):
        last_err = None
        for attempt in range(1, 4):
            try:
                self.page.goto(URL, wait_until="load", timeout=60000)
                for _ in range(20):
                    fact = self.page.evaluate(JS_GET_FACT)
                    if fact:
                        return fact
                    fact = extract_fact_from_html(self.page.content())
                    if fact and fact.get("data"):
                        return fact
                    self.page.wait_for_timeout(2000)
                raise RuntimeError("на сторінці немає DisconSchedule.fact")
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"Спроба {attempt} не вдалась: {e}")
                self._save_debug(attempt)
                if attempt == 2:
                    try:
                        self._open()  # на третю спробу — свіжий браузер
                    except Exception as e2:  # noqa: BLE001
                        log(f"  не вдалося перезапустити браузер: {e2}")
                time.sleep(5 * attempt)
        raise RuntimeError(f"Не вдалося отримати графік: {last_err}")


# ---------------------------------------------------------------- обробка графіка
def day_slots(hours: dict) -> list:
    slots = []
    for h in range(1, 25):
        st = str(hours.get(str(h), "yes")).lower()
        slots.extend(STATUS_MAP.get(st, ("on", "on")))
    return slots


def signature(slots: list) -> str:
    return "".join({"on": "1", "off": "0"}.get(x, "m") for x in slots)


def slot_time(i: int) -> str:
    return f"{i // 2:02d}:{(i % 2) * 30:02d}"


def ranges(slots: list):
    out, i = [], 0
    while i < len(slots):
        s = slots[i]
        if s == "on":
            i += 1
            continue
        j = i
        while j < len(slots) and slots[j] == s:
            j += 1
        out.append((s, slot_time(i), slot_time(j)))
        i = j
    return out


def group_days(fact: dict, group: str) -> dict:
    """{ 'YYYY-MM-DD': [48 слотів] } для черги, від сьогодні і далі."""
    key = f"GPV{group}"
    today = datetime.now(TZ).date()
    days = {}
    for ts, groups in (fact.get("data") or {}).items():
        try:
            d = datetime.fromtimestamp(int(ts), TZ).date()
        except (ValueError, TypeError):
            continue
        if d < today or not isinstance(groups, dict) or key not in groups:
            continue
        days[d.isoformat()] = day_slots(groups[key])
    return dict(sorted(days.items()))


def groups_in_fact(fact: dict) -> list:
    found = set()
    for groups in (fact.get("data") or {}).values():
        if isinstance(groups, dict):
            for k in groups:
                m = re.fullmatch(r"GPV(\d+\.\d+)", str(k))
                if m:
                    found.add(m.group(1))
    return sorted(found, key=lambda g: tuple(int(x) for x in g.split(".")))


def fmt_hours(n_slots: int) -> str:
    h, half = divmod(n_slots, 2)
    return f"{h} год" + (" 30 хв" if half else "")


def format_day(date_iso: str, slots: list, mark: str) -> str:
    d = datetime.fromisoformat(date_iso).date()
    delta = (d - datetime.now(TZ).date()).days
    label = {0: "Сьогодні", 1: "Завтра"}.get(delta, "")
    title = (f"{label}, " if label else "") + f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"
    lines = [f"📅 <b>{title}</b>{mark}"]
    rs = ranges(slots)
    if not rs:
        lines.append("🟢 Відключень не заплановано")
    else:
        for kind, a, b in rs:
            lines.append(f"🔴 {a}–{b}" if kind == "off" else f"🟡 {a}–{b} <i>(можливо)</i>")
        off = slots.count("off")
        if off:
            lines.append(f"⏱ Без світла: {fmt_hours(off)}")
    return "\n".join(lines)


def build_message(group: str, days: dict, changed: dict, update_str: str) -> str:
    header = f"💡 Група {group} · ДТЕК Одеські електромережі"
    if any(v == "changed" for v in changed.values()):
        header += "\n⚠️ <b>Зміна графіку</b>"
    parts = [header]
    for date_iso, slots in days.items():
        mark = "  ✏️ змінено" if changed.get(date_iso) == "changed" else ""
        parts.append(format_day(date_iso, slots, mark))
    if update_str:
        parts.append(f"🕒 Оновлено ДТЕК: {update_str}")
    return "\n\n".join(parts)


def current_message(group: str, fact) -> str:
    if not fact:
        return "⏳ Не вдалося отримати графік із сайту ДТЕК. Спробуйте трохи пізніше."
    days = group_days(fact, group)
    if not days:
        return f"💡 Група {group}\n\nНа сайті ДТЕК зараз немає графіка для цієї черги."
    return build_message(group, days, {}, str(fact.get("update") or ""))


# ---------------------------------------------------------------- telegram
def quiet_now() -> bool:
    h = datetime.now(TZ).hour
    return QUIET_FROM <= h < QUIET_TO


def tg(method: str, _timeout: int = 30, **params) -> dict:
    if not TG_TOKEN:
        return {"ok": False, "description": "no token"}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error_code": e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": str(e)}


def send(chat_id, text: str, keyboard=None) -> dict:
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": quiet_now(),
    }
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    if not TG_TOKEN:
        print(f"[→ {chat_id}{' 🔕' if params['disable_notification'] else ''}]\n{text}\n")
        return {"ok": True}
    res = tg("sendMessage", **params)
    if not res.get("ok"):
        log(f"Не надіслано в {chat_id}: {res}")
    time.sleep(0.05)
    return res


def groups_keyboard(groups: list) -> list:
    groups = groups or DEFAULT_GROUPS
    btns = [{"text": g, "callback_data": f"g:{g}"} for g in groups]
    return [btns[i:i + 4] for i in range(0, len(btns), 4)]


# ---------------------------------------------------------------- стан
def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("dtek-state:" + TG_TOKEN).encode()).digest()
    )
    return Fernet(key)


def load_state() -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "rb") as f:
                state = json.loads(_fernet().decrypt(f.read()))
        except Exception as e:  # noqa: BLE001
            log(f"Не вдалося прочитати стан ({e}), починаю з нуля")
            state = {}
    elif os.path.exists(OLD_STATE_FILE):  # перехід зі старої версії
        try:
            with open(OLD_STATE_FILE, encoding="utf-8") as f:
                old = json.load(f)
            state = {"groups": {MAIN_GROUP: {k: old[k] for k in ("days", "last_date") if k in old}}}
        except Exception:  # noqa: BLE001
            state = {}
    state.setdefault("groups", {})
    state.setdefault("subs", {})
    return state


def save_state(state: dict):
    data = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
    with open(STATE_FILE, "wb") as f:
        f.write(_fernet().encrypt(data))


def git_save():
    """Комітить стан у репозиторій (лише всередині GitHub Actions)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    def run(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    run("git", "add", STATE_FILE)
    run("git", "rm", "--cached", "-q", "--ignore-unmatch", OLD_STATE_FILE)
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    run("git", "commit", "-q", "-m", "стан бота")
    for _ in range(3):
        run("git", "pull", "--rebase", "-q")
        if run("git", "push", "-q").returncode == 0:
            return
        time.sleep(3)
    log("Не вдалося запушити стан")


# ---------------------------------------------------------------- логіка розсилки
def targets(state: dict) -> dict:
    """черга -> список чатів."""
    out = {}
    for chat, g in state["subs"].items():
        out.setdefault(g, []).append(chat)
    if MAIN_CHAT_ID:
        out.setdefault(MAIN_GROUP, [])
        if MAIN_CHAT_ID not in out[MAIN_GROUP]:
            out[MAIN_GROUP].append(MAIN_CHAT_ID)
    return out


def remember_group(state: dict, group: str, fact):
    """Записує поточний графік черги як уже відомий (щоб не слати двічі)."""
    days = group_days(fact, group) if fact else {}
    if not days:
        return
    gs = state["groups"].setdefault(group, {})
    gs["days"] = {d: signature(s) for d, s in days.items()}
    today = datetime.now(TZ).date().isoformat()
    if today in days:
        gs["last_date"] = today


def check_schedules(fact: dict, state: dict, force: bool = False) -> bool:
    """Розсилає графіки, де щось змінилось. Повертає True, якщо стан змінився."""
    dirty = False
    now = datetime.now(TZ)
    today = now.date().isoformat()
    update_str = str(fact.get("update") or "")
    known = groups_in_fact(fact)
    if known and known != state.get("known_groups"):
        state["known_groups"] = known
        dirty = True

    for group, chats in targets(state).items():
        days = group_days(fact, group)
        if not days:
            continue
        gs = state["groups"].setdefault(group, {})
        old = gs.get("days", {})
        changed = {}
        for d, slots in days.items():
            sig = signature(slots)
            if d not in old:
                changed[d] = "new"
            elif old[d] != sig:
                changed[d] = "changed"
        new_day = gs.get("last_date") != today and today in days and now.hour >= NEW_DAY_HOUR

        if changed or new_day or force:
            msg = build_message(group, days, changed, update_str)
            for chat in list(chats):
                res = send(chat, msg)
                new_id = (res.get("parameters") or {}).get("migrate_to_chat_id")
                if new_id and chat in state["subs"]:   # група стала супергрупою
                    state["subs"][str(new_id)] = state["subs"].pop(chat)
                    send(str(new_id), msg)
                elif res.get("error_code") == 403 and chat in state["subs"]:
                    del state["subs"][chat]  # бота заблокували або видалили
            if today in days:
                gs["last_date"] = today
            dirty = True

        new_sigs = {d: signature(s) for d, s in days.items()}
        if new_sigs != old:
            gs["days"] = new_sigs
            dirty = True
    return dirty


HELP = (
    "Я надсилаю графіки відключень світла <b>ДТЕК Одеські електромережі</b>.\n\n"
    "/start — вибрати або змінити чергу\n"
    "/now — показати графік зараз\n"
    "/stop — відписатися\n\n"
    "Графік приходить, коли настає новий день, коли з'являється графік на завтра "
    "і коли ДТЕК його змінює. З 00:00 до 07:00 — без звуку.\n"
    "Мене можна додати і в груповий чат — там чергу вибирають адміни.\n"
    "Свою чергу можна дізнатися на сайті ДТЕК за адресою: " + URL
)

BOT_USERNAME = ""   # заповнюється при старті (getMe)
OUR_COMMANDS = ("/start", "/change", "/now", "/stop", "/help")


def is_admin(chat_id, user_id) -> bool:
    res = tg("getChatMember", chat_id=chat_id, user_id=user_id)
    return (res.get("result") or {}).get("status") in ("creator", "administrator")


def chat_word(chat: dict) -> str:
    return "цього чату" if chat.get("type") != "private" else "ваша"


def handle_update(u: dict, state: dict, fact) -> bool:
    """Обробляє повідомлення/кнопку/додавання в чат. True — змінились підписки."""
    kb = groups_keyboard(state.get("known_groups"))

    # --- бота додали в групу або видалили з неї
    if "my_chat_member" in u:
        mc = u["my_chat_member"]
        chat = mc.get("chat") or {}
        chat_id = str(chat.get("id"))
        status = (mc.get("new_chat_member") or {}).get("status")
        if chat.get("type") not in ("group", "supergroup") or chat_id == MAIN_CHAT_ID:
            return False
        if status in ("left", "kicked"):
            return state["subs"].pop(chat_id, None) is not None
        old = (mc.get("old_chat_member") or {}).get("status")
        if status in ("member", "administrator") and old in ("left", "kicked", None):
            send(chat_id, "👋 Привіт! Я надсилатиму в цей чат графіки відключень "
                          "ДТЕК Одеські електромережі.\nАдміни, оберіть чергу 👇", kb)
        return False

    # --- натиснули кнопку з чергою
    if "callback_query" in u:
        cq = u["callback_query"]
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        data = cq.get("data") or ""
        chat_id = str(chat.get("id"))
        if not data.startswith("g:") or not chat:
            tg("answerCallbackQuery", callback_query_id=cq["id"])
            return False
        if chat_id == MAIN_CHAT_ID:
            tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True,
               text=f"У цьому чаті черга {MAIN_GROUP} налаштована на GitHub.")
            return False
        if chat.get("type") != "private" and not is_admin(chat_id, cq["from"]["id"]):
            tg("answerCallbackQuery", callback_query_id=cq["id"], show_alert=True,
               text="Змінювати чергу в чаті можуть лише адміни.")
            return False
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        group = data[2:]
        others = [c for c in targets(state).get(group, []) if c != chat_id]
        state["subs"][chat_id] = group
        if not others:
            remember_group(state, group, fact)
        tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
           text=f"✅ Черга {chat_word(chat)}: <b>{group}</b>\nГрафіки приходитимуть автоматично.",
           parse_mode="HTML")
        send(chat_id, current_message(group, fact))
        return True

    # --- команди
    m = u.get("message")
    if not m:
        return False
    chat = m.get("chat") or {}
    chat_id = str(chat.get("id"))
    private = chat.get("type") == "private"
    text = (m.get("text") or "").strip()
    first = text.split()[0] if text else ""
    cmd, _, target = first.partition("@")
    cmd = cmd.lower()
    if not private:
        # у групі відповідаємо лише на наші команди, адресовані нам
        if cmd not in OUR_COMMANDS:
            return False
        if target and BOT_USERNAME and target.lower() != BOT_USERNAME.lower():
            return False
    elif not cmd.startswith("/"):
        cmd = ""

    is_main = chat_id == MAIN_CHAT_ID
    sub = MAIN_GROUP if is_main else state["subs"].get(chat_id)

    if cmd in ("/start", "/change"):
        if is_main:
            send(chat_id, f"У цьому чаті налаштовано чергу <b>{MAIN_GROUP}</b>. /now — графік зараз.")
        else:
            intro = f"Зараз черга {chat_word(chat)}: <b>{sub}</b>. " if sub else ""
            who = "" if private else " (вибирають адміни)"
            send(chat_id, intro + f"Оберіть чергу{who} 👇", kb)
    elif cmd == "/now":
        if sub:
            send(chat_id, current_message(sub, fact))
        else:
            send(chat_id, "Спершу оберіть чергу 👇", kb)
    elif cmd == "/stop":
        if is_main:
            send(chat_id, "Цей чат налаштовано на GitHub, вимкнути його можна там.")
        elif not private and not is_admin(chat_id, (m.get("from") or {}).get("id")) \
                and (m.get("sender_chat") or {}).get("id") != chat.get("id"):
            send(chat_id, "Відписати чат можуть лише адміни.")
        elif state["subs"].pop(chat_id, None):
            send(chat_id, "🔕 Підписку вимкнено. Щоб повернутися — /start")
            return True
        else:
            send(chat_id, "Підписки й так немає. Щоб підписатися — /start")
    else:
        send(chat_id, HELP)
    return False


def handle_failure(state: dict, err: Exception) -> bool:
    log(err)
    now = int(time.time())
    if not state.get("fail_since"):
        state["fail_since"] = now
        return True
    if not state.get("fail_alerted") and now - state["fail_since"] > FAIL_ALERT_MIN * 60:
        if MAIN_CHAT_ID:
            send(MAIN_CHAT_ID, "⚠️ Бот уже понад годину не може дістати графік з сайту ДТЕК. "
                               "Перевірте вручну: " + URL)
        state["fail_alerted"] = True
        return True
    return False


# ---------------------------------------------------------------- головний цикл
def main():
    global BOT_USERNAME
    state = load_state()
    end = time.time() + RUN_MINUTES * 60
    BOT_USERNAME = (tg("getMe").get("result") or {}).get("username", "")
    tg("setMyCommands", commands=[
        {"command": "start", "description": "Вибрати чергу"},
        {"command": "now", "description": "Графік зараз"},
        {"command": "stop", "description": "Відписатися"},
    ])

    fetcher = None
    fact = None
    offset = None
    next_check = 0.0
    first = True
    try:
        while time.time() < end:
            dirty = False
            if time.time() >= next_check:
                try:
                    if fetcher is None:
                        fetcher = Fetcher()
                    fact = fetcher.get()
                    if state.pop("fail_since", None) is not None:
                        dirty = True
                    state.pop("fail_alerted", None)
                    dirty |= check_schedules(fact, state, force=FORCE and first)
                    first = False
                except Exception as e:  # noqa: BLE001
                    dirty |= handle_failure(state, e)
                next_check = time.time() + CHECK_EVERY

            wait = int(max(1, min(25, next_check - time.time(), end - time.time())))
            params = {"timeout": wait, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
            if offset is not None:
                params["offset"] = offset
            res = tg("getUpdates", _timeout=wait + 15, **params)
            if not res.get("ok"):
                time.sleep(wait)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    dirty |= handle_update(u, state, fact)
                except Exception as e:  # noqa: BLE001
                    log(f"Помилка обробки {u.get('update_id')}: {e}")

            if dirty:
                save_state(state)
                git_save()
    finally:
        if offset is not None:
            tg("getUpdates", _timeout=15, offset=offset, timeout=0)  # підтвердити оброблене
        save_state(state)
        git_save()
        if fetcher:
            fetcher.close()


if __name__ == "__main__":
    main()
