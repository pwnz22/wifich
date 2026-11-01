#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bdcom-channel.py — находит самый свободный канал Wi-Fi 2.4 ГГц и переключает на него модем BDCOM.

Использование:
  python3 bdcom-channel.py --scan       только анализ эфира, модем не трогаем
  python3 bdcom-channel.py --dry-run    анализ + вход в модем, показать что будет отправлено (ничего не меняет)
  python3 bdcom-channel.py              анализ и переключение, если есть канал заметно лучше
  python3 bdcom-channel.py --force 1    поставить канал 1 без анализа
  python3 bdcom-channel.py --dump       сохранить HTML страницы WLAN в bdcom-wlan.html (для отладки)

Настройки — через переменные окружения:
  BDCOM_ROUTER        адрес модема, например http://192.168.1.1 (обязательно, кроме --scan)
  BDCOM_PASS          пароль от веб-интерфейса модема (обязательно, кроме --scan)
  BDCOM_USER          логин, по умолчанию admin
  BDCOM_KEEP_OPTIONS  текущие значения выпадающих списков через запятую (см. README)

Работает на macOS. Нужен только стандартный Python 3, без сторонних библиотек.
"""

import argparse
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# ───────────────────────── Настройки ─────────────────────────
ROUTER   = os.environ.get("BDCOM_ROUTER", "")
USERNAME = os.environ.get("BDCOM_USER", "admin")
PASSWORD = os.environ.get("BDCOM_PASS", "")

CANDIDATES   = [1, 6, 11]   # только непересекающиеся каналы 2.4 ГГц
UNKNOWN_RSSI = -75          # уровень, если macOS не показала сигнал соседней сети
SWITCH_RATIO = 2.0          # переключать, только если текущий канал в 2+ раза «грязнее» лучшего
OWN_RSSI_DELTA = 10         # сеть на канале модема с сигналом ±10 dB от нашей — вторая SSID самого модема

# Текущие значения выпадающих списков на странице WLAN Basic Settings, как они подписаны в веб-интерфейсе.
# Нужны, только если модем выставляет их через JavaScript и в HTML они не отмечены.
# Без них скрипт ничего не отправит, чтобы не сбить настройки. Поменяешь что-то в модеме — поправь и здесь.
KEEP_OPTIONS = [s.strip() for s in os.environ.get("BDCOM_KEEP_OPTIONS", "").split(",") if s.strip()]

LOGIN_PAGES = ["/admin/login.asp", "/login.asp", "/"]
WLAN_PAGES  = ["/admin/wlbasic.asp", "/wlbasic.asp", "/admin/wlan_basic.asp", "/wlan_basic.asp"]
# ──────────────────────────────────────────────────────────────


# ───────────── 1. Анализ эфира ─────────────

def read_airport(path=None):
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return subprocess.run(["system_profiler", "SPAirPortDataType"],
                          capture_output=True, text=True, check=True).stdout


def parse_networks(text):
    """Возвращает (текущая_сеть, [соседние сети 2.4 ГГц]) из вывода system_profiler."""
    current, others = None, []
    section, entry = None, None
    for raw in text.splitlines():
        s = raw.strip()
        if s == "Current Network Information:":
            section, entry = "cur", None
            continue
        if s == "Other Local Wi-Fi Networks:":
            section, entry = "oth", None
            continue
        if re.match(r"^(awdl|en|llw)\d+:$", s):
            section, entry = None, None
            continue
        if not section or not s:
            continue
        if s.endswith(":") and ": " not in s:          # строка с именем сети
            entry = {"chan": None, "band": None, "width": 20, "rssi": None}
            if section == "cur" and current is None:
                current = entry
            elif section == "oth":
                others.append(entry)
            continue
        if entry is None:
            continue
        m = re.match(r"Channel:\s*(\d+)\s*\(([\d.]+)GHz(?:,\s*(\d+)MHz)?\)", s)
        if m:
            entry["chan"] = int(m.group(1))
            entry["band"] = float(m.group(2))
            entry["width"] = int(m.group(3) or 20)
        m = re.match(r"Signal / Noise:\s*(-?\d+)\s*dBm", s)
        if m:
            entry["rssi"] = int(m.group(1))
    others = [n for n in others if n["band"] == 2 and n["chan"]]
    return current, others


def split_own(current, nets):
    """Отделяет сети самого модема (гостевая/вторая SSID): тот же канал и почти тот же сигнал, что у нашей."""
    if not current or current["band"] != 2 or current["rssi"] is None:
        return nets, []
    others, own = [], []
    for n in nets:
        same = (n["chan"] == current["chan"] and n["rssi"] is not None
                and abs(n["rssi"] - current["rssi"]) <= OWN_RSSI_DELTA)
        (own if same else others).append(n)
    return others, own


def span(ch, width):
    # Каналы 2.4 ГГц идут с шагом 5 МГц, сеть 20 МГц занимает ≈ ±2 канала от центра.
    # Для 40 МГц macOS показывает основной канал, а вторая половина может быть сверху
    # или снизу — считаем, что занято с обеих сторон.
    return (ch - 2, ch + 2) if width <= 20 else (ch - 6, ch + 6)


def interference(ch, nets):
    lo, hi = span(ch, 20)
    total = 0.0
    for n in nets:
        nlo, nhi = span(n["chan"], n["width"])
        overlap = max(0, min(hi, nhi) - max(lo, nlo)) / 4      # 1.0 = тот же канал
        if overlap:
            rssi = n["rssi"] if n["rssi"] is not None else UNKNOWN_RSSI
            total += overlap * 10 ** ((rssi + 100) / 10)       # мощность в относительных единицах
    return total / 100


# ───────────── 2. Работа с модемом ─────────────

class Forms(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.form, self.select, self.option = [], None, None, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self.form = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(),
                         "name": a.get("name", ""), "fields": []}
            self.forms.append(self.form)
        elif self.form is None:
            return
        elif tag == "input":
            self.form["fields"].append(("input", a))
        elif tag == "select":
            self.select = {"name": a.get("name", ""), "options": [], "disabled": "disabled" in a}
            self.form["fields"].append(("select", self.select))
        elif tag == "option" and self.select is not None:
            self.option = {"value": a.get("value"), "text": "", "selected": "selected" in a}
            self.select["options"].append(self.option)

    def handle_data(self, data):
        if self.option is not None:
            self.option["text"] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self.option = None
        elif tag == "select":
            self.select = self.option = None
        elif tag == "form":
            self.form = None


def parse_forms(html):
    p = Forms()
    p.feed(html)
    return p.forms


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def opt_value(o):
    return o["value"] if o["value"] else o["text"].strip()


def build_payload(form, overrides=None, chan_field=None, channel=None):
    """Собирает данные формы в том порядке, в каком их отправил бы браузер.
    Возвращает (пары, неизвестные_поля)."""
    overrides = overrides or {}
    keep = {norm(k) for k in KEEP_OPTIONS}
    data, unknown, submits = [], [], []
    for kind, f in form["fields"]:
        name = f.get("name")
        if not name or name == "postSecurityFlag":
            continue
        if "disabled" in f and f.get("disabled") is not False:   # браузер не отправляет disabled-поля
            continue
        if name == chan_field:
            data.append([name, str(channel)])
            continue
        if name in overrides:
            data.append([name, overrides[name]])
            continue
        if kind == "input":
            t = (f.get("type") or "text").lower()
            if t in ("checkbox", "radio") and "checked" not in f:
                continue
            if t in ("button", "reset", "image", "file"):
                continue
            if t == "submit":
                # отправляется только нажатая кнопка, на её месте в форме
                submits.append(len(data))
                data.append([name, f.get("value", ""), "submit"])
                continue
            val = f.get("value", "")
            if t == "checkbox" and val == "":
                val = "on"
            if "ssid" in name.lower() and val == "":
                unknown.append(name)
                val = None
            data.append([name, val])
        else:
            opts = f["options"]
            sel = next((o for o in opts if o["selected"]), None)
            if sel is None:
                sel = next((o for o in opts if norm(o["text"]) in keep), None)
            if sel is None:
                unknown.append(name)
                data.append([name, None])
            else:
                data.append([name, opt_value(sel)])
    if submits:
        best = next((i for i in submits if re.search(r"apply|save|login", data[i][1], re.I)), submits[0])
        data = [d[:2] for i, d in enumerate(data) if i == best or i not in submits]
    return data, unknown


def js_encode(s):
    """encodeURIComponent + замены из postTableEncrypt() прошивки Realtek."""
    return urllib.parse.quote(s, safe="*").replace("~", "%7E").replace("%20", "+")


def to_int32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def security_flag(s):
    """Повторяет контрольную сумму postTableEncrypt() из common.js модема."""
    csum, i, n = 0, 0, len(s)
    while i < n:
        if i + 4 > n:
            if i < n:
                csum += ord(s[i]) << 24
            if i + 1 < n:
                csum += ord(s[i + 1]) << 16
            if i + 2 < n:
                csum += ord(s[i + 2]) << 8
            break
        csum += (ord(s[i]) << 24) + (ord(s[i + 1]) << 16) + (ord(s[i + 2]) << 8) + ord(s[i + 3])
        i += 4
    c = to_int32(csum)
    c = (c & 0xFFFF) + (c >> 16)
    c &= 0xFFFF
    return (~c) & 0xFFFF


def encode_body(pairs, with_flag=True):
    """Тело POST-запроса как у браузера: поля по порядку, в конце postSecurityFlag."""
    main, tail = "", ""
    for k, v in pairs:
        if v is None:
            continue
        item = k.replace("[", "%5B").replace("]", "%5D") + "=" + js_encode(v) + "&"
        if k == "csrftoken":
            tail += item
        else:
            main += item
    if with_flag:
        return main + tail + f"postSecurityFlag={security_flag(main)}"
    return (main + tail).rstrip("&")


def form_has_flag(form):
    return any(f.get("name") == "postSecurityFlag" for _, f in form["fields"])


class Router:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh) bdcom-channel")]

    def url(self, path):
        return urllib.parse.urljoin(self.base + "/", path)

    def request(self, url, body=None, referer=None):
        req = urllib.request.Request(url, data=body.encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if referer:
            req.add_header("Referer", referer)
        with self.op.open(req, timeout=15) as r:
            raw = r.read()
            return r.geturl(), raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def has_password_field(form):
    return any(k == "input" and (a.get("type") or "").lower() == "password" for k, a in form["fields"])


def login(rt):
    for path in LOGIN_PAGES:
        try:
            url, html = rt.request(rt.url(path))
        except (urllib.error.URLError, OSError):
            continue
        for form in parse_forms(html):
            if not has_password_field(form):
                continue
            inputs = [a for k, a in form["fields"] if k == "input" and a.get("name")]
            pw = next(a["name"] for a in inputs if (a.get("type") or "").lower() == "password")
            user = next((a["name"] for a in inputs if (a.get("type") or "text").lower() == "text"), None)
            overrides = {pw: PASSWORD}
            if user:
                overrides[user] = USERNAME
            data, _ = build_payload(form, overrides)
            body = encode_body(data, form_has_flag(form))
            try:
                rt.request(urllib.parse.urljoin(url, form["action"] or url), body, referer=url)
            except urllib.error.HTTPError as e:
                sys.exit(f"Модем отклонил вход (HTTP {e.code}). Проверь логин и пароль.")
            return True
    return False


CHAN_NAME = re.compile(r"^(chan|channel|channel_?num(ber)?|wlan_?chan|ch)$", re.I)


def find_wlan_form(rt):
    last = None
    for path in WLAN_PAGES:
        try:
            url, html = rt.request(rt.url(path))
        except (urllib.error.URLError, OSError):
            continue
        last = (url, html)
        for form in parse_forms(html):
            for _, f in form["fields"]:
                if CHAN_NAME.match(f.get("name") or ""):
                    return url, html, form, f["name"]
        if any(has_password_field(f) for f in parse_forms(html)):
            return None, html, "login", None
    return (last[0], last[1], None, None) if last else (None, None, None, None)


def wait_for_channel(target, timeout=60):
    """Ждёт, пока Mac переподключится к модему, и возвращает текущий канал 2.4 ГГц (или None)."""
    deadline = time.time() + timeout
    chan = None
    while time.time() < deadline:
        time.sleep(5)
        try:
            cur, _ = parse_networks(read_airport())
        except Exception:
            continue
        chan = cur["chan"] if cur and cur["band"] == 2 else None
        if chan == target:
            break
    return chan


def text_of(html, limit=300):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


# ───────────── 3. Основная логика ─────────────

def main():
    ap = argparse.ArgumentParser(description="Автовыбор канала Wi-Fi 2.4 ГГц для модема BDCOM")
    ap.add_argument("--scan", action="store_true", help="только анализ эфира")
    ap.add_argument("--dry-run", action="store_true", help="показать, что будет отправлено, без изменений")
    ap.add_argument("--force", type=int, metavar="N", help="поставить канал N без анализа")
    ap.add_argument("--dump", action="store_true", help="сохранить HTML страницы WLAN в bdcom-wlan.html")
    ap.add_argument("--scan-file", help=argparse.SUPPRESS)
    args = ap.parse_args()

    target = args.force
    if target is None or args.scan:
        current, nets = parse_networks(read_airport(args.scan_file))
        cur = current["chan"] if current and current["band"] == 2 else None
        nets, own = split_own(current, nets)

        print("Соседние сети 2.4 ГГц:")
        for n in sorted(nets + own, key=lambda n: n["chan"]):
            lvl = f'{n["rssi"]} dBm' if n["rssi"] is not None else "уровень неизвестен"
            note = "  ← похоже, сам модем, не учитываю" if any(n is o for o in own) else ""
            print(f'  канал {n["chan"]:>2} ({n["width"]} МГц)  {lvl}{note}')
        if not nets and not own:
            print("  не найдено")

        chans = sorted(set(CANDIDATES + ([cur] if cur else [])))
        scores = {c: interference(c, nets) for c in chans}
        best = min(CANDIDATES, key=lambda c: (round(scores[c]), c != cur, c))

        print("\nПомехи по каналам (меньше — лучше):")
        for c in chans:
            marks = []
            if c == best:
                marks.append("лучший")
            if c == cur:
                marks.append("сейчас")
            print(f"  {c:>2}: {scores[c]:7.0f}   {'← ' + ', '.join(marks) if marks else ''}")
        if cur is None:
            print("  (Mac сейчас не подключён к сети 2.4 ГГц — текущий канал модема неизвестен)")

        if args.scan:
            return
        need = cur not in CANDIDATES or scores[cur] > SWITCH_RATIO * scores[best] + 1
        if not need and not args.dry_run and not args.dump:
            print(f"\nКанал {cur} в порядке, переключать не нужно.")
            return
        target = best
        if target == cur and not args.dry_run and not args.dump:
            print(f"\nУже стоит лучший канал {cur}.")
            return

    if not 1 <= target <= 13:
        sys.exit("Канал должен быть от 1 до 13.")
    if not ROUTER:
        sys.exit("\nНе задан адрес модема: укажи его в BDCOM_ROUTER, например http://192.168.1.1")
    if not PASSWORD:
        sys.exit("\nНе задан пароль модема: укажи его в BDCOM_PASS.")

    print(f"\nВход в модем {ROUTER} ...")
    rt = Router(ROUTER)
    if not login(rt):
        sys.exit("Не нашёл страницу входа. Проверь адрес ROUTER и что Mac подключён к этому модему.")

    url, html, form, chan_field = find_wlan_form(rt)
    if args.dump and html:
        with open("bdcom-wlan.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("HTML страницы сохранён в bdcom-wlan.html")
    if form == "login":
        sys.exit("Вход не удался: модем снова показывает форму логина. Проверь логин/пароль.\n"
                 "Если они верные — возможно, прошивка шифрует пароль в JavaScript; такая прошивка пока не поддерживается.")
    if not form:
        sys.exit("Не нашёл страницу WLAN Basic Settings с полем канала. Возможно, эта прошивка не поддерживается;\n"
                 "страницу можно сохранить через --dump и посмотреть, как называется поле канала.")

    data, unknown = build_payload(form, chan_field=chan_field, channel=target)
    action = urllib.parse.urljoin(url, form["action"] or url)
    body = encode_body(data, form_has_flag(form))

    if args.dry_run or unknown:
        print(f"\nБудет отправлено на {action}:")
        for k, v in data:
            print(f"  {k} = {v if v is not None else '?? (значение не удалось определить)'}")
        if form_has_flag(form):
            print(f"  postSecurityFlag = {body.rsplit('=', 1)[1]}")
    if unknown:
        sys.exit(f"\nНе удалось определить текущие значения полей: {', '.join(unknown)}.\n"
                 "Чтобы случайно не сбить настройки, ничего не отправляю.\n"
                 "Перечисли текущие значения этих списков из веб-интерфейса модема в BDCOM_KEEP_OPTIONS (см. README).")
    if args.dry_run:
        print(f"\n(dry-run) Канал {target} не применён.")
        return

    print(f"Ставлю канал {target} ...")
    resp = None
    try:
        _, resp = rt.request(action, body, referer=url)
    except urllib.error.HTTPError as e:
        sys.exit(f"Модем отклонил изменение (HTTP {e.code}). Посмотри, что отправляется, через --dry-run.")
    except OSError:
        # После применения модем перезапускает Wi-Fi, и Mac теряет связь раньше, чем приходит ответ
        print("Модем не ответил — скорее всего, перезапускает Wi-Fi с новым каналом.")
    if resp:
        msg = text_of(resp)
        if msg:
            print("Ответ модема:", msg)
        if re.search(r"reboot|restart|перезагр", resp, re.I):
            print("Модем может попросить перезагрузку — если канал не сменится, перезагрузи его из веб-интерфейса.")

    print("Жду переподключения Wi-Fi ...")
    chan = wait_for_channel(target)
    if chan == target:
        print(f"Готово: Mac подключён на канале {target}.")
    elif chan:
        print(f"Mac сейчас на канале {chan}, а не {target}. Проверь в веб-интерфейсе модема — возможно, нужна перезагрузка.")
    else:
        print("Не удалось проверить канал: Mac не подключён к сети 2.4 ГГц. Проверь в веб-интерфейсе модема.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
    except FileNotFoundError as e:
        if e.filename == "system_profiler":
            sys.exit("Анализ эфира работает только на macOS: не найден system_profiler.")
        raise
    except urllib.error.URLError as e:
        sys.exit(f"Ошибка связи с модемом: {e}")
