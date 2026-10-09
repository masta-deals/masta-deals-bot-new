"""Masta Deals Bot (halbautomatisch)

Sucht Amazon.de-Angebote über die Creators API, filtert nach Rabatt,
vermeidet Doppelposts und schickt fertige Posts per Telegram an dich.
Du leitest sie mit einem Tipp in deinen WhatsApp-Kanal weiter.

Start:      python bot.py
Nur testen: python bot.py --dry-run   (druckt die Posts, sendet nichts)
"""
import html
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from amazon_creatorsapi import AmazonCreatorsApi, Country
from amazon_creatorsapi.models import SearchItemsResource

# ----------------------------------------------------------------------
# Einstellungen (hier anpassen)
# ----------------------------------------------------------------------
PARTNER_TAG = os.environ.get("PARTNER_TAG") or "mikesaffili0f-21"

# Suchbegriffe, gemischt aus vielen Bereichen. Du kannst sie frei ändern, löschen
# oder ergänzen. Der Bot mischt sie bei jedem Lauf neu durch.
KEYWORDS = [
    # Haushalt & Küche
    "Pfanne", "Küchenmesser", "Bettwäsche", "Whiskey", "Kaffeemaschine",
    "Vorratsdosen", "Handtücher",
    # Mode & Schuhe
    "Sneaker Herren", "Jogginghose", "Winterjacke", "Rucksack", "Sonnenbrille",
    "Armbanduhr",
    # Beauty & Pflege
    "Parfum", "Rasierer", "Haarpflege", "Zahnbürste elektrisch",
    # Sport & Freizeit
    "Fitness Zubehör", "Camping", "Fahrrad Zubehör", "Yogamatte",
    # Auto & Werkzeug
    "Autozubehör", "Akkuschrauber", "Werkzeug Set",
    # Spielzeug, Baby & Haustier
    "LEGO", "Brettspiel", "Hundefutter", "Katzenspielzeug",
    # Lebensmittel & Drogerie
    "Kaffee Bohnen", "babykleideung", "Tee",
    # Technik (bewusst nur ein paar)
    "Kopfhörer", "Alkohol", "LED Lampe",
]
MIN_DISCOUNT = 30          # nur Angebote mit mindestens so viel Prozent Rabatt
MIN_PRICE = 5.0            # Euro, darunter wird ignoriert
MAX_POSTS_PER_RUN = 30     # Ziel: so viele Posts pro Durchlauf (= pro Stunde)
MAX_SEARCHES_PER_RUN = 70  # Sicherung: so viele Amazon-Suchen höchstens pro Durchlauf
MAX_PAGES_PER_KEYWORD = 5  # so viele Ergebnisseiten (je 10 Treffer) pro Suchbegriff
PAGE_COOLDOWN_HOURS = 6    # Seiten ohne neue Deals werden so lange übersprungen
REPOST_AFTER_DAYS = 7      # gleiches Produkt frühestens nach so vielen Tagen
PAUSE_BETWEEN_SEARCHES = 0.5   # Sekunden (die Bibliothek bremst zusätzlich auf 1 Anfrage/Sek.)

# Uhrzeiten: Der Bot sendet nur in diesem Zeitfenster (volle Stunden, 24-Stunden-Format).
# Beispiel: 8 und 22 bedeutet von 08:00 bis 21:59 Uhr. Außerhalb passiert nichts.
# Mit "py bot.py --jetzt" kannst du das Zeitfenster einmalig überspringen.
ACTIVE_FROM_HOUR = 7
ACTIVE_UNTIL_HOUR = 22

STATE_FILE = Path(__file__).with_name("gepostet.json")
LOG_FILE = Path(__file__).with_name("bot.log")

DISCLOSURE = "Als Amazon-Partner verdiene ich an qualifizierten Verkäufen."
PRICE_NOTE = "Preis kann sich inzwischen geändert haben."
FOOTER = "🤝🏻 Smiley bei Kauf oder Weiterempfehlung"


