!/usr/bin/env python3
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

import json
import os
import re
import sys
import time
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

# Link, der in der "Tracker aktiv"-Info-Nachricht geöffnet wird
CATEGORY_LINK = "https://www.netto-online.de/sammelkarten/c-N06081404"

# ---------------------------------------------------------------------------
# Erkennungs-Muster
# ---------------------------------------------------------------------------
# Produkt-Links auf netto-online.de sehen so aus:  /<slug>/p-<zahl>
PRODUCT_RE = re.compile(r"https://www\.netto-online\.de/([a-z0-9\-]+)/p-(\d+)", re.I)
# Pokémon erkennt man am Slug: "pok-mon-...", "pokemon-...", "pokémon-..."
POKEMON_RE = re.compile(r"pok[\-eé]?mon", re.I)
# Eindeutiger Ausverkauft-Marker auf der Produktseite
SOLD_OUT_RE = re.compile(r"aktuell ausverkauft", re.I)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def fetch(url, timeout=30):
    """Lädt eine Seite und gibt den HTML-Text zurück."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "de-DE,de;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def slug_to_name(slug):
    """Macht aus einem URL-Slug einen lesbaren Produktnamen."""
    name = slug.replace("-", " ")
    name = re.sub(r"\bpokemon\b", "Pokémon", name, flags=re.I)
    name = re.sub(r"\bpok mon\b", "Pokémon", name, flags=re.I)
    # doppeltes "Pokémon Pokémon" entfernen
    name = re.sub(r"(Pokémon)(\s+Pokémon)+", r"\1", name)
    name = re.sub(r"\s+", " ", name).strip()
    words = []
    for w in name.split():
        words.append(w if w == "Pokémon" else w.capitalize())
    return " ".join(words)


def find_pokemon_products(html):
    """Findet alle Pokémon-Produkte auf einer Seite. Rückgabe: {sku: slug}."""
    found = {}
    for m in PRODUCT_RE.finditer(html):
        slug, sku = m.group(1), m.group(2)
        if POKEMON_RE.search(slug):
            found.setdefault(sku, slug)
    return found


def is_buyable(url):
    """
    True  = Produkt ist kaufbar
    False = ausverkauft
    None  = konnte nicht geprüft werden (Netzwerkfehler)
    """
    try:
        html = fetch(url)
    except Exception as e:
        print(f"  ! Produktseite nicht ladbar ({e})", file=sys.stderr)
        return None
    return SOLD_OUT_RE.search(html) is None


def notify(title, message, click_url=None, tags=None, priority=5):
    """Schickt eine Push-Nachricht über ntfy (JSON-Format, UTF-8-sicher)."""
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
    """Lädt den gespeicherten Zustand. Rückgabe: (state, had_state)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8")), True
        except Exception:
            return {}, True  # kaputte Datei -> als vorhanden behandeln, kein Spam
    return {}, False


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------
def main():
    state, had_state = load_state()
    # state-Schema:  { sku: {"slug": str, "buyable": bool, "last_seen": int} }

    products = {}
    for url in CATEGORY_URLS:
        try:
            products.update(find_pokemon_products(fetch(url)))
        except Exception as e:
            print(f"! Kategorie-Seite Fehler ({url}): {e}", file=sys.stderr)

    if not products:
        # Nichts gefunden -> Seite evtl. geändert. Zustand NICHT überschreiben,
        # damit es keinen Fehlalarm beim nächsten Lauf gibt.
        print("Keine Pokémon-Produkte gefunden (Seitenstruktur geändert?).",
              file=sys.stderr)
        return 1

    now = int(time.time())
    listed = set(products.keys())
    newly_available = []

    for sku, slug in products.items():
        url = f"https://www.netto-online.de/{slug}/p-{sku}"
        prev = state.get(sku)
        was_buyable = bool(prev and prev.get("buyable"))

        if was_buyable:
            # schon als kaufbar bekannt -> nur Zeitstempel auffrischen
            state[sku] = {"slug": slug, "buyable": True, "last_seen": now}
            continue

        # neu ODER vorher ausverkauft -> auf der Produktseite gegenprüfen
        buyable = is_buyable(url)
        if buyable is None:
            if prev is None:
                state[sku] = {"slug": slug, "buyable": False, "last_seen": now}
            continue

        state[sku] = {"slug": slug, "buyable": buyable, "last_seen": now}
        if buyable and not was_buyable:
            newly_available.append((sku, slug, url))
        time.sleep(1)  # fair zu Nettos Servern

    # Artikel, die nicht mehr gelistet sind -> als nicht kaufbar markieren
    for sku in list(state.keys()):
        if sku not in listed and state[sku].get("buyable"):
            state[sku]["buyable"] = False

    save_state(state)

    # Erstlauf: kein Spam, nur eine kurze Bestätigung
    if not had_state:
        anzahl = sum(1 for s in products if state[s].get("buyable"))
        notify("✅ Netto-Pokémon-Tracker aktiv",
               f"Ich beobachte jetzt die Sammelkarten-Kategorie. "
               f"Aktuell kaufbar: {anzahl} Pokémon-Artikel.",
               click_url=CATEGORY_LINK, tags=["white_check_mark"], priority=3)
        print(f"Erstlauf ok. {len(products)} Pokémon-Artikel erfasst, "
              f"{anzahl} davon kaufbar.")
        return 0

    for sku, slug, url in newly_available:
        name = slug_to_name(slug)
        notify("🔴 Pokémon bei Netto kaufbar!",
               f"{name}\nJetzt im Netto Online-Shop verfügbar – schnell sein!",
               click_url=url, tags=["rotating_light"], priority=5)

    print(f"Fertig. {len(products)} Pokémon-Artikel geprüft, "
          f"{len(newly_available)} neu verfügbar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
