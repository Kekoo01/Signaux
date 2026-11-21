#!/usr/bin/env python3
"""
Robot de signaux EURUSD (M15) avec base de donnees.

- AUCUN ordre n'est passe : le robot envoie seulement des signaux sur ton telephone
  (appli ntfy ou WhatsApp ; Telegram en option).
- Aucune dependance a installer (Python 3 standard).
- Educatif : pas un conseil financier. Tester d'abord sur compte DEMO.

Utilisation :
    python bot.py            -> analyse la derniere bougie M15 fermee (a lancer toutes les 15 min)
    python bot.py backtest   -> teste la strategie sur l'historique et affiche/envoie le resultat
"""
import bisect
import csv
import json
import math
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PIP = 0.0001
PIP_USD_PER_LOT = 10.0  # EURUSD, compte en dollars : 1 pip = 10 $ pour 1 lot


def _f(name, default):
    v = os.environ.get(name, "")
    try:
        return float(v) if v.strip() else default
    except ValueError:
        return default


CFG = {
    "symbol": "EUR/USD",
    "api_key": os.environ.get("TWELVE_DATA_KEY", ""),
    # Canaux d'alerte (configure-en au moins un ; sinon le message reste dans le journal)
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),            # appli ntfy : recommande
    "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
    "wa_phone": os.environ.get("WHATSAPP_PHONE", ""),          # WhatsApp via callmebot.com
    "wa_key": os.environ.get("CALLMEBOT_KEY", ""),
    "tg_token": os.environ.get("TELEGRAM_TOKEN", ""),          # Telegram : facultatif
    "tg_chat": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "balance": _f("BALANCE", 0.0),          # solde du compte en $
    "risk_pct": _f("RISK_PCT", 1.0),        # risque par trade en %
    "spread_pips": _f("SPREAD_PIPS", 2.3),  # spread de ton broker, en pips
    "rr": _f("RR", 1.5),                    # gain vise / risque
    "session_start": int(_f("SESSION_START", 7)),   # heure UTC (incluse)  = 02h en Jamaique
    "session_end": int(_f("SESSION_END", 17)),      # heure UTC (exclue)   = 12h en Jamaique
    "max_bars_open": 32,                    # un signal expire apres 32 bougies M15 (8 h)
    "max_data_age_min": 25,                 # ignore les donnees plus vieilles que ca
    "db": os.environ.get("DB_PATH", "signals.db"),
    "csv": os.environ.get("CSV_PATH", "signals.csv"),
    "status": os.environ.get("STATUS_PATH", "status.json"),   # lu par le site
    "max_per_day": int(_f("MAX_PER_DAY", 4)),   # pas plus de 4 signaux par jour
    "min_score": int(_f("MIN_SCORE", 3)),       # qualite minimale du signal (0 a 4)
}