# ----------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------
def scrub(text: str) -> str:
    """Ersetzt geheime Werte in Texten durch ***, damit sie nie in Protokollen auftauchen."""
    for name in ("TELEGRAM_TOKEN", "CREATORS_SECRET", "CREATORS_ID"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***")
    return text


def euro(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def get_resources():
    wanted = {
        "ITEM_INFO_DOT_TITLE",
        "IMAGES_DOT_PRIMARY_DOT_LARGE",
        "OFFERS_V2_DOT_LISTINGS_DOT_PRICE",
        "OFFERS_V2_DOT_LISTINGS_DOT_AVAILABILITY",
        "OFFERS_V2_DOT_LISTINGS_DOT_CONDITION",
        "OFFERS_V2_DOT_LISTINGS_DOT_IS_BUY_BOX_WINNER",
        "OFFERS_V2_DOT_LISTINGS_DOT_MERCHANT_INFO",
    }
    return [r for r in SearchItemsResource if r.name in wanted]


def link_for(item) -> str:
    url = getattr(item, "detail_page_url", None) or f"https://www.amazon.de/dp/{item.asin}"
    if "tag=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}tag={PARTNER_TAG}"
    return url


def parse_deal(item) -> dict | None:
    """Macht aus einem API-Item ein Deal-Dict, oder None wenn kein guter Deal."""
    try:
        listings = item.offers_v2.listings if item.offers_v2 else []
        if not listings:
            return None
        listing = next((l for l in listings if getattr(l, "is_buy_box_winner", False)), listings[0])
        price = listing.price
        if not price or not price.money:
            return None
        now = float(price.money.amount)
        if now < MIN_PRICE:
            return None

        percent = None
        old = None
        if price.savings and price.savings.percentage is not None:
            percent = float(price.savings.percentage)
        if price.saving_basis and price.saving_basis.money:
            old = float(price.saving_basis.money.amount)
        if percent is None and old and old > now:
            percent = round((old - now) / old * 100)
        if percent is None or percent < MIN_DISCOUNT:
            return None

        title = item.item_info.title.display_value
        image = None
        if item.images and item.images.primary and item.images.primary.large:
            image = item.images.primary.large.url

        return {
            "asin": item.asin,
            "title": title,
            "price": now,
            "old_price": old,
            "percent": int(round(percent)),
            "image": image,
            "link": link_for(item),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def format_post(deal: dict) -> str:
    title = deal["title"]
    if len(title) > 90:
        title = title[:87].rstrip() + "..."
    lines = [f"🧨 <b>{html.escape(title)}</b>", ""]
    lines.append(f"✅ Jetzt nur <b>{euro(deal['price'])}</b>  (-{deal['percent']} %)")
    if deal["old_price"]:
        lines.append(f"❌ Statt <s>{euro(deal['old_price'])}</s>")
    lines += ["", f"👉 {deal['link']}", "", FOOTER]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Doppelpost-Schutz
# ----------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def already_posted(state: dict, deal: dict) -> bool:
    entry = state.get(deal["asin"])
    if not entry:
        return False
    posted_at = datetime.fromisoformat(entry["time"])
    if datetime.now() - posted_at > timedelta(days=REPOST_AFTER_DAYS):
        return False
    # Nur erneut posten, wenn der Preis seit dem letzten Post deutlich gefallen ist
    return deal["price"] >= entry["price"] * 0.95


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def _telegram_post(url: str, data: dict) -> requests.Response:
    r = requests.post(url, data=data, timeout=30)
    if r.status_code == 429:  # Telegram bremst: kurz warten und einmal wiederholen
        wait = 5
        try:
            wait = int(r.json().get("parameters", {}).get("retry_after", 5))
        except (ValueError, TypeError, AttributeError):
            pass
        time.sleep(min(wait, 60) + 1)
        r = requests.post(url, data=data, timeout=30)
    return r


def send_telegram(deal: dict, text: str) -> None:
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    base = f"https://api.telegram.org/bot{token}"
    if deal["image"] and len(text) <= 1024:
        r = _telegram_post(
            f"{base}/sendPhoto",
            {"chat_id": chat_id, "photo": deal["image"], "caption": text, "parse_mode": "HTML"},
        )
        if r.ok:
            return
    r = _telegram_post(
        f"{base}/sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    r.raise_for_status()


# ----------------------------------------------------------------------
# Hauptablauf
# ----------------------------------------------------------------------
def collect_deals(api: AmazonCreatorsApi, state: dict) -> tuple[list[dict], dict]:
    """Sucht so lange, bis genug neue Deals da sind (oder das Such-Limit erreicht ist).

    Es werden erst Seite 1 aller Suchbegriffe durchsucht, dann Seite 2 usw. So kommen
    viele Bereiche vor, und tiefere Seiten nur, wenn sie wirklich gebraucht werden.
    """
    resources = get_resources()
    now = datetime.now()
    # Seiten, die zuletzt keine neuen Deals hatten, eine Weile überspringen (spart Anfragen)
    cooldown = {
        key: ts
        for key, ts in state.get("_seiten", {}).items()
        if now - datetime.fromisoformat(ts) < timedelta(hours=PAGE_COOLDOWN_HOURS)
    }
    state["_seiten"] = cooldown

    active = random.sample(KEYWORDS, len(KEYWORDS))
    seen: set[str] = set()
    groups: dict[str, list[dict]] = {}
    stats = {"searches": 0, "errors": 0, "skipped": 0, "aborted": False}
    new_count = 0
    errors_in_row = 0

    def finished() -> bool:
        return (
            new_count >= MAX_POSTS_PER_RUN
            or stats["searches"] >= MAX_SEARCHES_PER_RUN
            or errors_in_row >= 5
        )

    for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
        for keyword in list(active):
            if finished():
                break
            key = f"{keyword}|{page}"
            if key in cooldown:
                stats["skipped"] += 1
                continue
            stats["searches"] += 1
            try:
                result = api.search_items(
                    keywords=keyword,
                    item_count=10,
                    item_page=page,
                    min_saving_percent=MIN_DISCOUNT,
                    resources=resources or None,
                )
                errors_in_row = 0
            except Exception as exc:  # einzelne Fehler sollen den Lauf nicht stoppen
                stats["errors"] += 1
                errors_in_row += 1
                print(f"[Warnung] Suche '{keyword}' Seite {page} fehlgeschlagen: {scrub(str(exc))}")
                time.sleep(PAUSE_BETWEEN_SEARCHES)
                continue
            items = result.items or []
            if not items:
                active.remove(keyword)  # keine weiteren Seiten für diesen Begriff
            found = 0
            for item in items:
                deal = parse_deal(item)
                if deal and deal["asin"] not in seen and not already_posted(state, deal):
                    seen.add(deal["asin"])
                    deal["keyword"] = keyword
                    groups.setdefault(keyword, []).append(deal)
                    found += 1
            if found == 0:
                cooldown[key] = now.isoformat()
            new_count += found
            print(f"  Suche {stats['searches']}: {keyword} (Seite {page}): {found} neue Deals")
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        if finished():
            break

    if errors_in_row >= 5:
        stats["aborted"] = True
        print("[Abbruch] 5 Fehler in Folge. Mögliche Ursache: Amazon-Limit oder Zugang gesperrt.")

    # Abwechselnd aus jedem Bereich den besten Deal nehmen (Rundlauf)
    per_keyword = list(groups.values())
    for group in per_keyword:
        group.sort(key=lambda d: d["percent"], reverse=True)
    random.shuffle(per_keyword)
    mixed: list[dict] = []
    while any(per_keyword):
        for group in per_keyword:
            if group:
                mixed.append(group.pop(0))
    return mixed, stats


def in_active_hours(now: datetime | None = None) -> bool:
    hour = (now or datetime.now()).hour
    return ACTIVE_FROM_HOUR <= hour < ACTIVE_UNTIL_HOUR


def write_log(text: str) -> None:
    """Schreibt eine Zeile in bot.log, damit du später nachsehen kannst, was der Bot getan hat."""
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%d.%m.%Y %H:%M}  {text}\n")
    except OSError:
        pass


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if "--jetzt" not in sys.argv and not dry_run and not in_active_hours():
        print(f"Außerhalb des Zeitfensters ({ACTIVE_FROM_HOUR}:00 bis {ACTIVE_UNTIL_HOUR}:00 Uhr). Nichts gesendet.")
        return
    api = AmazonCreatorsApi(
        credential_id=os.environ["CREATORS_ID"],
        credential_secret=os.environ["CREATORS_SECRET"],
        version=os.environ["CREATORS_VERSION"],
        tag=PARTNER_TAG,
        country=Country.DE,
        throttling=1,
    )
    state = load_state()
    deals, stats = collect_deals(api, state)
    if not dry_run:
        save_state(state)  # merkt sich auch, welche Seiten leer waren
    print(
        f"{len(deals)} neue Deals gefunden "
        f"({stats['searches']} Suchen, {stats['errors']} Fehler, {stats['skipped']} Seiten übersprungen)."
    )

    sent = 0
    for deal in deals[:MAX_POSTS_PER_RUN]:
        text = format_post(deal)
        if dry_run:
            print("-" * 40)
            print(text)
            continue
        try:
            send_telegram(deal, text)
        except Exception as exc:
            print(f"[Fehler] Telegram-Versand für {deal['asin']} fehlgeschlagen: {scrub(str(exc))}")
            continue
        state[deal["asin"]] = {"time": datetime.now().isoformat(), "price": deal["price"]}
        save_state(state)
        sent += 1
        time.sleep(1)

    if not dry_run:
        write_log(
            f"gesendet: {sent} von {MAX_POSTS_PER_RUN} | gefunden: {len(deals)} | "
            f"Suchen: {stats['searches']} | Fehler: {stats['errors']}"
            + (" | ABGEBROCHEN" if stats["aborted"] else "")
        )
    print(f"Fertig. {sent if not dry_run else 0} Posts gesendet.")


if __name__ == "__main__":
    main()
