#!/usr/bin/env python3
"""EUR/USD, USD/JPY, EUR/JPY and US Treasury news, reminder and surprise-alert bot.

Runs once per invocation (schedule it every 5 min on GitHub Actions) or
continuously with `--loop` on a small server. Sends alerts to Telegram.

  python eurusd_alerts.py                 # one run
  python eurusd_alerts.py --loop          # run every 60s forever
  python eurusd_alerts.py backfill f.csv  # build history from past releases
  python eurusd_alerts.py check           # test every source, key and Telegram (prints OK/FAIL)
  python eurusd_alerts.py demo            # send a sample alert to Telegram
"""
import csv, json, os, re, statistics, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python
    ZoneInfo = None

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
TD_KEY = os.getenv("TWELVEDATA_API_KEY")   # prices (free plan: 800 calls/day)
FH_KEY = os.getenv("FINNHUB_API_KEY")      # optional: actual vs forecast
SHOCK_SIGMA = float(os.getenv("SHOCK_SIGMA", "3"))     # std devs a 5-min move must exceed
SURPRISE_PCT = float(os.getenv("SURPRISE_PCT", "5"))   # high-impact miss/beat %
PRICE_CHECK_MIN = int(os.getenv("PRICE_CHECK_MIN", "4"))  # keeps price calls under the free cap
CAL_REFRESH_MIN = int(os.getenv("CAL_REFRESH_MIN", "15"))  # Forex Factory rate-limits frequent calls
STATE_FILE = os.getenv("STATE_FILE", "state.json")
# anti-spam: caps per rolling hour, optional quiet hours (UTC, e.g. "22-6") for non-critical alerts
MAX_NORMAL_PER_HOUR = int(os.getenv("MAX_NORMAL_PER_HOUR", "5"))
MAX_CRITICAL_PER_HOUR = int(os.getenv("MAX_CRITICAL_PER_HOUR", "15"))
QUIET_HOURS_UTC = os.getenv("QUIET_HOURS_UTC", "")
CUR = {"st": None}
try:  # show event times in your own time zone, e.g. TIMEZONE=Asia/Amman (default UTC)
    TZ = ZoneInfo(os.getenv("TIMEZONE") or "UTC")
except Exception:
    TZ = timezone.utc
CUR_WORDS = {"EUR": ("euro", "eurozone", "euro zone", "ecb", "lagarde"),
             "USD": ("dollar", "usd", "fed", "fomc", "powell", "treasury", "yield", "bessent", "payroll", "nfp"),
             "JPY": ("yen", "jpy", "boj", "ueda", "jgb")}

# pair: (pips per 1.0 of price, min 5-min move in pips to flag). EUR/JPY is computed
# as EUR/USD x USD/JPY so it costs no extra price calls.
PAIRS = {
    "EUR/USD": (1e4, float(os.getenv("SHOCK_PIPS_EURUSD", "12"))),
    "USD/JPY": (100, float(os.getenv("SHOCK_PIPS_USDJPY", "15"))),
    "EUR/JPY": (100, float(os.getenv("SHOCK_PIPS_EURJPY", "20"))),
}
# what a stronger currency usually does to each pair (+1 up, -1 down)
EFFECT = {"USD": {"EUR/USD": -1, "USD/JPY": 1}, "EUR": {"EUR/USD": 1, "EUR/JPY": 1},
          "JPY": {"USD/JPY": -1, "EUR/JPY": -1}}