# ---------------------------------------------------------------- donnees
def http_json(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "eurusd-signal-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_bars(interval, size):
    """Bougies EUR/USD depuis Twelve Data (gratuit : 8 appels/min, 800/jour)."""
    if not CFG["api_key"]:
        raise RuntimeError("Cle TWELVE_DATA_KEY manquante.")
    q = urllib.parse.urlencode({
        "symbol": CFG["symbol"], "interval": interval, "outputsize": size,
        "order": "ASC", "timezone": "UTC", "apikey": CFG["api_key"],
    })
    d = http_json("https://api.twelvedata.com/time_series?" + q)
    if d.get("status") == "error" or "values" not in d:
        raise RuntimeError("Twelve Data : " + str(d.get("message", d)))
    bars = []
    for v in d["values"]:
        s = v["datetime"]
        fmt = "%Y-%m-%d %H:%M:%S" if len(s) > 10 else "%Y-%m-%d"
        t = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        bars.append({"t": t, "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])})
    bars.sort(key=lambda b: b["t"])
    return bars


def fetch_price():
    q = urllib.parse.urlencode({"symbol": CFG["symbol"], "apikey": CFG["api_key"]})
    d = http_json("https://api.twelvedata.com/price?" + q)
    return float(d["price"])


def closed_only(bars, minutes, now):
    """Retire la bougie encore en formation."""
    return [b for b in bars if b["t"] + timedelta(minutes=minutes) <= now]


def http_raw(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": "eurusd-signal-bot/1.0"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def notify(text):
    """Envoie le message sur chaque canal configure (ntfy, WhatsApp, Telegram)."""
    print(text)
    sent = False
    if CFG["ntfy_topic"]:
        try:
            http_raw(CFG["ntfy_server"].rstrip("/") + "/" + urllib.parse.quote(CFG["ntfy_topic"]),
                     data=text.encode("utf-8"),
                     headers={"Title": "Signal EURUSD", "Priority": "high", "Tags": "chart_with_upwards_trend"})
            sent = True
        except (urllib.error.URLError, ValueError) as e:
            print("Echec d'envoi ntfy :", e)
    if CFG["wa_phone"] and CFG["wa_key"]:
        try:
            q = urllib.parse.urlencode({"phone": CFG["wa_phone"], "text": text, "apikey": CFG["wa_key"]})
            http_raw("https://api.callmebot.com/whatsapp.php?" + q)
            sent = True
        except (urllib.error.URLError, ValueError) as e:
            print("Echec d'envoi WhatsApp :", e)
    if CFG["tg_token"] and CFG["tg_chat"]:
        try:
            body = urllib.parse.urlencode({"chat_id": CFG["tg_chat"], "text": text}).encode("utf-8")
            http_raw("https://api.telegram.org/bot%s/sendMessage" % CFG["tg_token"], data=body)
            sent = True
        except (urllib.error.URLError, ValueError) as e:
            print("Echec d'envoi Telegram :", e)
    if not sent:
        print("[Aucun canal d'alerte configure ou envoi en echec : message visible seulement dans le journal]")


# ------------------------------------------------------------ indicateurs
def ema(values, n):
    k = 2.0 / (n + 1)
    out, e = [], None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(values[:n]) / n if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    out = [None] * len(bars)
    if len(bars) <= n:
        return out
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))
    a = sum(trs[:n]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i - 1]) / n
        out[i] = a
    return out


def indicators(m15):
    closes = [b["c"] for b in m15]
    return {"ema": ema(closes, 20), "rsi": rsi(closes, 14), "atr": atr(m15, 14)}


def h1_trends(h1):
    """Tendance de fond sur H1 : EMA50 vs EMA200 et prix du bon cote de l'EMA50."""
    c = [b["c"] for b in h1]
    e50, e200 = ema(c, 50), ema(c, 200)
    out = []
    for i in range(len(h1)):
        if e50[i] is None or e200[i] is None:
            out.append(None)
        elif e50[i] > e200[i] and c[i] > e50[i]:
            out.append("up")
        elif e50[i] < e200[i] and c[i] < e50[i]:
            out.append("down")
        else:
            out.append("none")
    return out


def make_trend_lookup(h1):
    tr = h1_trends(h1)
    ends = [b["t"] + timedelta(minutes=60) for b in h1]

    def look(t_end):
        k = bisect.bisect_right(ends, t_end) - 1  # seulement des bougies H1 deja fermees
        return tr[k] if k >= 0 else None
    return look


# --------------------------------------------------------------- strategie
def check_bar(m15, i, ind, trend, min_score=None):
    """Signal sur la bougie M15 numero i (fermee), ou None.

    VENTE : tendance H1 baissiere, EMA20 qui descend, la bougie touche l'EMA20 par le haut
            puis cloture rouge en dessous, RSI > 32 (pas en survente).
    ACHAT : l'inverse. Seulement pendant la session Londres/New York, et si la volatilite est normale.
    """
    if i < 4 or trend not in ("up", "down"):
        return None
    b = m15[i]
    e, r, a = ind["ema"][i], ind["rsi"][i], ind["atr"][i]
    e_old = ind["ema"][i - 3]
    if e is None or r is None or a is None or e_old is None:
        return None
    if not (CFG["session_start"] <= b["t"].hour < CFG["session_end"]):
        return None
    atr_pips = a / PIP
    if atr_pips < 5 or atr_pips > 25:
        return None
    entry = b["c"]
    if trend == "down":
        if not (b["c"] < b["o"] and b["h"] >= e and b["c"] < e and e < e_old and r > 32):
            return None
        risk = max(b["h"] + 0.3 * a - entry, 0.8 * a, 6 * PIP)
        sl, tp, d = entry + risk, entry - CFG["rr"] * risk, "sell"
    else:
        if not (b["c"] > b["o"] and b["l"] <= e and b["c"] > e and e > e_old and r < 68):
            return None
        risk = max(entry - (b["l"] - 0.3 * a), 0.8 * a, 6 * PIP)
        sl, tp, d = entry - risk, entry + CFG["rr"] * risk, "buy"
    rng = b["h"] - b["l"]
    score = int(rng > 0 and abs(b["c"] - b["o"]) / rng >= 0.45)          # bougie de rejet franche
    score += int(abs(e - e_old) >= 0.3 * a)                              # pente nette de l'EMA20
    score += int(7 <= atr_pips <= 18)                                    # volatilite confortable
    score += int((38 <= r <= 58) if d == "sell" else (42 <= r <= 62))    # RSI dans la bonne zone
    if score < (CFG["min_score"] if min_score is None else min_score):
        return None
    return {"dir": d, "entry": entry, "sl": sl, "tp": tp, "risk_pips": risk / PIP,
            "atr_pips": atr_pips, "rsi": r, "trend": trend, "score": score}


def resolve(sig, bars_after):
    """Suit un signal sur les bougies suivantes.
    Retourne (statut, heure, resultat en R) ou None s'il est encore ouvert.
    Si stop et objectif sont touches dans la meme bougie, on compte la perte (prudent).
    Le spread est deduit du resultat."""
    risk = sig["risk_pips"]
    cost = CFG["spread_pips"] / risk
    rr = abs(sig["entry"] - sig["tp"]) / PIP / risk
    n = 0
    for b in bars_after:
        n += 1
        if sig["dir"] == "sell":
            hit_sl, hit_tp = b["h"] >= sig["sl"], b["l"] <= sig["tp"]
        else:
            hit_sl, hit_tp = b["l"] <= sig["sl"], b["h"] >= sig["tp"]
        if hit_sl:
            return ("loss", b["t"], -1.0 - cost)
        if hit_tp:
            return ("win", b["t"], rr - cost)
        if n >= CFG["max_bars_open"]:
            pnl = (sig["entry"] - b["c"] if sig["dir"] == "sell" else b["c"] - sig["entry"]) / PIP
            return ("expired", b["t"], pnl / risk - cost)
    return None


# -------------------------------------------------------------- base de donnees
def db_connect():
    con = sqlite3.connect(CFG["db"])
    con.execute("""CREATE TABLE IF NOT EXISTS signals(
        bar_time TEXT PRIMARY KEY, direction TEXT, entry REAL, sl REAL, tp REAL,
        risk_pips REAL, atr_pips REAL, rsi REAL, trend TEXT, score INTEGER,
        status TEXT DEFAULT 'open', closed_time TEXT, r_result REAL, created TEXT)""")
    return con


def db_stats(con):
    n, wins, avg_r, tot_r = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(status='win'),0), AVG(r_result), COALESCE(SUM(r_result),0) "
        "FROM signals WHERE status!='open'").fetchone()
    return n, wins, avg_r, tot_r


