"""Masta Deals Blitz-Bot: nur zeitlich begrenzte Angebote (Blitzangebote & Co.)

Nutzt dieselben Einstellungen, Suchbegriffe, Karten und Telegram-Funktionen wie bot.py,
nimmt aber nur Angebote, bei denen Amazon eine Ablaufzeit (dealDetails) mitliefert.
Auf der Karte steht "BLITZ bis 17:45 Uhr", im Text darunter die Endzeit.

Start:      python blitz.py
Nur testen: python blitz.py --dry-run   (druckt die Posts, sendet nichts)
Sofort:     python blitz.py --jetzt     (überspringt Zeitfenster und Pause)
"""
import io
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import bot  # gleiche Einstellungen, Suchbegriffe, Karte und Versand wie der normale Bot
from amazon_creatorsapi import AmazonCreatorsApi, Country
from amazon_creatorsapi.models import SearchItemsResource

# ----------------------------------------------------------------------
# Einstellungen nur für den Blitz-Bot
# ----------------------------------------------------------------------
BLITZ_MIN_DISCOUNT = 15        # Blitzangebote sind seltener, deshalb weniger streng als 25 %
BLITZ_MAX_POSTS = 10           # höchstens so viele Posts pro Lauf
BLITZ_MAX_SEARCHES = 120       # Sicherung: Amazon-Suchen pro Lauf
BLITZ_MAX_PAGES = 3            # Ergebnisseiten pro Suchbegriff
BLITZ_MIN_MINUTES_LEFT = 20    # Angebote, die gleich enden, werden nicht mehr gepostet
BLITZ_MAX_HOURS_LEFT = 24      # nur Angebote, die in höchstens so vielen Stunden enden (dort läuft auf Amazon der Countdown)
BLITZ_MIN_MINUTES_BETWEEN_RUNS = 90
EXTRA_KEYWORDS = ["Blitzangebot", "Angebot des Tages"]

BLITZ_STATE_FILE = Path(__file__).with_name("gepostet_blitz.json")
TZ = ZoneInfo("Europe/Berlin")

# Der normale Bot soll sich für Blitzangebote genauso verhalten, nur mit eigenen Werten
bot.MIN_DISCOUNT = BLITZ_MIN_DISCOUNT


