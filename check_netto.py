#!/usr/bin/env python3
"""
Netto Pokémon Verfügbarkeits-Tracker
------------------------------------
Prüft die Sammelkarten-Kategorie von netto-online.de auf KAUFBARE Pokémon-Artikel
und schickt bei neuer Verfügbarkeit eine Push-Nachricht über ntfy.

Braucht nur die Python-Standardbibliothek (kein pip install nötig).

Konfiguration über Umgebungsvariablen:
  NTFY_TOPIC   (Pflicht)  dein ntfy-Topic, z.B. "netto-pkm-Xy9f2Kq"
  NTFY_SERVER  (optional)  Standard: https://ntfy.sh
  NTFY_TOKEN   (optional)  nur nötig, wenn dein Topic passwortgeschützt ist
  STATE_FILE   (optional)  Standard: state.json
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = (os.environ.get("NTFY_TOPIC") or "").strip()
NTFY_TOKEN = (os.environ.get("NTFY_TOKEN") or "").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE") or "state.json")

# Diese Kategorie-Seite(n) werden durchsucht. Weitere kannst du einfach ergänzen.
CATEGORY_URLS = [
    "https://www.netto-online.de/sammelkarten/c-N06081404",
]
CATEGORY_LINK = "https://www.netto-online.de/sammelkarten/c-N06081404"

# ---------------------------------------------------------------------------
# Erkennungs-Muster
# ---------------------------------------------------------------------------
PRODUCT_RE = re.compile(r"https://www\.netto-online\.de/([a-z0-9\-]+)/p-(\d+)", re.I)
POKEMON_RE = re.compile(r"pok[\-e\u00e9]?mon", re.I)  # pok-mon / pokemon / pokémon
SOLD_OUT_RE = re.compile(r"aktuell ausverkauft", re.I)

# Anzeichen für eine Sperr-/Consent-/Bot-Seite (falls Netto die IP abweist)
BLOCK_HINTS = ("captcha", "access denied", "zugriff verweigert", "just a moment",
               "cloudflare", "are you a robot", "bot detection", "forbidden")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Netzwerk
# ---------------------------------------------------------------------------
def fetch(url, timeout=30, attempts=3):
    """Lädt eine Seite (mit Wiederholversuchen) und gibt HTML-Text zurück."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Referer": "https://www.netto-online.de/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    last_err = None
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                status = getattr(r, "status", 200)
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
            print(f"  [ok] HTTP {status}, {len(text)} Zeichen (Versuch {i})")
            return text
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            print(f"  [!] Versuch {i}: {last_err} bei {url}", file=sys.stderr)
        except Exception as e:
            last_err = str(e)
            print(f"  [!] Versuch {i}: {last_err} bei {url}", file=sys.stderr)
        time.sleep(2 * i)
    raise RuntimeError(last_err or "unbekannter Fehler")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def slug_to_name(slug):
    name = slug.replace("-", " ")
    name = re.sub(r"\bpokemon\b", "Pok\u00e9mon", name, flags=re.I)
    name = re.sub(r"\bpok mon\b", "Pok\u00e9mon", name, flags=re.I)
    name = re.sub(r"(Pok\u00e9mon)(\s+Pok\u00e9mon)+", r"\1", name)
    name = re.sub(r"\s+", " ", name).strip()
    return " ".join(w if w == "Pok\u00e9mon" else w.capitalize() for w in name.split())


def find_pokemon_products(html):
    """{sku: slug} aller Pokémon-Produkte auf einer Seite."""
    found = {}
    for m in PRODUCT_RE.finditer(html):
        slug, sku = m.group(1), m.group(2)
        if POKEMON_RE.search(slug):
            found.setdefault(sku, slug)
    return found


def count_all_products(html):
    return len(set(m.group(2) for m in PRODUCT_RE.finditer(html)))


def looks_blocked(html):
    low = html.lower()
    return any(w in low for w in BLOCK_HINTS)