def export_csv(con):
    rows = con.execute("SELECT * FROM signals ORDER BY bar_time").fetchall()
    cols = [c[1] for c in con.execute("PRAGMA table_info(signals)").fetchall()]
    with open(CFG["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def iso(t):
    return t.strftime("%Y-%m-%d %H:%M")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------- messages
def lot_for(risk_pips):
    bal = CFG["balance"]
    if bal <= 0:
        return None
    target = bal * CFG["risk_pct"] / 100.0
    lot = math.floor(target / (risk_pips * PIP_USD_PER_LOT) * 100 + 1e-9) / 100.0
    if lot < 0.01:
        lot = 0.01
    risk_usd = lot * risk_pips * PIP_USD_PER_LOT
    return lot, risk_usd, risk_usd / bal * 100.0


def history_line(con):
    n, wins, avg_r, tot_r = db_stats(con)
    if n == 0:
        return "Historique du robot : aucun signal termine pour l'instant."
    return "Historique du robot : %d signaux termines, %.0f %% gagnants, %+.2f R par trade (total %+.1f R)." % (
        n, wins / n * 100, avg_r, tot_r)


def signal_message(s, bar_time, con):
    sell = s["dir"] == "sell"
    txt = "%s EURUSD (M15)\nBougie du %s UTC\n" % ("VENTE" if sell else "ACHAT", iso(bar_time))
    txt += "Entree ~%.5f (valable si le prix est a moins de 3 pips de ce niveau)\n" % s["entry"]
    txt += "Stop-loss %.5f | Take-profit %.5f\n" % (s["sl"], s["tp"])
    txt += "Risque %.1f pips | Gain vise %.1f pips\n" % (s["risk_pips"], s["risk_pips"] * CFG["rr"])
    lot = lot_for(s["risk_pips"])
    if lot:
        l, usd, pct = lot
        txt += "Lot conseille : %.2f (perte max ~%.2f $ = %.1f %% du solde)\n" % (l, usd, pct)
        if pct > max(CFG["risk_pct"] * 2, 3):
            txt += "ATTENTION : trop gros pour ton compte, passe ton tour.\n"
    txt += "Tendance H1 : %s | RSI %.0f | Ne prends pas le trade si ton spread depasse 3 pips\n" % (
        "baissiere" if s["trend"] == "down" else "haussiere", s["rsi"])
    txt += "Qualite du signal : %d/4\n" % s["score"]
    txt += history_line(con) + "\nMets le SL et le TP des l'ouverture. Aucun signal ne garantit un gain."
    return txt


def jam(h):
    return "%02dh" % ((h - 5) % 24)  # heure de la Jamaique (UTC-5, sans heure d'ete)


def write_status(con, m15, ind, trend, now, price, stale):
    """Ecrit status.json : c'est ce fichier que le site affiche."""
    i = len(m15) - 1
    b = m15[i]
    e, r, a = ind["ema"][i], ind["rsi"][i], ind["atr"][i]
    atr_pips = a / PIP if a else None
    session_ok = now.weekday() < 5 and CFG["session_start"] <= now.hour < CFG["session_end"]
    vol_ok = atr_pips is not None and 5 <= atr_pips <= 25
    today = iso(b["t"])[:10]
    n_today = con.execute("SELECT COUNT(*) FROM signals WHERE substr(bar_time,1,10)=?", (today,)).fetchone()[0]
    row = con.execute("SELECT bar_time,direction,entry,sl,tp,risk_pips,score,created "
                      "FROM signals WHERE status='open'").fetchone()
    active = None
    if row:
        active = dict(zip(["bar_time", "dir", "entry", "sl", "tp", "risk_pips", "score", "created"], row))
        active["rr"] = CFG["rr"]

    if stale:
        headline = "Données en retard ou marché fermé : le robot ne juge pas."
    elif active:
        headline = "Un signal est ouvert : suis-le jusqu'au stop ou à l'objectif."
    elif not session_ok:
        headline = "Hors session : le robot cherche des signaux de %s à %s (heure de la Jamaïque), du lundi au vendredi." % (
            jam(CFG["session_start"]), jam(CFG["session_end"]))
    elif n_today >= CFG["max_per_day"]:
        headline = "Maximum de %d signaux atteint aujourd'hui : on s'arrête là." % CFG["max_per_day"]
    elif not vol_ok:
        headline = "Volatilité anormale (%.0f pips en moyenne) : on attend que ça se calme." % (atr_pips or 0)
    elif trend == "down":
        headline = "Tendance H1 baissière. J'attends un rebond vers %.5f (moyenne mobile), puis une bougie M15 rouge qui clôture en dessous." % e
    elif trend == "up":
        headline = "Tendance H1 haussière. J'attends un repli vers %.5f (moyenne mobile), puis une bougie M15 verte qui clôture au-dessus." % e
    else:
        headline = "Tendance H1 pas claire : pas de trade tant que le marché n'a pas choisi."

    recent = [dict(zip(["bar_time", "dir", "status", "r", "entry", "sl", "tp", "score"], x)) for x in con.execute(
        "SELECT bar_time,direction,status,r_result,entry,sl,tp,score FROM signals ORDER BY bar_time DESC LIMIT 40").fetchall()]
    n, wins, avg_r, tot_r = db_stats(con)
    out = {
        "demo": False,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bar_time": iso(b["t"]),
        "stale": bool(stale),
        "context": {"price": round(price, 5), "ema20": round(e, 5), "rsi": round(r, 1),
                    "atr_pips": round(atr_pips, 1) if atr_pips is not None else None,
                    "trend": trend if trend in ("up", "down") else "none",
                    "session_ok": bool(session_ok), "vol_ok": bool(vol_ok), "headline": headline},
        "active": active,
        "recent": recent,
        "stats": {"n": n, "wins": int(wins), "avg_r": round(avg_r, 3) if avg_r is not None else None,
                  "total_r": round(tot_r, 2)},
        "signals_today": n_today,
        "max_per_day": CFG["max_per_day"],
        "min_score": CFG["min_score"],
        "spread_pips": CFG["spread_pips"],
    }
    tmp = CFG["status"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CFG["status"])


# --------------------------------------------------------------------- mode live
def run_live(now=None):
    now = now or datetime.now(timezone.utc)
    m15 = closed_only(fetch_bars("15min", 600), 15, now)
    h1 = closed_only(fetch_bars("1h", 500), 60, now)
    if len(m15) < 60 or len(h1) < 220:
        raise RuntimeError("Pas assez de bougies pour calculer les indicateurs.")
    con = db_connect()

    # 1) mise a jour des signaux ouverts
    for bar_time, d, entry, sl, tp, risk in con.execute(
            "SELECT bar_time, direction, entry, sl, tp, risk_pips FROM signals WHERE status='open'").fetchall():
        t0 = parse_iso(bar_time)
        sig = {"dir": d, "entry": entry, "sl": sl, "tp": tp, "risk_pips": risk}
        res = resolve(sig, [b for b in m15 if b["t"] > t0])
        if res:
            status, t_close, r = res
            con.execute("UPDATE signals SET status=?, closed_time=?, r_result=? WHERE bar_time=?",
                        (status, iso(t_close), round(r, 3), bar_time))
            con.commit()
            label = {"win": "GAGNANT", "loss": "PERDANT", "expired": "EXPIRE"}[status]
            notify("Signal du %s (%s) termine : %s (%+.2f R, spread deduit).\n%s" % (
                bar_time, "VENTE" if d == "sell" else "ACHAT", label, r, history_line(con)))

    # 2) nouveau signal sur la derniere bougie fermee
    i = len(m15) - 1
    bar_end = m15[i]["t"] + timedelta(minutes=15)
    ind = indicators(m15)
    trend = make_trend_lookup(h1)(bar_end)
    stale = (now - bar_end).total_seconds() > CFG["max_data_age_min"] * 60
    today = iso(m15[i]["t"])[:10]
    n_today = con.execute("SELECT COUNT(*) FROM signals WHERE substr(bar_time,1,10)=?", (today,)).fetchone()[0]
    has_open = con.execute("SELECT 1 FROM signals WHERE status='open'").fetchone() is not None
    if stale:
        print("Donnees trop anciennes (marche ferme ou retard) : pas d'analyse.")
    elif has_open:
        print("Un signal est deja ouvert : pas de nouveau signal.")
    elif n_today >= CFG["max_per_day"]:
        print("Maximum de %d signaux atteint aujourd'hui." % CFG["max_per_day"])
    else:
        s = check_bar(m15, i, ind, trend)
        if s:
            cur = con.execute(
                "INSERT OR IGNORE INTO signals(bar_time,direction,entry,sl,tp,risk_pips,atr_pips,rsi,trend,score,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (iso(m15[i]["t"]), s["dir"], round(s["entry"], 5), round(s["sl"], 5), round(s["tp"], 5),
                 round(s["risk_pips"], 1), round(s["atr_pips"], 1), round(s["rsi"], 1), s["trend"], s["score"], iso(now)))
            con.commit()
            if cur.rowcount == 1:
                notify(signal_message(s, m15[i]["t"], con))
        else:
            print("Pas de signal sur la bougie %s." % iso(m15[i]["t"]))
    try:
        price = fetch_price()
    except Exception as e:  # noqa: BLE001
        print("Prix en direct indisponible :", e)
        price = m15[i]["c"]
    write_status(con, m15, ind, trend, now, price, stale)
    export_csv(con)
    con.close()


# ------------------------------------------------------------------- backtest
def backtest(m15, h1, min_score=None):
    ind = indicators(m15)
    look = make_trend_lookup(h1)
    index_of = {b["t"]: k for k, b in enumerate(m15)}
    results, skip_until, per_day = [], -1, {}
    for i in range(60, len(m15) - 1):
        if i <= skip_until:
            continue
        day = m15[i]["t"].strftime("%Y-%m-%d")
        if per_day.get(day, 0) >= CFG["max_per_day"]:
            continue
        s = check_bar(m15, i, ind, look(m15[i]["t"] + timedelta(minutes=15)), min_score)
        if not s:
            continue
        res = resolve(s, m15[i + 1:])
        if res is None:
            continue
        status, t_close, r = res
        skip_until = index_of.get(t_close, i)  # un seul trade a la fois
        per_day[day] = per_day.get(day, 0) + 1
        results.append((m15[i]["t"], s["dir"], status, r))
    return results


def summarize(results, n_bars, days):
    n = len(results)
    if n == 0:
        return "Backtest : aucun signal sur %d bougies M15 (~%d jours)." % (n_bars, days)
    rs = [r for *_, r in results]
    wins = sum(1 for x in results if x[2] == "win")
    gain = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)
    pf = gain / loss if loss > 0 else float("inf")
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    half = n // 2
    h1_avg = sum(rs[:half]) / half if half else 0.0
    h2_avg = sum(rs[half:]) / (n - half)
    txt = ("Backtest EURUSD M15 : ~%d jours, %d trades\n"
           "Gagnants : %.0f %% | Resultat moyen : %+.2f R par trade | Total : %+.1f R\n"
           "Facteur de profit : %s | Pire serie perdante cumulee : %.1f R\n"
           "Premiere moitie : %+.2f R | Seconde moitie : %+.2f R\n"
           "(spread de %.1f pips deduit, stop prioritaire si les deux niveaux sont touches)\n") % (
        days, n, wins / n * 100, sum(rs) / n, sum(rs), ("%.2f" % pf) if pf != float("inf") else "n/a", dd,
        h1_avg, h2_avg, CFG["spread_pips"])
    if n < 30:
        txt += "CONCLUSION : trop peu de trades (%d) pour juger. N'utilise pas ces signaux avec de l'argent reel.\n" % n
    elif sum(rs) / n <= 0:
        txt += "CONCLUSION : la strategie perd apres spread sur cette periode. N'utilise pas ces signaux avec de l'argent reel.\n"
    elif h1_avg <= 0 or h2_avg <= 0:
        txt += "CONCLUSION : resultat instable (une des deux moities est negative). Reste en DEMO.\n"
    else:
        txt += "CONCLUSION : positif sur cette periode, mais le passe ne garantit rien. Valide d'abord en DEMO plusieurs semaines.\n"
    return txt


def run_backtest():
    now = datetime.now(timezone.utc)
    m15 = closed_only(fetch_bars("15min", 5000), 15, now)
    h1 = closed_only(fetch_bars("1h", 5000), 60, now)
    days = max(1, (m15[-1]["t"] - m15[0]["t"]).days)
    base = summarize(backtest(m15, h1), len(m15), days)
    rows = []
    for ms in (0, 2, 3, 4):
        res = backtest(m15, h1, ms)
        n = len(res)
        if n:
            rs = [x[3] for x in res]
            rows.append("Qualite >= %d : %d trades, %.0f %% gagnants, %+.2f R par trade" % (
                ms, n, sum(1 for x in res if x[2] == "win") / n * 100, sum(rs) / n))
        else:
            rows.append("Qualite >= %d : 0 trade" % ms)
    notify(base + "Selon le filtre de qualite (reglage actuel : %d) :\n" % CFG["min_score"] + "\n".join(rows))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "backtest":
            run_backtest()
        else:
            run_live()
    except Exception as e:  # noqa: BLE001
        print("ERREUR :", e)
        sys.exit(1)