CURRENCIES = ("EUR", "USD", "JPY")
FH_COUNTRY = {"US": "USD", "EU": "EUR", "JP": "JPY"}
TREASURY = ("bond auction", "note auction", "tips auction", "treasury")

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# name: (url, minimum score to alert, must mention pair-relevant terms)
FEEDS = {
    "ECB": ("https://www.ecb.europa.eu/rss/press.html", 2, False),
    "Fed": ("https://www.federalreserve.gov/feeds/press_all.xml", 2, False),
    "Fed speeches": ("https://www.federalreserve.gov/feeds/speeches.xml", 2, False),
    "BoJ": ("https://www.boj.or.jp/en/rss/whatsnew.xml", 2, False),
    "BLS": ("https://www.bls.gov/feed/bls_latest.rss", 2, False),
    "FXStreet": ("https://www.fxstreet.com/rss/news", 3, True),
    "Investing.com": ("https://www.investing.com/rss/news_1.rss", 3, True),
    "investingLive": ("https://investinglive.com/feed/forex/", 3, True),
}
# News sites publish constantly, so they only alert on strong, pair-related headlines.
RELEVANT = ("eur/usd", "eurusd", "usd/jpy", "usdjpy", "eur/jpy", "eurjpy", "euro", "eurozone", "ecb",
            "fed", "dollar", "usd", "yen", "jpy", "boj", "ueda", "treasury", "yield",
            "bessent", "auction", "powell", "lagarde", "nfp", "payroll", "cpi", "inflation", "fomc")
KEYWORDS = {
    3: ("rate decision", "interest rate", "emergency", "monetary policy", "fomc statement",
        "balance sheet", "intervention", "unexpected", "surprise", "shock", "plunge", "surge",
        "tumble", "soar", "breaking", "flash", "refunding", "debt ceiling", "yield curve control"),
    2: ("inflation", "press conference", "projections", "minutes", "employment", "gdp", "speech",
        "lagarde", "powell", "ueda", "bessent", "payroll", "cpi", "auction", "yield", "jgb"),
}
INVERTED = ("unemployment", "jobless", "claims")  # higher = weaker currency
UA = {"User-Agent": "eurusd-alerts/1.0"}
FEED_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36",
           "Accept": "application/rss+xml, application/xml, text/xml, */*"}


# ---------- plumbing ----------
def now():
    return datetime.now(timezone.utc)


def load():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, v in (("seen", []), ("reminded", []), ("alerted", []), ("pending", []),
                 ("recent", []), ("history", {}), ("last_shock", {}), ("feeds_init", []),
                 ("last_heartbeat", ""), ("last_run", {}), ("sent_log", []), ("titles", []), ("held", 0), ("cal", None), ("fails", {}), ("px", {})):
        st.setdefault(k, v)
    if not isinstance(st["last_shock"], dict):
        st["last_shock"] = {}
    return st


