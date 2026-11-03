import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
DEBUG = os.environ.get("DEBUG", "0") == "1"
BEH_MINUT = int(os.environ.get("BEH_MINUT", "0"))  # 0 = beží donekonečna

MIN_OD, MIN_DO = 15, 20
MAX_STRIEL_NA_BRANU = 1

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
HLAVICKY = {"User-Agent": "Mozilla/5.0"}

odoslane = set()


def log(*a):
    print(*a, flush=True)


def posli_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("Chýba TELEGRAM_TOKEN alebo CHAT_ID")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            log("Telegram chyba:", r.status_code, r.text)
    except Exception as e:
        log("Telegram výnimka:", type(e).__name__)


def get_json(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HLAVICKY, timeout=20)
    except Exception as e:
        if DEBUG:
            log("Požiadavka zlyhala:", type(e).__name__, url)
        return None
    if r.status_code != 200:
        if DEBUG:
            log("HTTP", r.status_code, url)
        return None
    try:
        return r.json()
    except ValueError:
        return None


def cislo(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def minuta(status):
    m = re.match(r"(\d+)", str(status.get("displayClock", "")))
    return int(m.group(1)) if m else 0


def stiahni_vsetky_zapasy():
    """Stiahne scoreboard pre všetky ligy (dnes aj včera kvôli polnoci).
    Vráti zoznam dvojíc (udalosť, slug_ligy)."""
    dnes = datetime.now(timezone.utc)
    datumy = [None, (dnes - timedelta(days=1)).strftime("%Y%m%d")]
    videne = set()
    vysledok = []

    for d in datumy:
        params = {"limit": 1000}
        if d:
            params["dates"] = d
        data = get_json(f"{BASE}/all/scoreboard", params)
        if not data:
            continue

        # mapa: ID ligy -> slug ligy
        slug_podla_id = {}
        for l in data.get("leagues", []) or []:
            if l.get("id") and l.get("slug"):
                slug_podla_id[str(l["id"])] = l["slug"]

        for ev in data.get("events", []) or []:
            eid = ev.get("id")
            if eid in videne:
                continue
            videne.add(eid)

            slug = None
            m = re.search(r"l:(\d+)", str(ev.get("uid", "")))
            if m:
                slug = slug_podla_id.get(m.group(1))
            vysledok.append((ev, slug))
    return vysledok


def cervene_karty(comp):
    return sum(1 for d in comp.get("details", []) or [] if d.get("redCard"))


def strely_z_competitorov(tymy):
    """Skúsi nájsť strely na bránu priamo v scoreboarde."""
    spolu, najdene = 0, False
    for t in tymy:
        for s in t.get("statistics", []) or []:
            if str(s.get("name", "")).lower() in ("shotsontarget", "ontargetshots"):
                spolu += cislo(s.get("displayValue", s.get("value")))
                najdene = True
    return spolu if najdene else None


def strely_zo_summary(summary):
    spolu, najdene = 0, False
    for t in (summary.get("boxscore") or {}).get("teams", []) or []:
        for s in t.get("statistics", []) or []:
            if str(s.get("name", "")).lower() in ("shotsontarget", "ontargetshots"):
                spolu += cislo(s.get("displayValue", s.get("value")))
                najdene = True
    return spolu if najdene else None


def nacitaj_summary(eid, slug):
    kandidati = [slug] if slug else []
    kandidati.append("all")
    for k in kandidati:
        s = get_json(f"{BASE}/{k}/summary", {"event": eid})
        if s:
            return s
    return None


def skenuj(ev, slug):
    eid = ev.get("id")
    if eid in odoslane:
        return

    status = ev.get("status", {})
    if (status.get("type") or {}).get("state") != "in":
        return
    if status.get("period") != 1:
        return

    m = minuta(status)
    if not (MIN_OD <= m <= MIN_DO):
        return

    comp = (ev.get("competitions") or [{}])[0]
    tymy = comp.get("competitors", []) or []
    if len(tymy) != 2:
        return

    if sum(cislo(t.get("score")) for t in tymy) != 0:
        return

    home = next((t for t in tymy if t.get("homeAway") == "home"), tymy[0])
    away = next((t for t in tymy if t.get("homeAway") == "away"), tymy[1])
    nazov = (
        f"{(home.get('team') or {}).get('displayName', 'Domáci')} vs "
        f"{(away.get('team') or {}).get('displayName', 'Hostia')}"
    )

    if cervene_karty(comp) > 0:
        if DEBUG:
            log(f"[{nazov}] vyradený: červená karta")
        return

    strely = strely_z_competitorov(tymy)
    if strely is None:
        summary = nacitaj_summary(eid, slug) or {}
        strely = strely_zo_summary(summary)

    if strely is None:
        strely_text = "neznáme, skontroluj ručne"
    elif strely > MAX_STRIEL_NA_BRANU:
        if DEBUG:
            log(f"[{nazov}] vyradený: strely na bránu {strely}")
        return
    else:
        strely_text = str(strely)

    liga = slug or "?"
    msg = (
        f"⚽ *STRATÉGIA 1*\n"
        f"🏆 {nazov}\n"
        f"🌍 Liga: {liga}\n"
        f"⏱ Minúta: {m}' | Stav: 0:0\n"
        f"📊 Strely na bránu: {strely_text}\n"
        f"🔥 Skontroluj kurz Under 1.5 (1. polčas) v Tipsporte!"
    )
    posli_telegram(msg)
    odoslane.add(eid)
    log("Odoslané:", nazov)


def ladenie(zapasy):
    live = [(e, s) for e, s in zapasy if (e.get("status", {}).get("type") or {}).get("state") == "in"]
    log(f"--- Zápasov spolu: {len(zapasy)}, live: {len(live)} ---")
    for ev, slug in live[:8]:
        st = ev.get("status", {})
        comp = (ev.get("competitions") or [{}])[0]
        skore = [t.get("score") for t in comp.get("competitors", [])]
        log(f"{slug} | {ev.get('name')} | clock {st.get('displayClock')} | "
            f"perioda {st.get('period')} | skóre {skore} | červené {cervene_karty(comp)}")
    if live:
        ev, slug = live[0]
        summ = nacitaj_summary(ev.get("id"), slug) or {}
        nazvy = sorted({
            str(s.get("name"))
            for t in (summ.get("boxscore") or {}).get("teams", []) or []
            for s in t.get("statistics", []) or []
        })
        log("Štatistiky v summary (prvý live zápas):", nazvy)


def main():
    log("Bot štartuje (ESPN, všetky ligy)...")
    posli_telegram("🚀 Bot bol spustený (ESPN, všetky ligy).")
    koniec = time.time() + BEH_MINUT * 60 if BEH_MINUT else None
    den = time.strftime("%Y-%m-%d")

    while True:
        if koniec and time.time() >= koniec:
            log("Čas behu vypršal, končím.")
            return
        zaciatok = time.time()
        if time.strftime("%Y-%m-%d") != den:
            odoslane.clear()
            den = time.strftime("%Y-%m-%d")
        try:
            zapasy = stiahni_vsetky_zapasy()
            if DEBUG:
                ladenie(zapasy)
            for ev, slug in zapasy:
                skenuj(ev, slug)
        except Exception as e:
            log("Chyba:", type(e).__name__, e)
        time.sleep(max(5, 60 - (time.time() - zaciatok)))


if __name__ == "__main__":
    main()