def is_buyable(url):
    """True = kaufbar, False = ausverkauft, None = nicht prüfbar."""
    try:
        html = fetch(url)
    except Exception as e:
        print(f"  ! Produktseite nicht ladbar ({e})", file=sys.stderr)
        return None
    return SOLD_OUT_RE.search(html) is None


def notify(title, message, click_url=None, tags=None, priority=5):
    if not NTFY_TOPIC:
        print("!! NTFY_TOPIC nicht gesetzt – keine Benachrichtigung", file=sys.stderr)
        return
    payload = {"topic": NTFY_TOPIC, "title": title,
               "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if click_url:
        payload["click"] = click_url
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(
        NTFY_SERVER + "/", data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        print(f"  -> Push gesendet: {title}")
    except Exception as e:
        print(f"!! Push fehlgeschlagen: {e}", file=sys.stderr)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8")), True
        except Exception:
            return {}, True
    return {}, False


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------
def main():
    state, had_state = load_state()
    products = {}
    saw_block = False

    for url in CATEGORY_URLS:
        print(f"Lade Kategorie: {url}")
        try:
            html = fetch(url)
        except Exception as e:
            print(f"! Kategorie nicht ladbar: {e}", file=sys.stderr)
            continue
        total = count_all_products(html)
        pk = find_pokemon_products(html)
        print(f"[Diagnose] {total} Produktlinks insgesamt, {len(pk)} davon Pokémon.")
        if total == 0 and looks_blocked(html):
            saw_block = True
            print("[Diagnose] Seite sieht nach Sperre/Bot-Schutz aus – Netto blockt "
                  "vermutlich die GitHub-Server-IP.", file=sys.stderr)
        elif total == 0:
            print("[Diagnose] Keine Produktlinks – evtl. Consent-Seite oder geänderte "
                  "Struktur. Erste 300 Zeichen:", file=sys.stderr)
            print("   " + " ".join(html[:300].split()), file=sys.stderr)
        products.update(pk)

    if not products:
        print("Keine Pokémon-Produkte gefunden – Zustand wird NICHT verändert.", file=sys.stderr)
        if saw_block and not had_state:
            notify("⚠️ Netto-Tracker: Zugriff geblockt",
                   "Netto scheint die GitHub-IP zu sperren. Sag Claude Bescheid – "
                   "wir stellen dann auf eine andere Methode um.",
                   tags=["warning"], priority=4)
        return 1

    now = int(time.time())
    listed = set(products.keys())
    newly_available = []

    for sku, slug in products.items():
        url = f"https://www.netto-online.de/{slug}/p-{sku}"
        prev = state.get(sku)
        was_buyable = bool(prev and prev.get("buyable"))
        if was_buyable:
            state[sku] = {"slug": slug, "buyable": True, "last_seen": now}
            continue
        buyable = is_buyable(url)
        if buyable is None:
            if prev is None:
                state[sku] = {"slug": slug, "buyable": False, "last_seen": now}
            continue
        state[sku] = {"slug": slug, "buyable": buyable, "last_seen": now}
        if buyable and not was_buyable:
            newly_available.append((sku, slug, url))
        time.sleep(1)

    for sku in list(state.keys()):
        if sku not in listed and state[sku].get("buyable"):
            state[sku]["buyable"] = False

    save_state(state)

    if not had_state:
        anzahl = sum(1 for s in products if state[s].get("buyable"))
        notify("✅ Netto-Pokémon-Tracker aktiv",
               f"Ich beobachte jetzt die Sammelkarten-Kategorie. "
               f"Aktuell kaufbar: {anzahl} Pokémon-Artikel.",
               click_url=CATEGORY_LINK, tags=["white_check_mark"], priority=3)
        print(f"Erstlauf ok. {len(products)} Pokémon-Artikel, {anzahl} kaufbar.")
        return 0

    for sku, slug, url in newly_available:
        notify("🔴 Pokémon bei Netto kaufbar!",
               f"{slug_to_name(slug)}\nJetzt im Netto Online-Shop verfügbar – schnell sein!",
               click_url=url, tags=["rotating_light"], priority=5)

    print(f"Fertig. {len(products)} Pokémon-Artikel, {len(newly_available)} neu verfügbar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