# ----------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------
def parse_end(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def end_label(end: datetime, short: bool = False) -> str:
    now = datetime.now(TZ)
    uhr = f"{end:%H:%M} Uhr"
    if end.date() == now.date():
        tag = "heute"
    elif end.date() == (now + timedelta(days=1)).date():
        tag = "morgen"
    else:
        tag = f"{end:%d.%m.}"
    if short:
        return uhr if tag == "heute" else f"{tag} {uhr}"
    return f"{tag} um {uhr}"


def get_resources():
    wanted = {
        "ITEM_INFO_DOT_TITLE",
        "IMAGES_DOT_PRIMARY_DOT_LARGE",
        "OFFERS_V2_DOT_LISTINGS_DOT_PRICE",
        "OFFERS_V2_DOT_LISTINGS_DOT_AVAILABILITY",
        "OFFERS_V2_DOT_LISTINGS_DOT_CONDITION",
        "OFFERS_V2_DOT_LISTINGS_DOT_IS_BUY_BOX_WINNER",
        "OFFERS_V2_DOT_LISTINGS_DOT_MERCHANT_INFO",
        "OFFERS_V2_DOT_LISTINGS_DOT_DEAL_DETAILS",
        "OFFERS_V2_DOT_LISTINGS_DOT_TYPE",
    }
    return [r for r in SearchItemsResource if r.name in wanted]


def parse_blitz(item) -> dict | None:
    """Wie bot.parse_deal, aber nur für Angebote mit Ablaufzeit."""
    try:
        listings = item.offers_v2.listings if item.offers_v2 else []
        with_deal = [l for l in listings if getattr(l, "deal_details", None)]
        if not with_deal:
            return None
        listing = next((l for l in with_deal if getattr(l, "is_buy_box_winner", False)), with_deal[0])
        details = listing.deal_details
        end = parse_end(getattr(details, "end_time", None))
        now = datetime.now(TZ)
        if end is None or end < now + timedelta(minutes=BLITZ_MIN_MINUTES_LEFT):
            return None
        if end > now + timedelta(hours=BLITZ_MAX_HOURS_LEFT):
            return None  # läuft noch lange, kein Countdown
        start = parse_end(getattr(details, "start_time", None))
        if start is not None and start > now:
            return None  # Angebot hat noch nicht begonnen

        shim = SimpleNamespace(
            asin=item.asin,
            item_info=item.item_info,
            images=item.images,
            offers_v2=SimpleNamespace(listings=[listing]),
        )
        deal = bot.parse_deal(shim)
        if not deal:
            return None
        deal["end"] = end
        deal["claimed"] = getattr(details, "percent_claimed", None)
        deal["badge"] = getattr(details, "badge", None)
        deal["prime_only"] = getattr(details, "access_type", None) == "PRIME_EXCLUSIVE"
        return deal
    except (AttributeError, TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Karte und Text mit Ablaufzeit
# ----------------------------------------------------------------------
_orig_make_card = bot.make_card


def make_blitz_card(deal: dict) -> bytes | None:
    card_bytes = _orig_make_card(deal)
    if card_bytes is None or bot.Image is None:
        return card_bytes
    try:
        img = bot.Image.open(io.BytesIO(card_bytes)).convert("RGB")
        d = bot.ImageDraw.Draw(img)
        text = f"BLITZ bis {end_label(deal['end'], short=True)}"
        size = 30
        font = bot._font(size, True)
        max_w = 380
        while font.getlength(text) + 36 > max_w and size > 18:
            size -= 2
            font = bot._font(size, True)
        w = int(font.getlength(text)) + 36
        right = bot.CARD_W - 30
        y0, y1 = 262, 316
        d.rounded_rectangle((right - w, y0, right, y1), radius=14, fill="#ff9900")
        d.text((right - w / 2, (y0 + y1) / 2), text, font=font, fill="#1a1a1a", anchor="mm")
        out = io.BytesIO()
        img.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as exc:  # die Karte darf nie den Lauf stoppen
        print(f"[Hinweis] Blitz-Schild nicht möglich: {bot.scrub(str(exc))}")
        return card_bytes


def make_blitz_caption(deal: dict) -> str:
    kopf = f"⚡ Blitzangebot – endet {end_label(deal['end'])}"
    infos = []
    stunden = (deal["end"] - datetime.now(TZ)).total_seconds() / 3600
    if stunden >= 1:
        infos.append(f"noch ca. {int(round(stunden))} Std.")
    if deal.get("claimed"):
        try:
            infos.append(f"{int(float(deal['claimed']))} % verkauft")
        except (TypeError, ValueError):
            pass
    if deal.get("prime_only"):
        infos.append("nur Prime")
    if infos:
        kopf += " (" + ", ".join(infos) + ")"
    return "\n".join([kopf, "", f"👉 {deal['link']}", "", bot.FOOTER])


def make_blitz_text(deal: dict) -> str:
    """Klassischer Text (Rückfall, falls die Karte nicht klappt)."""
    text = bot.format_post(deal)
    return f"⚡ Blitzangebot – endet {end_label(deal['end'])}\n" + text


bot.make_card = make_blitz_card
bot.format_caption = make_blitz_caption


# ----------------------------------------------------------------------
# Merkzettel (eigene Datei, damit sich beide Bots nicht stören)
# ----------------------------------------------------------------------
def load_state() -> dict:
    if BLITZ_STATE_FILE.exists():
        try:
            return json.loads(BLITZ_STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    BLITZ_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def prune(state: dict) -> None:
    """Wirft alte Einträge raus (Angebote, die längst vorbei sind)."""
    limit = (datetime.now() - timedelta(days=3)).isoformat()
    for key in [k for k, v in state.items() if isinstance(v, dict) and v.get("time", "") < limit]:
        del state[key]


def already_posted(state: dict, deal: dict) -> bool:
    entry = state.get(deal["asin"])
    # Dasselbe Angebot (gleiche Endzeit) nur einmal; ein neues Angebot am nächsten Tag ist wieder erlaubt.
    return bool(entry) and entry.get("end") == deal["end"].isoformat()


# ----------------------------------------------------------------------
# Hauptablauf
# ----------------------------------------------------------------------
def collect(api: AmazonCreatorsApi, state: dict) -> tuple[list[dict], dict]:
    resources = get_resources()
    keywords = random.sample(bot.KEYWORDS, len(bot.KEYWORDS))
    # Extra-Begriffe kommen zuerst dran, dort stecken am ehesten Aktionen
    keywords = EXTRA_KEYWORDS + [k for k in keywords if k not in EXTRA_KEYWORDS]
    seen: set[str] = set()
    deals: list[dict] = []
    stats = {"searches": 0, "errors": 0, "items": 0, "aborted": False}
    errors_in_row = 0
    active = list(keywords)

    def finished() -> bool:
        return (
            len(deals) >= BLITZ_MAX_POSTS
            or stats["searches"] >= BLITZ_MAX_SEARCHES
            or errors_in_row >= 5
        )

    for page in range(1, BLITZ_MAX_PAGES + 1):
        for keyword in list(active):
            if finished():
                break
            stats["searches"] += 1
            try:
                result = api.search_items(
                    keywords=keyword,
                    item_count=10,
                    item_page=page,
                    min_saving_percent=BLITZ_MIN_DISCOUNT,
                    resources=resources or None,
                )
                errors_in_row = 0
            except Exception as exc:
                stats["errors"] += 1
                errors_in_row += 1
                print(f"[Warnung] Suche '{keyword}' Seite {page} fehlgeschlagen: {bot.scrub(str(exc))}")
                time.sleep(bot.PAUSE_BETWEEN_SEARCHES)
                continue
            items = result.items or []
            stats["items"] += len(items)
            if not items:
                active.remove(keyword)
            found = 0
            for item in items:
                deal = parse_blitz(item)
                if deal and deal["asin"] not in seen and not already_posted(state, deal):
                    seen.add(deal["asin"])
                    deal["keyword"] = keyword
                    deals.append(deal)
                    found += 1
            if found:
                print(f"  Suche {stats['searches']}: {keyword} (Seite {page}): {found} Blitzangebote")
            time.sleep(bot.PAUSE_BETWEEN_SEARCHES)
        if finished():
            break

    if errors_in_row >= 5:
        stats["aborted"] = True
        print("[Abbruch] 5 Fehler in Folge. Mögliche Ursache: Amazon-Limit oder Zugang gesperrt.")

    # Zuerst die, die bald enden, bei gleicher Zeit der höhere Rabatt
    deals.sort(key=lambda d: (d["end"], -d["percent"]))
    return deals, stats


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    jetzt = "--jetzt" in sys.argv
    if not jetzt and not dry_run and not bot.in_active_hours():
        print(f"Außerhalb des Zeitfensters ({bot.ACTIVE_FROM_HOUR}:00 bis {bot.ACTIVE_UNTIL_HOUR}:00 Uhr). Nichts gesendet.")
        return
    state = load_state()
    prune(state)
    if not jetzt and not dry_run:
        last = state.get("_letzter_lauf")
        if last:
            try:
                age = datetime.now() - datetime.fromisoformat(last)
            except ValueError:
                age = None
            if age is not None and age < timedelta(minutes=BLITZ_MIN_MINUTES_BETWEEN_RUNS):
                print(f"Letzter Blitz-Lauf war vor {int(age.total_seconds() // 60)} Minuten. Übersprungen.")
                return

    api = AmazonCreatorsApi(
        credential_id=bot.os.environ["CREATORS_ID"],
        credential_secret=bot.os.environ["CREATORS_SECRET"],
        version=bot.os.environ["CREATORS_VERSION"],
        tag=bot.PARTNER_TAG,
        country=Country.DE,
        throttling=1,
    )
    deals, stats = collect(api, state)
    print(
        f"{len(deals)} Blitzangebote gefunden "
        f"({stats['searches']} Suchen, {stats['items']} Treffer geprüft, {stats['errors']} Fehler)."
    )

    sent = 0
    for deal in deals[:BLITZ_MAX_POSTS]:
        if dry_run:
            print("-" * 40)
            print(f"[Karte] {deal['title'][:60]} | {bot.euro(deal['price'])} (-{deal['percent']} %)")
            print(make_blitz_caption(deal))
            continue
        try:
            bot.send_telegram(deal, make_blitz_text(deal))
        except Exception as exc:
            print(f"[Fehler] Telegram-Versand für {deal['asin']} fehlgeschlagen: {bot.scrub(str(exc))}")
            continue
        state[deal["asin"]] = {
            "time": datetime.now().isoformat(),
            "price": deal["price"],
            "end": deal["end"].isoformat(),
        }
        save_state(state)
        sent += 1
        time.sleep(1)

    if not dry_run:
        bot.write_log(
            f"BLITZ gesendet: {sent} | gefunden: {len(deals)} | Treffer geprüft: {stats['items']} | "
            f"Suchen: {stats['searches']} | Fehler: {stats['errors']}"
            + (" | ABGEBROCHEN" if stats["aborted"] else "")
        )
        if not stats["aborted"]:
            state["_letzter_lauf"] = datetime.now().isoformat()
        save_state(state)
    print(f"Fertig. {sent if not dry_run else 0} Blitz-Posts gesendet.")


if __name__ == "__main__":
    main()
