"""
Бот графіків відключень ДТЕК Одеські електромережі.
Запускається GitHub Actions за розкладом, шле графік у Telegram,
коли з'являється новий або змінюється існуючий.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

URL = "https://www.dtek-oem.com.ua/ua/shutdowns"
GROUP = os.environ.get("GROUP", "2.1").strip()
KEY = f"GPV{GROUP}"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
FORCE = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
# з якої години (за Києвом) слати графік нового дня; 0 = одразу після півночі
NEW_DAY_HOUR = int(os.environ.get("NEW_DAY_HOUR", "0") or 0)
FAIL_ALERT_MIN = 60  # через скільки хвилин збоїв попередити в Telegram

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

# статус години на сайті ДТЕК -> (перші 30 хв, другі 30 хв)
STATUS_MAP = {
    "yes": ("on", "on"),
    "no": ("off", "off"),
    "first": ("off", "on"),
    "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"),
    "msecond": ("on", "maybe"),
}


# ---------------------------------------------------------------- дані з сайту
def extract_fact_from_html(html: str):
    """Запасний спосіб: шукає 'DisconSchedule.fact = {...}' прямо в HTML."""
    import re
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


def save_debug(page, attempt: int):
    """Зберігає, що бачив браузер, щоб зрозуміти причину збою."""
    os.makedirs("debug", exist_ok=True)
    try:
        page.screenshot(path=f"debug/attempt{attempt}.png", full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        html = page.content()
        with open(f"debug/attempt{attempt}.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  URL: {page.url}", file=sys.stderr)
        print(f"  Заголовок: {page.title()!r}", file=sys.stderr)
        print(f"  Розмір HTML: {len(html)}; DisconSchedule у коді: "
              f"{'так' if 'DisconSchedule' in html else 'ні'}", file=sys.stderr)
        text = page.inner_text("body")[:400].replace("\n", " ")
        print(f"  Текст сторінки: {text}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  (не вдалося зберегти діагностику: {e})", file=sys.stderr)


def fetch_fact():
    """Відкриває сторінку ДТЕК у справжньому браузері і забирає DisconSchedule.fact."""
    from playwright.sync_api import sync_playwright

    last_err = None
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
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
        page = ctx.new_page()
        for attempt in range(1, 4):
            try:
                page.goto(URL, wait_until="load", timeout=60000)
                # до 40 с чекаємо: сторінка перевірки може сама перенаправити на сайт
                for _ in range(20):
                    fact = page.evaluate(JS_GET_FACT)
                    if fact:
                        browser.close()
                        return fact
                    fact = extract_fact_from_html(page.content())
                    if fact and fact.get("data"):
                        browser.close()
                        return fact
                    page.wait_for_timeout(2000)
                raise RuntimeError("на сторінці немає DisconSchedule.fact")
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"Спроба {attempt} не вдалась: {e}", file=sys.stderr)
                save_debug(page, attempt)
                page.wait_for_timeout(5000 * attempt)
        browser.close()
    raise RuntimeError(f"Не вдалося отримати графік: {last_err}")


# ---------------------------------------------------------------- обробка
def day_slots(hours: dict) -> list:
    """48 півгодинних слотів: 'on' / 'off' / 'maybe'."""
    slots = []
    for h in range(1, 25):
        st = str(hours.get(str(h), "yes")).lower()
        slots.extend(STATUS_MAP.get(st, ("on", "on")))
    return slots


def slot_time(i: int) -> str:
    return f"{i // 2:02d}:{(i % 2) * 30:02d}"


def ranges(slots: list):
    """Групує однакові підряд слоти без світла в інтервали."""
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


def parse_days(fact: dict) -> dict:
    """{ 'YYYY-MM-DD': [48 слотів] } для дат від сьогодні і далі."""
    today = datetime.now(TZ).date()
    days = {}
    for ts, groups in (fact.get("data") or {}).items():
        try:
            d = datetime.fromtimestamp(int(ts), TZ).date()
        except (ValueError, TypeError):
            continue
        if d < today or not isinstance(groups, dict) or KEY not in groups:
            continue
        days[d.isoformat()] = day_slots(groups[KEY])
    return dict(sorted(days.items()))


def fmt_hours(n_slots: int) -> str:
    h, half = divmod(n_slots, 2)
    return f"{h} год" + (" 30 хв" if half else "")


def format_day(date_iso: str, slots: list, mark: str) -> str:
    d = datetime.fromisoformat(date_iso).date()
    today = datetime.now(TZ).date()
    delta = (d - today).days
    label = {0: "Сьогодні", 1: "Завтра"}.get(delta, "")
    title = f"{label}, " if label else ""
    title += f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"

    lines = [f"📅 <b>{title}</b>{mark}"]
    rs = ranges(slots)
    if not rs:
        lines.append("🟢 Відключень не заплановано")
    else:
        for kind, a, b in rs:
            if kind == "off":
                lines.append(f"🔴 {a}–{b}")
            else:
                lines.append(f"🟡 {a}–{b} <i>(можливо)</i>")
        off = slots.count("off")
        if off:
            lines.append(f"⏱ Без світла: {fmt_hours(off)}")
    return "\n".join(lines)


def build_message(days: dict, changed: dict, update_str: str, new_day: bool = False) -> str:
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    if any(v == "changed" for v in changed.values()):
        head = "⚠️ <b>Графік змінився</b>"
    elif changed.get(tomorrow) == "new":
        head = "🆕 <b>З'явився графік на завтра</b>"
    elif changed:
        head = "🆕 <b>Новий графік</b>"
    elif new_day:
        head = "☀️ <b>Графік на сьогодні</b>"
    else:
        head = "📋 <b>Актуальний графік</b>"
    parts = [f"💡 Група {GROUP} · ДТЕК Одеські електромережі\n{head}"]
    for date_iso, slots in days.items():
        mark = {"changed": "  ✏️ змінено", "new": "  🆕"}.get(changed.get(date_iso), "")
        parts.append(format_day(date_iso, slots, mark))
    if update_str:
        parts.append(f"🕒 Оновлено ДТЕК: {update_str}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- telegram і стан
def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TG_TOKEN / TG_CHAT_ID не задані, повідомлення:\n" + text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def handle_failure(state: dict, err: Exception):
    print(err, file=sys.stderr)
    now = int(time.time())
    if not state.get("fail_since"):
        state["fail_since"] = now
        save_state(state)
    elif not state.get("fail_alerted") and now - state["fail_since"] > FAIL_ALERT_MIN * 60:
        send_telegram(
            f"⚠️ Бот групи {GROUP} вже понад годину не може дістати графік з сайту ДТЕК. "
            "Перевір вручну: " + URL
        )
        state["fail_alerted"] = True
        save_state(state)


def signature(slots: list) -> str:
    """Рядок на 48 символів: 1 = світло є, 0 = нема, m = можливо."""
    return "".join({"on": "1", "off": "0"}.get(x, "m") for x in slots)


def run(fact: dict, state: dict):
    days = parse_days(fact)
    if not days:
        print(f"У даних немає групи {KEY} на сьогодні/майбутнє")
        return

    old = state.get("days", {})
    changed = {}
    for date_iso, slots in days.items():
        sig = signature(slots)
        if date_iso not in old:
            changed[date_iso] = "new"
        elif old[date_iso] != sig:
            changed[date_iso] = "changed"

    recovered = bool(state.pop("fail_since", None))
    state.pop("fail_alerted", None)

    now = datetime.now(TZ)
    today = now.date().isoformat()
    # новий день: є графік на сьогодні, сьогодні ще не слали і вже настала потрібна година
    new_day = (
        state.get("last_date") != today
        and today in days
        and now.hour >= NEW_DAY_HOUR
    )

    sent = False
    if changed or new_day or FORCE:
        msg = build_message(days, changed, str(fact.get("update") or ""), new_day)
        send_telegram(msg)
        sent = True
    else:
        print("Без змін")

    old_last = state.get("last_date")
    if sent and today in days:
        state["last_date"] = today
    state["days"] = {d: signature(sl) for d, sl in days.items()}
    if sent or recovered or old != state["days"] or old_last != state.get("last_date"):
        save_state(state)


def main():
    state = load_state()
    try:
        fact = fetch_fact()
    except Exception as e:  # noqa: BLE001
        handle_failure(state, e)
        return
    run(fact, state)


if __name__ == "__main__":
    main()
