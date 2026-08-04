#!/usr/bin/env python3
"""
Pokémon Verfügbarkeits-Tracker – Netto + Rossmann
-------------------------------------------------
Prüft die Pokémon-Artikel bei netto-online.de UND rossmann.de auf
Verfügbarkeit und schickt bei neuer Kaufbarkeit eine ntfy-Push.

Nur Python-Standardbibliothek nötig (kein pip install).

Umgebungsvariablen:
  NTFY_TOPIC   (Pflicht)  dein ntfy-Topic
  NTFY_SERVER  (optional)  Standard: https://ntfy.sh
  NTFY_TOKEN   (optional)  nur bei passwortgeschütztem Topic
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

# Pokémon erkennt man am URL-Slug: "pok-mon", "pokemon", "pokémon"
POKEMON_RE = re.compile(r"pok[\-e\u00e9]?mon", re.I)

# Anzeichen für eine Sperr-/Bot-Seite
BLOCK_HINTS = ("captcha", "access denied", "zugriff verweigert", "just a moment",
               "cloudflare", "are you a robot", "bot detection", "forbidden")

# --- Shop-Definitionen -----------------------------------------------------
SHOPS = [
    {
        "id": "netto",
        "name": "Netto",
        "listings": [
            "https://www.netto-online.de/sammelkarten/c-N06081404",
        ],
        "product_re": re.compile(
            r"https://www\.netto-online\.de/([a-z0-9\-]+)/p-(\d+)", re.I),
        "url_tmpl": "https://www.netto-online.de/{slug}/p-{sku}",
        "sold_out_re": re.compile(r"aktuell ausverkauft", re.I),
        "link": "https://www.netto-online.de/sammelkarten/c-N06081404",
    },
    {
        "id": "rossmann",
        "name": "Rossmann",
        "listings": [
            "https://www.rossmann.de/de/search/?text=pokemon&pageSize=100",
            "https://www.rossmann.de/de/search?text=pokemon",
            "https://www.rossmann.de/de/alle-marken/amigo/c/online-dachmarke_5549699?pageSize=100",
        ],
        "product_re": re.compile(
            r"https://www\.rossmann\.de/de/([a-z0-9\-]+)/p/(\d+)", re.I),
        "url_tmpl": "https://www.rossmann.de/de/{slug}/p/{sku}",
        "sold_out_re": re.compile(r"momentan nicht verf", re.I),  # "...verfügbar"
        "link": "https://www.rossmann.de/de/search/?text=pokemon",
    },
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Netzwerk
# ---------------------------------------------------------------------------
def fetch(url, timeout=30, attempts=3):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
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
    cased = " ".join(w if w == "Pok\u00e9mon" else w.capitalize() for w in name.split())
    # Kategorie-/Marken-Vorspann abschneiden: ab dem ersten "Pokémon" beginnen
    idx = cased.find("Pok\u00e9mon")
    return cased[idx:] if idx > 0 else cased


def find_products(html, shop):
    """{sku: slug} aller Pokémon-Produkte auf einer Seite dieses Shops."""
    found = {}
    for m in shop["product_re"].finditer(html):
        slug, sku = m.group(1), m.group(2)
        if POKEMON_RE.search(slug):
            found.setdefault(sku, slug)
    return found


def count_all(html, shop):
    return len(set(m.group(2) for m in shop["product_re"].finditer(html)))


def looks_blocked(html):
    low = html.lower()
    return any(w in low for w in BLOCK_HINTS)


def is_buyable(shop, url):
    """True = kaufbar, False = ausverkauft, None = nicht prüfbar."""
    try:
        html = fetch(url)
    except Exception as e:
        print(f"  ! Produktseite nicht ladbar ({e})", file=sys.stderr)
        return None
    return shop["sold_out_re"].search(html) is None


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
            data = json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    # nur Shop-namespaced Schlüssel behalten ("shop:sku")
    return {k: v for k, v in data.items() if ":" in k}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Ein Shop
# ---------------------------------------------------------------------------
def process_shop(shop, state):
    shop_id = shop["id"]
    shop_known = any(k.startswith(shop_id + ":") for k in state)
    print(f"\n=== {shop['name']} ===")

    products = {}
    for url in shop["listings"]:
        print(f"Lade Liste: {url}")
        try:
            html = fetch(url)
        except Exception as e:
            print(f"! Liste nicht ladbar: {e}", file=sys.stderr)
            continue
        total = count_all(html, shop)
        pk = find_products(html, shop)
        print(f"[Diagnose] {total} Produktlinks, {len(pk)} davon Pokémon.")
        if total == 0 and looks_blocked(html):
            print(f"[Diagnose] {shop['name']} sieht nach Sperre/Bot-Schutz aus "
                  "(evtl. GitHub-IP blockiert).", file=sys.stderr)
        products.update(pk)

    if not products:
        print(f"Keine Pokémon-Produkte bei {shop['name']} – Zustand unverändert.",
              file=sys.stderr)
        return

    now = int(time.time())
    listed = set(products.keys())
    newly = []

    for sku, slug in products.items():
        key = f"{shop_id}:{sku}"
        url = shop["url_tmpl"].format(slug=slug, sku=sku)
        prev = state.get(key)
        was_buyable = bool(prev and prev.get("buyable"))
        if was_buyable:
            state[key] = {"slug": slug, "buyable": True, "last_seen": now}
            continue
        buyable = is_buyable(shop, url)
        if buyable is None:
            if prev is None:
                state[key] = {"slug": slug, "buyable": False, "last_seen": now}
            continue
        state[key] = {"slug": slug, "buyable": buyable, "last_seen": now}
        if buyable and not was_buyable and shop_known:
            newly.append((slug, url))
        time.sleep(1)

    # nicht mehr gelistete Artikel dieses Shops -> nicht kaufbar
    for key in list(state.keys()):
        if key.startswith(shop_id + ":") and key.split(":", 1)[1] not in listed:
            if state[key].get("buyable"):
                state[key]["buyable"] = False

    if not shop_known:
        anzahl = sum(1 for s in products if state[f"{shop_id}:{s}"].get("buyable"))
        notify(f"✅ {shop['name']}-Tracking aktiv",
               f"Ich beobachte jetzt Pokémon bei {shop['name']}. "
               f"Aktuell kaufbar: {anzahl}.",
               click_url=shop["link"], tags=["white_check_mark"], priority=3)
        print(f"{shop['name']}: Erstlauf, {len(products)} Artikel, {anzahl} kaufbar.")
    else:
        for slug, url in newly:
            notify(f"🔴 Pokémon bei {shop['name']} kaufbar!",
                   f"{slug_to_name(slug)}\nJetzt verfügbar – schnell sein!",
                   click_url=url, tags=["rotating_light"], priority=5)
        print(f"{shop['name']}: {len(products)} Artikel, {len(newly)} neu verfügbar.")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    any_ok = False
    for shop in SHOPS:
        before = len(state)
        try:
            process_shop(shop, state)
            any_ok = True
        except Exception as e:
            print(f"!! Fehler bei {shop['name']}: {e}", file=sys.stderr)
        _ = before
    save_state(state)
    return 0 if any_ok else 1


if __name__ == "__main__":
    sys.exit(main())