def save(st):
    for k in ("seen", "reminded", "alerted"):
        st[k] = st[k][-1500:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def scrub(x):
    """Remove secrets from any text before it is printed or sent (error messages can contain URLs with keys)."""
    x = str(x)
    for v in (TG_TOKEN, TD_KEY, FH_KEY, TG_CHAT):
        if v:
            x = x.replace(v, "***")
    return x


def has(text, words):
    """Whole-word match, so 'euro' does not match 'european'."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text) for w in words)


def entries(root):
    """(title, link) pairs from RSS 2.0, RSS 1.0/RDF or Atom feeds."""
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] in ("item", "entry"):
            t = l = ""
            for c in el:
                nm = c.tag.rsplit("}", 1)[-1]
                if nm == "title" and not t:
                    t = (c.text or "").strip()
                elif nm == "link" and not l:
                    l = (c.text or "").strip() or c.get("href", "")
            out.append((t, l))
    return out


def get_items(url):
    """Download a feed -> [(title, link)]. Raises RuntimeError with a short, readable reason."""
    r = requests.get(url, headers=FEED_UA, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = r.content.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        items = entries(ET.fromstring(body))
    except ET.ParseError:
        items = None
    if items is None or (not items and not re.search(rb"<(rss|feed|rdf)", body[:600], re.I)):
        raise RuntimeError("not a valid feed (page starts with: " + re.sub(r"\s+", " ", r.text[:50]) + ")")
    return items


def quiet(n):
    if not QUIET_HOURS_UTC:
        return False
    a, b = map(int, QUIET_HOURS_UTC.split("-"))
    return (a <= n.hour < b) if a < b else (n.hour >= a or n.hour < b)


def send(text, prio="critical"):
    """prio 'critical' (big events, shocks) or 'normal' (medium news). Caps stop spam."""
    st = CUR["st"]
    if st is not None:
        n = now()
        st["sent_log"] = [x for x in st["sent_log"] if n - datetime.fromisoformat(x[0]) < timedelta(hours=1)]
        used = sum(1 for _, p in st["sent_log"] if p == prio)
        cap = MAX_NORMAL_PER_HOUR if prio == "normal" else MAX_CRITICAL_PER_HOUR
        if used >= cap or (prio == "normal" and quiet(n)):
            st["held"] += 1
            print("held back:", text[:70])
            return
        st["sent_log"].append([n.isoformat(), prio])
    print(text, "\n")
    if not (TG_TOKEN and TG_CHAT):
        return
    for attempt in (1, 2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": TG_CHAT, "text": text[:4000],
                                    "disable_web_page_preview": True}, timeout=20)
            if r.ok:
                return
            print("telegram rejected:", r.status_code, r.text[:100])
        except requests.RequestException as e:
            print("telegram error:", type(e).__name__)
        time.sleep(2)


def is_dup(st, title):
    """Same story from another source? (60%+ word overlap with a recent headline)"""
    w = set(re.findall(r"[a-z0-9]+", title.lower()))
    return any(w and set(t.split()) and len(w & set(t.split())) / len(w | set(t.split())) >= 0.6
               for t in st["titles"])


def due(st, key, minutes):
    """True at most once per `minutes` for this task (protects free API quotas)."""
    last = st["last_run"].get(key)
    if last and now() - datetime.fromisoformat(last) < timedelta(minutes=minutes):
        return False
    st["last_run"][key] = now().isoformat()
    return True


def market_open(n=None):
    n = n or now()
    d, h = n.weekday(), n.hour
    return not (d == 5 or (d == 4 and h >= 22) or (d == 6 and h < 22))


def side(v):
    return "▲ buyers" if v > 0 else "▼ sellers"


def pf(v):
    """Pips with direction arrow, e.g. 52▼ (EUR/USD down) or 31▲ (up)."""
    return f"{abs(v):.0f}{'▲' if v > 0 else '▼'}"


def num(x):
    """'0.4%' -> 0.4, '225K' -> 225000, '-1.2B' -> -1.2e9; None if not a plain number."""
    m = re.fullmatch(r"\s*(-?[\d,]*\.?\d+)\s*([kKmMbBtT]?)\s*%?\s*", str(x or ""))
    if not m:
        return None
    return float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[m.group(2).lower()]


def classify(actual, forecast):
    a, f = num(actual), num(forecast)
    if a is None or f is None:
        return None
    return "above" if a > f else "below" if a < f else "inline"


def fill_actual(st, name, e):
    """Free source of actual results: next release's 'previous' value = this release's actual.
    Lets the latest logged release be tagged above/below forecast one cycle later."""
    if e["prev"] in ("n/a", ""):
        return
    h = sorted(st["history"].get(name, []), key=lambda x: x["date"])
    if not h:
        return
    last = h[-1]
    if "actual" in last or last.get("fc") in (None, "", "n/a"):
        return
    d = datetime.strptime(last["date"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    if not (timedelta(hours=12) < e["t"] - d < timedelta(days=45)):
        return
    last["actual"] = e["prev"]
    if classify(e["prev"], last["fc"]):
        last["dir"] = classify(e["prev"], last["fc"])


def hm(t):
    return t.astimezone(TZ).strftime("%H:%M")


def px_fmt(pair, v):
    return f"{v:.{4 if PAIRS[pair][0] == 1e4 else 2}f}"


def now_line(st, pairs):
    """Latest known prices (from the most recent price check, if fresh)."""
    out = []
    for p in pairs:
        v = st["px"].get(p)
        if v and now() - datetime.fromisoformat(v[1]) < timedelta(minutes=12):
            out.append(f"{p} {px_fmt(p, v[0])}")
    return ("Now: " + " · ".join(out)) if out else ""


def beat_line(title, fc):
    """What a better-than-forecast result usually does (skipped when there is no forecast)."""
    cur = title.split()[0]
    if cur not in EFFECT or fc in ("n/a", ""):
        return ""
    sign = -1 if any(w in title.lower() for w in INVERTED) else 1
    up = " ".join(f'{p}{"▲" if EFFECT[cur][p] * sign > 0 else "▼"}' for p in EFFECT[cur])
    mixed = ", ".join(p for p in PAIRS if p not in EFFECT[cur])
    return f"Above fcst usually: {up}" + (f" ({mixed} mixed)" if mixed else "") + " (below: reverse)"


def affected(low):
    ps = []
    for cur, ws in CUR_WORDS.items():
        if has(low, ws):
            ps += [p for p in EFFECT[cur] if p not in ps]
    if ps and "EUR/JPY" not in ps:
        ps.append("EUR/JPY (mixed)")
    return ps


def relevant(title):
    """All three pairs are shown on every alert."""
    return list(PAIRS)


# ---------- prices & historical reaction ----------
def candles(symbol, start=None, end=None, size=90):
    p = {"symbol": symbol, "interval": "1min", "apikey": TD_KEY,
         "timezone": "UTC", "outputsize": size}
    if start and end:
        fmt = "%Y-%m-%d %H:%M:%S"
        p.update(start_date=start.strftime(fmt), end_date=end.strftime(fmt), outputsize=200)
    d = requests.get("https://api.twelvedata.com/time_series", params=p, timeout=30).json()
    if "values" not in d:
        raise RuntimeError("price data: " + str(d.get("message", d))[:120])
    rows = d["values"]
    return dict((datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
                 .replace(tzinfo=timezone.utc), float(r["close"])) for r in rows)


def pair_series(start=None, end=None, size=90):
    """Two API calls give all three pairs (EUR/JPY = EUR/USD x USD/JPY)."""
    a, b = candles("EUR/USD", start, end, size), candles("USD/JPY", start, end, size)
    ts = sorted(set(a) & set(b))
    return {"EUR/USD": [(t, a[t]) for t in ts], "USD/JPY": [(t, b[t]) for t in ts],
            "EUR/JPY": [(t, a[t] * b[t]) for t in ts]}


def price_at(series, t):
    prior = [c for ts, c in series if ts <= t]
    return prior[-1] if prior else None


def reaction(t, horizon=60):
    """Pip moves at +5/+30/+60 min (up to `horizon`) after t, plus the biggest swing (peak)."""
    S = pair_series(t - timedelta(minutes=5), t + timedelta(minutes=horizon + 5))
    out = {}
    for pair, s in S.items():
        k = PAIRS[pair][0]
        base = price_at(s, t - timedelta(minutes=1))
        if base is None:
            return None
        r = {}
        for m in (5, 30, 60):
            if m > horizon:
                continue
            p = price_at(s, t + timedelta(minutes=m))
            if p is None:
                return None
            r[f"m{m}"] = round((p - base) * k, 1)
        devs = [(c - base) * k for ts, c in s if t <= ts <= t + timedelta(minutes=horizon)]
        r["peak"] = round(max(devs, key=abs), 1) if devs else r.get("m5", 0)
        end_px = price_at(s, t + timedelta(minutes=horizon))
        if end_px is not None:
            r["px"] = round(end_px, 5)
        out[pair] = r
    return out


def pairs_of(x):
    """History entry -> {pair: moves}. Old entries (EUR/USD only) still work."""
    if "pairs" in x:
        return x["pairs"]
    return {"EUR/USD": {"m5": x["m5"], "m30": x["m30"], "m60": x["m60"],
                        "peak": x.get("peak", x["m30"])}}


def hist_line(st, title, pairs=None, only=None, split=True):
    h = sorted(st["history"].get(title, []), key=lambda x: x["date"])
    if not h:
        return "No history yet (builds as releases happen)"
    lines = []
    for pair in pairs or relevant(title):
        rows = [pairs_of(x)[pair]["peak"] for x in h if pair in pairs_of(x)]
        if rows:
            avg = statistics.mean(abs(v) for v in rows)
            up = sum(1 for v in rows if v > 0)
            lines.append(f"{pair} last {pf(rows[-1])} | max {pf(max(rows, key=abs))} | "
                         f"avg {avg:.0f} | up {up}/{len(rows)}")
    out = "Past reactions, pips (▲ buyers ▼ sellers):\n" + "\n".join(lines)
    if split:
        for d, label in (("above", "above fcst"), ("below", "below fcst")):
            if only and only != d:
                continue
            rows = [x for x in h if x.get("dir") == d]
            if not rows:
                continue
            parts = []
            for pair in pairs or relevant(title):
                v = [pairs_of(x)[pair]["peak"] for x in rows if pair in pairs_of(x)]
                if v:
                    parts.append(f"{pair} {pf(statistics.mean(v))} (up {sum(1 for y in v if y > 0)}/{len(v)})")
            out += f"\nWhen {label} ({len(rows)}x, avg swing): " + " · ".join(parts)
    return out


# ---------- 1) scheduled events: reminders + reaction logging ----------
def calendar(st=None):
    st = st or CUR["st"]
    cached = st.get("cal") if st else None
    if cached and now() - datetime.fromisoformat(cached["at"]) < timedelta(minutes=CAL_REFRESH_MIN):
        data = cached["raw"]
    else:
        try:
            data = requests.get(FF_URL, headers=UA, timeout=30).json()
            if not isinstance(data, list):
                raise ValueError("unexpected calendar response")
            if st is not None:
                st["cal"] = {"at": now().isoformat(), "raw": data}
        except (requests.RequestException, ValueError):
            if not cached:
                raise RuntimeError("Forex Factory calendar unreachable")
            data = cached["raw"]  # fall back to the last good copy
    out = []
    for e in data:
        imp, title = e.get("impact"), e.get("title", "")
        if e.get("country") == "USD" and imp == "Low" and any(k in title.lower() for k in TREASURY):
            imp = "Medium"  # Treasury auctions move yields and USD/JPY
        if e.get("country") not in CURRENCIES or imp not in ("High", "Medium"):
            continue
        t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
        out.append(dict(id=f'{e["country"]}|{title}|{t.isoformat()}', title=title,
                        cur=e["country"], impact=imp, t=t,
                        fc=e.get("forecast") or "n/a", prev=e.get("previous") or "n/a"))
    return out


def events(st):
    n = now()
    for e in calendar(st):
        fill_actual(st, f'{e["cur"]} {e["title"]}', e)
        mins = (e["t"] - n).total_seconds() / 60
        window = 60 if e["impact"] == "High" else 15
        if 0 < mins <= window and e["id"] not in st["reminded"]:
            icon = "🔴" if e["impact"] == "High" else "🟠"
            name = f'{e["cur"]} {e["title"]}'
            parts = [f'{icon} {name} · {hm(e["t"])} (in {mins:.0f} min)',
                     f'Fcst {e["fc"]} · Prev {e["prev"]}',
                     beat_line(name, e["fc"]), now_line(st, relevant(name)), hist_line(st, name)]
            send("\n".join(x for x in parts if x), "critical" if e["impact"] == "High" else "normal")
            st["reminded"].append(e["id"])
            if mins <= 10:
                st["reminded"].append(e["id"] + "|soon")
            st["pending"].append(dict(title=name, t=e["t"].isoformat(), high=e["impact"] == "High",
                                      fc=e["fc"]))
        elif (e["impact"] == "High" and 0 < mins <= 10 and e["id"] + "|soon" not in st["reminded"]):
            st["reminded"].append(e["id"] + "|soon")
            name = f'{e["cur"]} {e["title"]}'
            send("\n".join(x for x in (f'⏰ {name} in {mins:.0f} min', beat_line(name, e["fc"]),
                                       now_line(st, relevant(name))) if x))
    keep = []
    for p in st["pending"]:
        t = datetime.fromisoformat(p["t"])
        rel = relevant(p["title"])
        if n < t + timedelta(minutes=65):
            if (p["high"] and not p.get("quick") and n >= t + timedelta(minutes=6)
                    and TD_KEY and market_open(n)):
                p["quick"] = True
                q = reaction(t, horizon=5)
                if q:
                    send(f'⏱ {p["title"]} +5 min:\n' + "\n".join(
                        f'{x} {pf(q[x]["m5"])} → {px_fmt(x, q[x]["px"])}' for x in rel if "px" in q[x]))
            keep.append(p)
            continue
        if n > t + timedelta(hours=6):
            continue  # too old to measure reliably
        r = reaction(t) if TD_KEY else None
        if r:
            st["history"].setdefault(p["title"], []).append(
                dict(date=p["t"][:16].replace("T", " "), pairs=r, fc=p.get("fc", "n/a")))
            if p["high"]:
                body = "\n".join(
                    f'{x} peak {pf(r[x]["peak"])} | 5m {r[x]["m5"]:+.0f} · 30m {r[x]["m30"]:+.0f} · '
                    f'60m {r[x]["m60"]:+.0f}' + (" (faded)" if abs(r[x]["m60"]) < 0.5 * abs(r[x]["peak"]) else "")
                    + (f' → {px_fmt(x, r[x]["px"])}' if "px" in r[x] else "") for x in rel)
                send(f'📊 {p["title"]} result:\n{body}')
        else:
            keep.append(p)
    st["pending"] = keep


# ---------- 2) surprises: actual vs forecast (needs Finnhub key) ----------
def surprises(st):
    if not FH_KEY or not due(st, "surprises", PRICE_CHECK_MIN):
        return
    try:
        resp = requests.get("https://finnhub.io/api/v1/calendar/economic",
                            params={"token": FH_KEY}, timeout=30).json()
    except (requests.RequestException, ValueError):
        return
    if "economicCalendar" not in resp:
        raise RuntimeError("Finnhub: " + str(resp)[:120] + " (plan may not include the calendar)")
    rows = resp["economicCalendar"]
    for e in rows:
        a, f = e.get("actual"), e.get("estimate")
        if a is None or f is None or e.get("country") not in FH_COUNTRY:
            continue
        if e.get("impact") not in ("high", "medium"):
            continue
        key = f'{e.get("event")}|{e.get("time")}'
        if key in st["alerted"]:
            continue
        pct = abs(a - f) / max(abs(f), 0.1) * 100
        if a == f or pct < (SURPRISE_PCT if e["impact"] == "high" else SURPRISE_PCT * 3):
            continue
        st["alerted"].append(key)
        name = str(e.get("event", ""))
        cur = FH_COUNTRY[e["country"]]
        stronger = (a > f) != any(w in name.lower() for w in INVERTED)
        sign = 1 if stronger else -1
        moves = ", ".join(f'{p} {"up" if EFFECT[cur][p] * sign > 0 else "down"}' for p in EFFECT[cur])
        arrows = ", ".join(f'{p} {"▲" if EFFECT[cur][p] * sign > 0 else "▼"}' for p in EFFECT[cur])
        send(f'⚡ {cur} {name}: {a} vs fcst {f}\n'
             f'{"Stronger" if stronger else "Weaker"} {cur} → {arrows}\n'
             f'{hist_line(st, f"{cur} {name}", only="above" if a > f else "below")}')


# ---------- 3) price shock: catches surprises of any kind ----------
def shock(st):
    if not TD_KEY or not market_open() or not due(st, "shock", PRICE_CHECK_MIN):
        return
    S = pair_series(size=90)
    hits, moved = [], []
    for pair, s in S.items():
        k, min_pips = PAIRS[pair]
        c = [x for _, x in s]
        if len(c) < 40 or now() - s[-1][0] > timedelta(minutes=15):
            continue  # no data or market closed
        sd = statistics.pstdev([(c[i] - c[i - 5]) * k for i in range(5, len(c) - 5)]) or 0.5
        mv = (c[-1] - c[-6]) * k
        h1 = (c[-1] - c[-61]) * k if len(c) >= 61 else 0
        st["px"][pair] = [c[-1], s[-1][0].isoformat()]
        last = st["last_shock"].get(pair)
        cooled = not last or now() - datetime.fromisoformat(last) > timedelta(minutes=30)
        if abs(mv) >= min_pips and abs(mv) >= SHOCK_SIGMA * sd and cooled:
            st["last_shock"][pair] = now().isoformat()
            moved.append(pair)
            hits.append(f"{pair} {abs(mv):.0f} pips {side(mv)} in 5 min ({abs(mv) / sd:.1f}x normal, "
                        f"1h {pf(h1)}) → {px_fmt(pair, c[-1])}")
    if not hits:
        return
    lines = ["🚨 " + hits[0]] + hits[1:]
    ev = [e for e in calendar(st) if timedelta(minutes=-1) <= now() - e["t"] <= timedelta(minutes=20)]
    if ev:
        e = ev[0]
        lines.append(f'Trigger: {e["cur"]} {e["title"]} (fcst {e["fc"]}, prev {e["prev"]})')
        h = hist_line(st, f'{e["cur"]} {e["title"]}', pairs=moved, split=False)
        if not h.startswith("No history"):
            lines.append(h)
    lines += ["• " + x for x in st["recent"][-2:]]
    send("\n".join(lines))


# ---------- 4) headlines (central banks, Treasury-related, forex news) ----------
def headlines(st):
    for src, (url, min_score, need_rel) in FEEDS.items():
        first = src not in st["feeds_init"]  # don't flood alerts when a feed is new
        try:
            items = get_items(url)
        except (requests.RequestException, RuntimeError) as e:
            print(f"feed {src} failed: {e if isinstance(e, RuntimeError) else type(e).__name__}")
            continue
        if not items:
            print(f"feed {src}: 0 items")
        for title, link in items:
            if not title or link in st["seen"]:
                continue
            st["seen"].append(link)
            low = title.lower()
            if need_rel and not has(low, RELEVANT):
                continue
            score = max([s for s, ws in KEYWORDS.items() if has(low, ws)] or [1])
            if score >= min_score and not is_dup(st, title):
                st["titles"] = (st["titles"] + [" ".join(re.findall(r"[a-z0-9]+", low))])[-80:]
                st["recent"] = (st["recent"] + [f"{src}: {title}"])[-10:]
                if not first:
                    ap = affected(low)
                    send(f'{"🔴" if score == 3 else "🟠"} {src}: {title}\n'
                         + (f'Affects: {", ".join(ap)}\n' if ap else "") + link,
                         "critical" if score == 3 else "normal")
        if src not in st["feeds_init"]:
            st["feeds_init"].append(src)


# ---------- daily "I'm alive" message so silent failures get noticed ----------
def heartbeat(st):
    n = now()
    today = n.strftime("%Y-%m-%d")
    if n.hour < 6 or st["last_heartbeat"] == today:
        return
    st["last_heartbeat"] = today
    nxt = [e for e in calendar(st) if e["impact"] == "High" and n < e["t"] <= n + timedelta(hours=24)]
    lines = "\n".join(f'{hm(e["t"])} {e["cur"]} {e["title"]} (fcst {e["fc"]})' for e in nxt[:6]) or "none"
    held, st["held"] = st["held"], 0
    extra = f" ({held} minor alerts held back)" if held else ""
    send(f"✅ Running{extra}. High impact next 24h ({os.getenv('TIMEZONE') or 'UTC'}):\n{lines}")


# ---------- historical backfill from a CSV of past releases ----------
def backfill(path):
    """CSV columns: datetime_utc (YYYY-MM-DD HH:MM), title (e.g. 'USD CPI m/m'),
    and optionally forecast and actual (enables the above/below-forecast split)."""
    st = load()
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.strptime(row["datetime_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            r = reaction(t)
            if r:
                entry = dict(date=row["datetime_utc"], pairs=r)
                fc, ac = (row.get("forecast") or "").strip(), (row.get("actual") or "").strip()
                if fc and ac:
                    entry.update(fc=fc, actual=ac)
                    if classify(ac, fc):
                        entry["dir"] = classify(ac, fc)
                st["history"].setdefault(row["title"], []).append(entry)
                print("logged", row["title"], row["datetime_utc"], r)
            time.sleep(15)  # stay under the free-tier per-minute limit
    save(st)


def check():
    """Test every source, key and Telegram. Prints OK/FAIL lines (never prints secrets)."""
    def ok(m):
        print("OK   ", m)

    def bad(m):
        print("FAIL ", m)

    print(f"Python {sys.version.split()[0]} | timezone {os.getenv('TIMEZONE') or 'UTC'} | market open now: {market_open()}")
    for nm, v in (("TELEGRAM_BOT_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT),
                  ("TWELVEDATA_API_KEY", TD_KEY), ("FINNHUB_API_KEY (optional)", FH_KEY)):
        print("set    " if v else "MISSING", nm)
    try:
        d = requests.get(FF_URL, headers=UA, timeout=30).json()
        ok(f"Forex Factory calendar: {len(d)} events, {sum(1 for e in d if e.get('forecast'))} with forecasts, "
           f"'actual' field present: {any('actual' in e for e in d)}")
        evs = calendar()
        ok(f"EUR/USD/JPY events this week: {len(evs)} ({sum(1 for e in evs if e['impact'] == 'High')} high impact)")
        for e in sorted((e for e in evs if e["t"] > now()), key=lambda e: e["t"])[:6]:
            print(f"       next: {e['t'].astimezone(TZ):%a %H:%M} {e['cur']} {e['title']} [{e['impact']}]")
    except Exception as e:
        bad("Forex Factory calendar: " + type(e).__name__)
    for src, (url, _, _) in FEEDS.items():
        try:
            n = len(get_items(url))
            (ok if n else bad)(f"{src}: {n} items")
        except Exception as e:
            bad(f"{src}: {e if isinstance(e, RuntimeError) else type(e).__name__}")
    if TD_KEY:
        for sym in ("EUR/USD", "USD/JPY"):
            try:
                last = max(candles(sym, size=5))
                age = (now() - last).total_seconds() / 60
                ok(f"{sym}: newest price {last:%H:%M} UTC, {age:.0f} min old"
                   + ("" if market_open() else " (market closed, age is expected)"))
            except Exception as e:
                bad(f"{sym}: {scrub(e)[:120]}")
    if FH_KEY:
        try:
            resp = requests.get("https://finnhub.io/api/v1/calendar/economic", params={"token": FH_KEY}, timeout=30).json()
            (ok if "economicCalendar" in resp else bad)(
                "Finnhub economic calendar: " + ("accessible" if "economicCalendar" in resp else scrub(resp)[:100]))
        except Exception as e:
            bad("Finnhub: " + type(e).__name__)
    if TG_TOKEN and TG_CHAT:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": TG_CHAT, "text": "✅ Test message from your bot"}, timeout=20)
            (ok if r.ok else bad)("Telegram test message " + ("sent" if r.ok else f"rejected: {r.status_code} {r.text[:80]}"))
        except Exception as e:
            bad("Telegram: " + type(e).__name__)


def demo():
    """Send one sample reminder (made-up history) so you can see the format on your phone."""
    def pr(a, b, c):
        return {"EUR/USD": {"peak": a}, "USD/JPY": {"peak": b}, "EUR/JPY": {"peak": c}}
    st = {"history": {"USD CPI m/m": [
        {"date": "2026-08-12 12:30", "pairs": pr(-40, 25, -5), "dir": "above"},
        {"date": "2026-09-10 12:30", "pairs": pr(-30, 15, -8), "dir": "above"},
        {"date": "2026-10-14 12:30", "pairs": pr(35, -20, 10), "dir": "below"}]},
        "px": {"EUR/USD": [1.1201, now().isoformat()], "USD/JPY": [157.0, now().isoformat()]}}
    name = "USD CPI m/m"
    send("SAMPLE with made-up numbers\n" + "\n".join(x for x in (
        f"🔴 {name} · {hm(now())} (in 40 min)", "Fcst 0.3% · Prev 0.6%", beat_line(name, "0.3%"),
        now_line(st, relevant(name)), hist_line(st, name)) if x))


def run_once():
    st = load()
    CUR["st"] = st
    for step in (events, surprises, shock, headlines, heartbeat):
        name = step.__name__
        try:
            step(st)
            st["fails"][name] = 0
        except Exception as e:  # one failing source must not stop the rest
            st["fails"][name] = st["fails"].get(name, 0) + 1
            print(f"{name} failed: {scrub(e)}")
            if st["fails"][name] >= 3 and due(st, "fail_" + name, 360):
                send(f"⚠️ {name} has failed {st['fails'][name]} runs in a row: {scrub(e)[:150]}")
    save(st)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check()
    elif len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()
    elif len(sys.argv) > 2 and sys.argv[1] == "backfill":
        backfill(sys.argv[2])
    elif "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                print("run failed:", scrub(e))
            time.sleep(60)
    else:
        run_once()
