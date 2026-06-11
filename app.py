from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from collections.abc import Callable
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, unquote, urlparse
from urllib.request import Request, urlopen
import hashlib
import html
import json
import math
import re
import shlex
import sqlite3
import sys
import time
import traceback
import warnings

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering
from sklearn.cluster import HDBSCAN
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CACHE_DIR = ROOT / ".cache"
CACHE_DB = CACHE_DIR / "cache.sqlite3"
DEPS_DIR = ROOT / ".deps"
if DEPS_DIR.exists() and str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))
MTGTOP8 = "https://www.mtgtop8.com"
SCRYFALL_COLLECTION = "https://api.scryfall.com/cards/collection"
USER_AGENT = "Mozilla/5.0 mtgtop8-pca-local-tool"
DEFAULT_CACHE_TTL = object()
MTGTOP8_SEARCH_CACHE_TTL = 7 * 24 * 60 * 60
MTGTOP8_OTHER_CACHE_TTL = 24 * 60 * 60
SEARCH_OPTIONS_CACHE_TTL = 24 * 60 * 60
SCRYFALL_COLOR_CACHE_TTL = 90 * 24 * 60 * 60
PAIRWISE_DECK_LIMIT = 3000
SILHOUETTE_DECK_LIMIT = 3000
FETCH_CACHE: dict[tuple[str, str], str] = {}
SEARCH_OPTIONS_CACHE: dict[str, object] = {}
SCRYFALL_CARD_COLORS: dict[str, list[str]] = {}
SCRYFALL_CARD_METADATA: dict[str, dict[str, object]] = {}
COLOR_ORDER = ["W", "U", "B", "R", "G"]
FALLBACK_ARCHETYPE_FORMATS = [
    "VI",
    "LE",
    "MO",
    "PI",
    "EX",
    "HI",
    "ST",
    "BL",
    "PAU",
    "EDH",
    "HIGH",
    "EDHP",
    "EDHM",
    "CHL",
    "PEA",
    "ALCH",
    "cEDH",
    "EXP",
    "PREM",
]
FALLBACK_FORMATS = [
    {"value": "", "label": "All"},
    {"value": "COMF", "label": "All Commander formats"},
    {"value": "NCOMF", "label": "All non-Commander formats"},
    {"value": "ST", "label": "Standard"},
    {"value": "ALCH", "label": "Alchemy"},
    {"value": "EXP", "label": "Explorer"},
    {"value": "HI", "label": "Historic"},
    {"value": "PI", "label": "Pioneer"},
    {"value": "MO", "label": "Modern"},
    {"value": "PREM", "label": "Premodern"},
    {"value": "LE", "label": "Legacy"},
    {"value": "VI", "label": "Vintage"},
    {"value": "cEDH", "label": "cEDH"},
    {"value": "EDH", "label": "Duel Commander"},
    {"value": "EDHM", "label": "MTGO Commander"},
    {"value": "BL", "label": "Block"},
    {"value": "EX", "label": "Extended"},
    {"value": "PAU", "label": "Pauper"},
    {"value": "PEA", "label": "Peasant"},
    {"value": "HIGH", "label": "Highlander"},
    {"value": "CHL", "label": "Canadian Highlander"},
    {"value": "LI", "label": "Limited"},
]
FALLBACK_LEVELS = [
    {"code": "P", "label": "Professional", "checked": True},
    {"code": "M", "label": "Major", "checked": True},
    {"code": "C", "label": "Competitive", "checked": True},
    {"code": "R", "label": "Regular", "checked": True},
]


@dataclass
class FetchStats:
    memory_hits: int = 0
    disk_hits: int = 0
    misses: int = 0

    @property
    def hits(self) -> int:
        return self.memory_hits + self.disk_hits

    @property
    def total(self) -> int:
        return self.hits + self.misses

    def as_dict(self) -> dict[str, int]:
        return {
            "memory_hits": self.memory_hits,
            "disk_hits": self.disk_hits,
            "hits": self.hits,
            "misses": self.misses,
            "total": self.total,
        }


def disk_cache_key(namespace: str, *parts: object) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def cache_connection() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_DB, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_entries (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL
        )
        """
    )
    return connection


def disk_cache_get(key: str) -> str | None:
    now = time.time()
    try:
        with cache_connection() as connection:
            row = connection.execute(
                "SELECT value, expires_at FROM cache_entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            value, expires_at = row
            if expires_at is not None and float(expires_at) <= now:
                connection.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                return None
            return str(value)
    except sqlite3.Error:
        return None


def disk_cache_set(key: str, value: str, ttl_seconds: int | None) -> None:
    now = time.time()
    expires_at = now + ttl_seconds if ttl_seconds else None
    try:
        with cache_connection() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries (key, value, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (key, value, now, expires_at),
            )
    except sqlite3.Error:
        return


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b.*?</style>", "", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).replace("\xa0", " ").strip()


def split_tds(row: str) -> list[str]:
    return re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)


class SingleTagParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attrs = dict(attrs)


def parse_attrs(tag_html: str) -> dict[str, str | None]:
    parser = SingleTagParser()
    parser.feed(tag_html)
    return parser.attrs


def parse_star_cell(cell: str) -> tuple[int, bool]:
    stars = 0
    big_star = False
    for tag in re.findall(r"<img\b[^>]*>", cell, flags=re.I | re.S):
        attrs = parse_attrs(tag)
        src = (attrs.get("src") or "").split("?", 1)[0]
        filename = unquote(src).rsplit("/", 1)[-1].lower()
        if filename == "bigstar.png":
            big_star = True
        elif filename == "star.png":
            stars += 1
    if big_star and stars == 0:
        stars = 1
    return stars, big_star


FEATURED_FINISH_PERCENT = 0.02


def parse_rank_info(rank: str | None) -> dict[str, int | float | None]:
    text = (rank or "").strip().lower()
    info: dict[str, int | float | None] = {
        "finish_min": None,
        "finish_max": None,
        "players": None,
        "finish_percent": None,
    }
    if not text:
        return info

    placement = text
    if "/" in text:
        placement, players_text = text.split("/", 1)
        players_match = re.search(r"\d[\d,]*", players_text)
        if players_match:
            info["players"] = int(players_match.group(0).replace(",", ""))

    if any(word in placement for word in ("winner", "champion")):
        info["finish_min"] = 1
        info["finish_max"] = 1
    elif top_match := re.search(r"\btop\s*(\d+)\b", placement):
        info["finish_min"] = 1
        info["finish_max"] = int(top_match.group(1))
    elif range_match := re.search(r"\b(\d+)\s*-\s*(\d+)\b", placement):
        info["finish_min"] = int(range_match.group(1))
        info["finish_max"] = int(range_match.group(2))
    elif number_match := re.search(r"\b(\d+)\b", placement):
        info["finish_min"] = int(number_match.group(1))
        info["finish_max"] = int(number_match.group(1))

    if info["players"] and info["finish_max"]:
        info["finish_percent"] = float(info["finish_max"]) / float(info["players"])
    return info


def rank_is_featured_finish(rank: str | None) -> bool:
    info = parse_rank_info(rank)
    if info["finish_percent"] is not None:
        return float(info["finish_percent"]) <= FEATURED_FINISH_PERCENT
    return bool(info["finish_max"] and int(info["finish_max"]) <= 8)


def mtgtop8_cache_ttl(url: str, data: list[tuple[str, str]] | None = None) -> int | None:
    path = urlparse(url).path
    if path == "/compare":
        return None
    if path == "/search" and data is not None:
        return MTGTOP8_SEARCH_CACHE_TTL
    return MTGTOP8_OTHER_CACHE_TTL


def deck_id_sort_key(deck_id: str) -> tuple[int, int | str]:
    try:
        return (0, int(deck_id))
    except ValueError:
        return (1, deck_id)


def fetch_url(
    url: str,
    *,
    data: list[tuple[str, str]] | None = None,
    ttl_seconds: int | None | object = DEFAULT_CACHE_TTL,
    stats: FetchStats | None = None,
) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {"www.mtgtop8.com", "mtgtop8.com"}:
        raise ValueError("Only mtgtop8.com URLs are supported.")

    encoded = None
    encoded_text = ""
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        encoded_text = urlencode(data, doseq=True)
        encoded = encoded_text.encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    ttl = mtgtop8_cache_ttl(url, data) if ttl_seconds is DEFAULT_CACHE_TTL else ttl_seconds
    cache_key = (url, encoded_text)
    if cache_key in FETCH_CACHE:
        if stats:
            stats.memory_hits += 1
        return FETCH_CACHE[cache_key]
    persisted_key = disk_cache_key("mtgtop8-fetch", url, encoded_text)
    persisted = disk_cache_get(persisted_key)
    if persisted is not None:
        FETCH_CACHE[cache_key] = persisted
        disk_cache_set(persisted_key, persisted, ttl)
        if stats:
            stats.disk_hits += 1
        return persisted

    request = Request(url, data=encoded, headers=headers)
    with urlopen(request, timeout=30) as response:
        body = response.read()
    text = body.decode("latin1", errors="replace")
    FETCH_CACHE[cache_key] = text
    disk_cache_set(persisted_key, text, ttl)
    if stats:
        stats.misses += 1
    return text


def looks_like_html(value: str) -> bool:
    sample = value.lstrip()[:300].lower()
    return "<html" in sample or "<!doctype html" in sample or "<form" in sample or "<table" in sample


def looks_like_post_body(value: str) -> bool:
    if "\n" in value and "&" not in value:
        return False
    parsed = parse_qsl(value, keep_blank_values=True)
    names = {name for name, _ in parsed}
    return bool(parsed) and (
        "format" in names
        or "cards" in names
        or "current_page" in names
        or any(name.startswith("compet_check[") for name in names)
    )


def extract_post_body_from_curl(value: str) -> str | None:
    if not value.lstrip().startswith("curl "):
        return None
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = value.split()

    data_flags = {"--data", "--data-raw", "--data-binary", "--data-urlencode", "-d"}
    for i, part in enumerate(parts):
        if part in data_flags and i + 1 < len(parts):
            body = parts[i + 1]
            return body[1:] if body.startswith("$") else body
        for flag in data_flags:
            prefix = flag + "="
            if part.startswith(prefix):
                body = part[len(prefix) :]
                return body[1:] if body.startswith("$") else body
    return None


def find_search_form(source: str) -> str:
    form_match = re.search(r"<form\b[^>]*name=[\"']?search_form[\"']?[^>]*>(.*?)</form>", source, flags=re.I | re.S)
    return form_match.group(1) if form_match else ""


def select_options(select_html: str) -> list[dict[str, str]]:
    options = []
    for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_html, flags=re.I | re.S):
        attrs = parse_attrs("<option " + option_match.group(1) + ">")
        label = strip_tags(option_match.group(2))
        if not label:
            continue
        options.append({"value": attrs.get("value") or "", "label": label})
    return options


def parse_search_options(source: str) -> dict:
    form = find_search_form(source)
    if not form:
        raise ValueError("Could not find the MTGTop8 search form.")

    formats = FALLBACK_FORMATS
    archetypes: dict[str, list[dict[str, str]]] = {}
    for select_match in re.finditer(r"<select\b([^>]*)>(.*?)</select>", form, flags=re.I | re.S):
        attrs = parse_attrs("<select " + select_match.group(1) + ">")
        name = attrs.get("name") or ""
        options = select_options(select_match.group(2))
        if name == "format" and options:
            formats = options
            continue
        archetype_match = re.fullmatch(r"archetype_sel\[(.+)\]", name)
        if archetype_match:
            archetypes[archetype_match.group(1)] = options

    levels = []
    for input_match in re.finditer(
        r"(<input\b[^>]*name=[\"']?compet_check\[[^\]]+\][\"']?[^>]*>)(.*?)(?:<br\b[^>]*>|</td>)",
        form,
        flags=re.I | re.S,
    ):
        attrs = parse_attrs(input_match.group(1))
        name = attrs.get("name") or ""
        code_match = re.fullmatch(r"compet_check\[(.+)\]", name)
        if not code_match:
            continue
        levels.append(
            {
                "code": code_match.group(1),
                "label": strip_tags(input_match.group(2)) or code_match.group(1),
                "checked": "checked" in attrs,
            }
        )
    if not levels:
        levels = FALLBACK_LEVELS

    return {"formats": formats, "archetypes": archetypes, "levels": levels}


def search_options_payload() -> dict:
    cached = SEARCH_OPTIONS_CACHE.get("payload")
    if isinstance(cached, dict):
        return cached
    persisted = disk_cache_get(disk_cache_key("search-options"))
    if persisted is not None:
        try:
            payload = json.loads(persisted)
            if isinstance(payload, dict):
                SEARCH_OPTIONS_CACHE["payload"] = payload
                return payload
        except json.JSONDecodeError:
            pass
    try:
        payload = parse_search_options(fetch_url(f"{MTGTOP8}/search", ttl_seconds=SEARCH_OPTIONS_CACHE_TTL))
    except Exception:
        payload = {"formats": FALLBACK_FORMATS, "archetypes": {}, "levels": FALLBACK_LEVELS}
    SEARCH_OPTIONS_CACHE["payload"] = payload
    disk_cache_set(disk_cache_key("search-options"), json.dumps(payload), SEARCH_OPTIONS_CACHE_TTL)
    return payload


def build_search_form(criteria: dict) -> list[tuple[str, str]]:
    if not isinstance(criteria, dict):
        criteria = {}

    selected_format = str(criteria.get("format") or "")
    selected_archetype = str(criteria.get("archetype") or "")
    options = search_options_payload()
    archetype_keys = list((options.get("archetypes") or {}).keys()) or FALLBACK_ARCHETYPE_FORMATS

    selected_levels = criteria.get("levels")
    if isinstance(selected_levels, list):
        levels = {str(code) for code in selected_levels}
    elif isinstance(selected_levels, dict):
        levels = {str(code) for code, enabled in selected_levels.items() if enabled}
    else:
        levels = {level["code"] for level in FALLBACK_LEVELS}

    main_deck = bool(criteria.get("mainDeck", True))
    sideboard = bool(criteria.get("sideboard", False))
    if not main_deck and not sideboard:
        main_deck = True

    values: list[tuple[str, str]] = [
        ("current_page", ""),
        ("event_titre", str(criteria.get("event") or "")),
        ("deck_titre", str(criteria.get("deck") or "")),
        ("player", str(criteria.get("player") or "")),
        ("format", selected_format),
    ]

    for key in archetype_keys:
        value = selected_archetype if selected_format == key else ""
        values.append((f"archetype_sel[{key}]", value))

    for code in ("P", "M", "C", "R"):
        if code in levels:
            values.append((f"compet_check[{code}]", "1"))

    if main_deck:
        values.append(("MD_check", "1"))
    if sideboard:
        values.append(("SB_check", "1"))
    cards = str(criteria.get("cards") or "")
    card_lines = [line.strip() for line in re.split(r"\r?\n", cards) if line.strip()]
    normalized_cards = ("\r\n".join(card_lines) + "\r\n") if card_lines else ""

    values.extend(
        [
            ("cards", normalized_cards),
            ("date_start", str(criteria.get("dateStart") or "")),
            ("date_end", str(criteria.get("dateEnd") or "")),
        ]
    )
    return values


def find_compare_table(source: str) -> str | None:
    marker = '<table  border=0 cellspacing=0 cellpadding=1 align=center style="margin-top:110px;">'
    start = source.find(marker)
    if start == -1:
        match = re.search(r"<table\b[^>]*margin-top:\s*110px[^>]*>", source, flags=re.I)
        if not match:
            return None
        start = match.start()
    end = source.find("</table>", start)
    if end == -1:
        return None
    return source[start : end + len("</table>")]


def parse_compare_html(source: str, source_url: str = "") -> tuple[list[dict], list[dict]]:
    table = find_compare_table(source)
    if table is None:
        raise ValueError("Could not find an MTGTop8 compare table.")

    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", table, flags=re.I | re.S)
    if not rows:
        raise ValueError("No rows found in the compare table.")

    decks: list[dict] = []
    for cell in split_tds(rows[0])[2:]:
        match = re.search(r"href=event\?e=(\d+)&d=(\d+)>(.*?)</a>", cell, flags=re.I | re.S)
        if not match:
            continue
        event_id, deck_id, player_html = match.groups()
        finish_match = re.search(r"<span[^>]*>(.*?)</span>", cell, flags=re.I | re.S)
        decks.append(
            {
                "deck_id": deck_id,
                "event_id": event_id,
                "player": strip_tags(player_html),
                "rank": strip_tags(finish_match.group(1)) if finish_match else "",
                "deck_url": f"{MTGTOP8}/event?e={event_id}&d={deck_id}",
                "source_url": source_url,
                "tournament_stars": 0,
                "tournament_big_star": False,
            }
        )

    deck_ids = [deck["deck_id"] for deck in decks]
    for row in rows[1:]:
        if not re.search(r"(?:big)?star\.png", row, flags=re.I):
            continue
        cells = split_tds(row)
        if len(cells) < 2 + len(deck_ids):
            continue
        for deck, cell in zip(decks, cells[2 : 2 + len(deck_ids)]):
            stars, big_star = parse_star_cell(cell)
            deck["tournament_stars"] = stars
            deck["tournament_big_star"] = big_star
        break

    records: list[dict] = []
    section = None
    for row in rows[1:]:
        cells = split_tds(row)
        if not cells:
            continue
        first_text = strip_tags(cells[0])
        if len(cells) == 1 and first_text in {"LANDS", "CREATURES", "OTHER SPELLS", "SIDEBOARDS"}:
            section = first_text
            continue
        card_match = re.search(r'<div\s+class=c2\b[^>]*>(.*?)</div>', cells[0], flags=re.I | re.S)
        if not card_match or section is None:
            continue
        card = strip_tags(card_match.group(1))
        for deck_id, cell in zip(deck_ids, cells[2 : 2 + len(deck_ids)]):
            text = strip_tags(cell)
            copies = int(float(text)) if text else 0
            if copies:
                records.append({"deck_id": deck_id, "section": section, "card": card, "copies": copies})

    return decks, records


def extract_search_form(source: str, page: int | None = None) -> list[tuple[str, str]]:
    form_match = re.search(r"<form\b[^>]*name=[\"']?search_form[\"']?[^>]*>(.*?)</form>", source, flags=re.I | re.S)
    if not form_match:
        return []
    form = form_match.group(1)
    values: list[tuple[str, str]] = []

    for tag in re.findall(r"<input\b[^>]*>", form, flags=re.I | re.S):
        attrs = parse_attrs(tag)
        name = attrs.get("name")
        if not name:
            continue
        input_type = (attrs.get("type") or "text").lower()
        if input_type in {"submit", "button", "image", "reset"}:
            continue
        if input_type in {"checkbox", "radio"} and "checked" not in attrs:
            continue
        value = attrs.get("value") or ""
        if name == "current_page" and page is not None:
            value = str(page)
        values.append((name, value))

    for textarea_match in re.finditer(r"<textarea\b([^>]*)>(.*?)</textarea>", form, flags=re.I | re.S):
        attrs = parse_attrs("<textarea " + textarea_match.group(1) + ">")
        name = attrs.get("name")
        if name:
            values.append((name, html.unescape(textarea_match.group(2)).strip()))

    for select_match in re.finditer(r"<select\b([^>]*)>(.*?)</select>", form, flags=re.I | re.S):
        attrs = parse_attrs("<select " + select_match.group(1) + ">")
        name = attrs.get("name")
        if not name:
            continue
        selected = re.search(r"<option\b([^>]*)>(.*?)</option>", select_match.group(2), flags=re.I | re.S)
        for option_match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_match.group(2), flags=re.I | re.S):
            option_attrs = parse_attrs("<option " + option_match.group(1) + ">")
            if "selected" in option_attrs:
                selected = option_match
                break
        option_attrs = parse_attrs("<option " + selected.group(1) + ">") if selected else {}
        values.append((name, option_attrs.get("value") or ""))

    if page is not None and not any(name == "current_page" for name, _ in values):
        values.append(("current_page", str(page)))
    return values


def parse_deck_count(source: str, deck_rows: int) -> int:
    match = re.search(r"(\d+)\s+decks matching", source, flags=re.I)
    return int(match.group(1)) if match else deck_rows


def parse_search_rows(source: str) -> list[dict]:
    decks: list[dict] = []
    seen: set[str] = set()

    for row in re.findall(r"<tr\b[^>]*class=[\"']?hover_tr[\"']?[^>]*>.*?</tr>", source, flags=re.I | re.S):
        ref_match = re.search(r"name=[\"']?deck_ref\[\d+\][\"']?\s+value=[\"']?(\d+)[\"']?", row, flags=re.I)
        if not ref_match:
            continue
        deck_id = ref_match.group(1)
        if deck_id in seen:
            continue

        link_match = re.search(r"href=[\"']?(?:https?://www\.mtgtop8\.com/)?event\?([^\"'>\s]+)[\"']?>(.*?)</a>", row, flags=re.I | re.S)
        query = parse_qs(html.unescape(link_match.group(1))) if link_match else {}
        event_id = query.get("e", [""])[0]
        fmt = query.get("f", [""])[0]
        deck_name = strip_tags(link_match.group(2)) if link_match else ""
        cells = split_tds(row)
        player = strip_tags(cells[2]) if len(cells) > 2 else ""
        fmt_text = strip_tags(cells[3]) if len(cells) > 3 else fmt
        event = strip_tags(cells[4]) if len(cells) > 4 else ""
        stars, big_star = parse_star_cell(cells[5]) if len(cells) > 5 else (0, False)
        rank = strip_tags(cells[6]) if len(cells) > 6 else ""
        date = strip_tags(cells[7]) if len(cells) > 7 else ""
        deck_url = f"{MTGTOP8}/event?e={event_id}&d={deck_id}"
        if fmt:
            deck_url += f"&f={fmt}"

        decks.append(
            {
                "deck_id": deck_id,
                "event_id": event_id,
                "format": fmt or fmt_text,
                "format_name": fmt_text,
                "deck_name": deck_name,
                "player": player,
                "event": event,
                "rank": rank,
                "date": date,
                "deck_url": deck_url,
                "tournament_stars": stars,
                "tournament_big_star": big_star,
            }
        )
        seen.add(deck_id)
    return decks


SNOW_BASIC_ALIASES = {
    "Snow-Covered Plains": "Plains",
    "Snow-Covered Island": "Island",
    "Snow-Covered Swamp": "Swamp",
    "Snow-Covered Mountain": "Mountain",
    "Snow-Covered Forest": "Forest",
}


def parse_aliases(alias_text: str, *, snow_basics_as_basics: bool = False) -> dict[str, str]:
    aliases: dict[str, str] = {}

    def display_name(name: str) -> str:
        if any(char.isupper() for char in name):
            return name
        return re.sub(r"(?<!['’])\b([a-z])", lambda match: match.group(1).upper(), name)

    def add_alias(name: str, canonical: str) -> None:
        aliases[name] = canonical
        aliases[name.casefold()] = canonical

    if snow_basics_as_basics:
        for name, canonical in SNOW_BASIC_ALIASES.items():
            add_alias(name, canonical)

    for line in alias_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            canonical, raw_aliases = line.split(":", 1)
            canonical = display_name(canonical.strip())
            names = [name.strip() for name in re.split(r",|\|", raw_aliases) if name.strip()]
            for name in [canonical, *names]:
                add_alias(name, canonical)
        elif "=" in line:
            canonical, raw_aliases = line.split("=", 1)
            canonical = display_name(canonical.strip())
            names = [name.strip() for name in re.split(r",|\|", raw_aliases) if name.strip()]
            for name in [canonical, *names]:
                add_alias(name, canonical)
        else:
            names = [name.strip() for name in re.split(r",|\|", line) if name.strip()]
            if len(names) > 1:
                canonical = display_name(names[0])
                for name in names:
                    add_alias(name, canonical)
    return aliases


def canonical_card_name(card: str, aliases: dict[str, str]) -> str:
    return aliases.get(card) or aliases.get(card.casefold()) or card


CLUSTER_ARCHETYPE_SECTIONS = [
    ("LANDS", "Lands"),
    ("CREATURES", "Creatures"),
    ("OTHER SPELLS", "Other Spells"),
    ("SIDEBOARDS", "Sideboard"),
]


def build_cluster_archetypes(
    records: list[dict],
    deck_ids: list[str] | pd.Index,
    labels: np.ndarray,
    aliases: dict[str, str],
) -> list[dict]:
    label_by_deck = {str(deck_id): int(label) for deck_id, label in zip(deck_ids, labels)}
    if not label_by_deck:
        return []

    df = pd.DataFrame(records)
    if df.empty:
        return []

    df["deck_id"] = df["deck_id"].map(str)
    df = df[df["deck_id"].isin(label_by_deck)].copy()
    if df.empty:
        return []

    df["cluster"] = df["deck_id"].map(label_by_deck)
    df["card_norm"] = df["card"].map(lambda card: canonical_card_name(str(card), aliases))

    deck_ids_by_cluster: dict[int, list[str]] = {}
    for deck_id, label in label_by_deck.items():
        deck_ids_by_cluster.setdefault(label, []).append(deck_id)

    cluster_archetypes = []
    for cluster in sorted(deck_ids_by_cluster):
        cluster_decks = deck_ids_by_cluster[cluster]
        deck_count = len(cluster_decks)
        if deck_count == 0:
            continue

        cluster_df = df[df["cluster"] == cluster]
        sections = []
        for section_key, section_label in CLUSTER_ARCHETYPE_SECTIONS:
            section_df = cluster_df[cluster_df["section"] == section_key]
            if section_df.empty:
                continue

            deck_card_totals = (
                section_df.groupby(["deck_id", "card_norm"], as_index=False)["copies"].sum()
            )
            cards = []
            for card_name, card_rows in deck_card_totals.groupby("card_norm"):
                copy_counts = Counter(int(value) for value in card_rows["copies"])
                zero_count = deck_count - len(card_rows)
                if zero_count > 0:
                    copy_counts[0] = zero_count
                played_decks = deck_count - copy_counts.get(0, 0)
                played_pct = played_decks / deck_count if deck_count else 0
                avg_copies = float(card_rows["copies"].sum()) / deck_count if deck_count else 0.0
                cards.append(
                    {
                        "name": str(card_name),
                        "dist": {
                            str(qty): int(count)
                            for qty, count in sorted(copy_counts.items(), key=lambda item: int(item[0]), reverse=True)
                        },
                        "played_decks": int(played_decks),
                        "played_pct": float(played_pct),
                        "avg": avg_copies,
                    }
                )

            cards.sort(key=lambda card: (-card["played_pct"], -card["avg"], card["name"]))
            if not cards:
                continue
            sections.append(
                {
                    "key": section_key.lower().replace(" ", "_"),
                    "label": section_label,
                    "cards": cards,
                }
            )

        cluster_archetypes.append(
            {
                "cluster": int(cluster),
                "n": int(deck_count),
                "sections": sections,
            }
        )

    return cluster_archetypes


def build_decklists(records: list[dict], deck_ids: list[str] | pd.Index) -> dict[str, list[dict]]:
    wanted = {str(deck_id) for deck_id in deck_ids}
    section_order = {section: index for index, (section, _) in enumerate(CLUSTER_ARCHETYPE_SECTIONS)}
    cards_by_deck: dict[str, dict[tuple[str, str], dict]] = {}

    for index, record in enumerate(records):
        deck_id = str(record.get("deck_id") or "")
        if deck_id not in wanted:
            continue
        section = str(record.get("section") or "")
        card = str(record.get("card") or "")
        copies = int(record.get("copies") or 0)
        if not section or not card or copies <= 0:
            continue

        key = (section, card)
        deck_cards = cards_by_deck.setdefault(deck_id, {})
        if key not in deck_cards:
            deck_cards[key] = {
                "section": section,
                "name": card,
                "copies": 0,
                "first_seen": index,
            }
        deck_cards[key]["copies"] += copies

    card_metadata = fetch_scryfall_card_metadata(
        [
            str(card["name"])
            for deck_cards in cards_by_deck.values()
            for card in deck_cards.values()
        ]
    )

    decklists: dict[str, list[dict]] = {}
    for deck_id in wanted:
        cards = sorted(
            cards_by_deck.get(deck_id, {}).values(),
            key=lambda card: (section_order.get(str(card["section"]), 99), int(card["first_seen"])),
        )
        sections = []
        for section, label in CLUSTER_ARCHETYPE_SECTIONS:
            section_cards = [
                {
                    "name": str(card["name"]),
                    "copies": int(card["copies"]),
                    "mana_cost": str(card_metadata.get(str(card["name"]), {}).get("mana_cost") or ""),
                }
                for card in cards
                if card["section"] == section
            ]
            if section_cards:
                sections.append(
                    {
                        "key": section.lower().replace(" ", "_"),
                        "label": label,
                        "total": sum(card["copies"] for card in section_cards),
                        "cards": section_cards,
                    }
                )
        decklists[deck_id] = sections

    return decklists


def batched(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def sorted_colors(colors: list[str] | set[str]) -> list[str]:
    present = set(colors)
    return [color for color in COLOR_ORDER if color in present]


def colors_from_scryfall_card(card: dict) -> list[str]:
    colors = card.get("color_identity")
    if colors is None:
        colors = card.get("colors")
    if colors is None and isinstance(card.get("card_faces"), list):
        face_colors: set[str] = set()
        for face in card["card_faces"]:
            face_colors.update(face.get("color_identity") or face.get("colors") or [])
        colors = list(face_colors)
    return sorted_colors(colors or [])


def mana_cost_from_scryfall_card(card: dict) -> str:
    mana_cost = card.get("mana_cost")
    if not mana_cost and isinstance(card.get("card_faces"), list):
        face_costs = [
            str(face.get("mana_cost") or "")
            for face in card["card_faces"]
            if str(face.get("mana_cost") or "").strip()
        ]
        mana_cost = " // ".join(face_costs)
    return str(mana_cost or "")


def metadata_from_scryfall_card(card: dict) -> dict[str, object]:
    return {
        "colors": colors_from_scryfall_card(card),
        "mana_cost": mana_cost_from_scryfall_card(card),
    }


def fetch_scryfall_card_metadata(card_names: list[str]) -> dict[str, dict[str, object]]:
    unique_names = sorted({name for name in card_names if name})
    for name in unique_names:
        if name in SCRYFALL_CARD_METADATA:
            continue
        persisted = disk_cache_get(disk_cache_key("scryfall-card-metadata-v1", name.lower()))
        if persisted is None:
            continue
        try:
            metadata = json.loads(persisted)
        except json.JSONDecodeError:
            continue
        if isinstance(metadata, dict):
            colors = metadata.get("colors") if isinstance(metadata.get("colors"), list) else []
            SCRYFALL_CARD_METADATA[name] = {
                "colors": [str(color) for color in colors],
                "mana_cost": str(metadata.get("mana_cost") or ""),
            }

    missing = [name for name in unique_names if name not in SCRYFALL_CARD_METADATA]
    for batch in batched(missing, 75):
        body = json.dumps({"identifiers": [{"name": name} for name in batch]}).encode("utf-8")
        request = Request(
            SCRYFALL_COLLECTION,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            for name in batch:
                SCRYFALL_CARD_METADATA.setdefault(name, {"colors": [], "mana_cost": ""})
            continue

        found_by_name: dict[str, dict[str, object]] = {}
        for card in payload.get("data", []):
            found_by_name[str(card.get("name") or "").lower()] = metadata_from_scryfall_card(card)

        not_found = {
            str(identifier.get("name") or "").lower()
            for identifier in payload.get("not_found", [])
            if isinstance(identifier, dict)
        }
        for name in batch:
            metadata = found_by_name.get(name.lower(), {"colors": [], "mana_cost": ""})
            if name.lower() in not_found:
                metadata = {"colors": [], "mana_cost": ""}
            colors = metadata.get("colors") if isinstance(metadata.get("colors"), list) else []
            normalized_metadata = {
                "colors": [str(color) for color in colors],
                "mana_cost": str(metadata.get("mana_cost") or ""),
            }
            SCRYFALL_CARD_METADATA[name] = normalized_metadata
            disk_cache_set(
                disk_cache_key("scryfall-card-metadata-v1", name.lower()),
                json.dumps(normalized_metadata),
                SCRYFALL_COLOR_CACHE_TTL,
            )

        if len(missing) > 75:
            time.sleep(0.55)

    return {name: SCRYFALL_CARD_METADATA.get(name, {"colors": [], "mana_cost": ""}) for name in unique_names}


def fetch_scryfall_card_colors(card_names: list[str]) -> dict[str, list[str]]:
    metadata = fetch_scryfall_card_metadata(card_names)
    colors_by_card: dict[str, list[str]] = {}
    for name, card_metadata in metadata.items():
        colors = card_metadata.get("colors") if isinstance(card_metadata, dict) else []
        colors_by_card[name] = [str(color) for color in colors] if isinstance(colors, list) else []
    return colors_by_card


def deck_colors_from_records(records: list[dict]) -> dict[str, list[str]]:
    maindeck_records = [record for record in records if record.get("section") != "SIDEBOARDS"]
    card_colors = fetch_scryfall_card_colors([str(record.get("card") or "") for record in maindeck_records])
    deck_colors: dict[str, set[str]] = {}
    for record in maindeck_records:
        deck_id = str(record.get("deck_id") or "")
        if not deck_id:
            continue
        deck_colors.setdefault(deck_id, set()).update(card_colors.get(str(record.get("card") or ""), []))
    return {deck_id: sorted_colors(colors) for deck_id, colors in deck_colors.items()}


def merge_deck_meta(compare_meta: dict, search_meta: dict) -> dict:
    merged = {**compare_meta, **search_meta}
    compare_rank = str(compare_meta.get("rank") or "")
    search_rank = str(search_meta.get("rank") or "")
    if "/" in compare_rank and "/" not in search_rank:
        merged["rank"] = compare_rank

    merged["tournament_stars"] = max(
        int(compare_meta.get("tournament_stars") or 0),
        int(search_meta.get("tournament_stars") or 0),
    )
    merged["tournament_big_star"] = bool(compare_meta.get("tournament_big_star")) or bool(
        search_meta.get("tournament_big_star")
    )
    return merged


def cosine_distance_matrix(values: np.ndarray) -> np.ndarray:
    ensure_pairwise_deck_limit(len(values), "Cosine distance")
    norms = np.linalg.norm(values, axis=1)
    denom = np.outer(norms, norms)
    similarity = np.divide(values @ values.T, denom, out=np.zeros_like(denom), where=denom > 0)
    distances = 1 - np.clip(similarity, -1, 1)
    np.fill_diagonal(distances, 0)
    return distances


def bray_curtis_distance_matrix(values: np.ndarray) -> np.ndarray:
    ensure_pairwise_deck_limit(len(values), "Bray-Curtis distance")
    numerator = np.abs(values[:, None, :] - values[None, :, :]).sum(axis=2)
    denominator = (values[:, None, :] + values[None, :, :]).sum(axis=2)
    distances = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 0)
    np.fill_diagonal(distances, 0)
    return distances


def euclidean_distance_matrix(values: np.ndarray) -> np.ndarray:
    ensure_pairwise_deck_limit(len(values), "Euclidean distance")
    distances = np.sqrt(((values[:, None, :] - values[None, :, :]) ** 2).sum(axis=2))
    np.fill_diagonal(distances, 0)
    return distances


def ensure_pairwise_deck_limit(deck_count: int, operation: str) -> None:
    if deck_count > PAIRWISE_DECK_LIMIT:
        raise ValueError(
            f"{operation} needs all deck-by-deck distances for {deck_count:,} decks. "
            f"That is too large for this local app limit of {PAIRWISE_DECK_LIMIT:,}. "
            "Use UMAP/PCA with plot-space clustering or narrow the search."
        )


def pcoa(distance_matrix: np.ndarray) -> tuple[np.ndarray, list[float]]:
    n = distance_matrix.shape[0]
    centering = np.eye(n) - np.ones((n, n)) / n
    gram = -0.5 * centering @ (distance_matrix**2) @ centering
    eigenvalues, eigenvectors = np.linalg.eigh(gram)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    eigenvectors = eigenvectors[:, order]

    positive = np.maximum(eigenvalues, 0)
    coords = eigenvectors[:, :2] * np.sqrt(positive[:2])
    if coords.shape[1] < 2:
        coords = np.pad(coords, ((0, 0), (0, 2 - coords.shape[1])))

    total = positive[positive > 1e-12].sum()
    explained = [(float(value) / float(total)) if total else 0.0 for value in positive[:2]]
    return coords, explained


def projection_for_features(values: np.ndarray, projection: str) -> tuple[np.ndarray, list[float | None], np.ndarray | None, dict]:
    if projection == "pca":
        pca = PCA(n_components=2, random_state=1)
        coords = pca.fit_transform(values)
        return coords, [float(value) for value in pca.explained_variance_ratio_], None, {
            "projection": "pca",
            "projection_label": "PCA",
            "axis_labels": ["PC1", "PC2"],
            "distance_metric": None,
        }

    if projection == "pcoa_cosine":
        distances = cosine_distance_matrix(values)
        coords, explained = pcoa(distances)
        return coords, explained, distances, {
            "projection": projection,
            "projection_label": "Cosine PCoA",
            "axis_labels": ["PCoA1", "PCoA2"],
            "distance_metric": "cosine",
        }

    if projection == "pcoa_braycurtis":
        distances = bray_curtis_distance_matrix(values)
        coords, explained = pcoa(distances)
        return coords, explained, distances, {
            "projection": projection,
            "projection_label": "Bray-Curtis PCoA",
            "axis_labels": ["PCoA1", "PCoA2"],
            "distance_metric": "bray-curtis",
        }

    if projection == "umap_braycurtis":
        try:
            from umap import UMAP
        except Exception as exc:
            raise ValueError("UMAP is unavailable. Install umap-learn, then restart the app.") from exc

        n_neighbors = max(2, min(15, values.shape[0] - 1))
        reducer = UMAP(
            n_components=2,
            n_neighbors=n_neighbors,
            min_dist=0.1,
            metric="braycurtis",
            init="random",
            n_jobs=1,
            random_state=1,
        )
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="n_jobs value .*")
            coords = reducer.fit_transform(values)
        return coords, [None, None], None, {
            "projection": projection,
            "projection_label": "UMAP",
            "axis_labels": ["UMAP1", "UMAP2"],
            "distance_metric": "bray-curtis",
            "umap_metric": "braycurtis",
            "umap_neighbors": n_neighbors,
            "umap_min_dist": 0.1,
        }

    raise ValueError("Unknown projection.")


def snap_duplicate_feature_coords(coords: np.ndarray, features: pd.DataFrame) -> tuple[np.ndarray, dict[str, int]]:
    row_hashes = pd.util.hash_pandas_object(features, index=False).to_numpy()
    duplicate_groups = 0
    duplicate_decks = 0
    snapped = np.array(coords, copy=True)

    for row_hash in pd.unique(row_hashes):
        indices = np.flatnonzero(row_hashes == row_hash)
        if len(indices) < 2:
            continue
        duplicate_groups += 1
        duplicate_decks += len(indices)
        snapped[indices] = snapped[indices].mean(axis=0)

    return snapped, {
        "duplicate_feature_groups": duplicate_groups,
        "duplicate_feature_decks": duplicate_decks,
    }


def silhouette_for_labels(values: np.ndarray, labels: np.ndarray, *, distance_matrix: np.ndarray | None = None) -> float | None:
    labels = np.asarray(labels)
    if len(labels) > SILHOUETTE_DECK_LIMIT:
        return None
    if np.any(labels < 0):
        keep = labels >= 0
        labels = labels[keep]
        values = values[keep]
        if distance_matrix is not None:
            distance_matrix = distance_matrix[np.ix_(keep, keep)]

    unique = set(int(label) for label in labels)
    if len(unique) < 2 or len(unique) >= len(labels):
        return None
    if distance_matrix is not None:
        distances = np.array(distance_matrix, copy=True)
        np.fill_diagonal(distances, 0)
        return float(silhouette_score(distances, labels, metric="precomputed"))
    return float(silhouette_score(values, labels))


def labels_by_descending_size(labels: np.ndarray) -> np.ndarray:
    labels = np.asarray(labels, dtype=int)
    counts = Counter(int(label) for label in labels if int(label) >= 0)
    ordered = sorted(counts, key=lambda label: (-counts[label], label))
    remap = {old_label: new_label for new_label, old_label in enumerate(ordered)}
    return np.array([remap.get(int(label), int(label)) for label in labels], dtype=int)


def auto_min_branch_size(n: int) -> int:
    if n < 3:
        return 1
    return max(2, min(8, math.ceil(n * 0.01)))


def choose_agglomerative_k(distance_matrix: np.ndarray, max_k: int = 8, allow_tiny: bool = False) -> tuple[int, int]:
    n = len(distance_matrix)
    if n < 3:
        return 1, 1
    min_branch_size = auto_min_branch_size(n)
    distances = np.array(distance_matrix, copy=True)
    np.fill_diagonal(distances, 0)

    candidates: list[tuple[float, int]] = []
    fallback: list[tuple[float, int]] = []
    max_k = min(max_k, n - 1)
    for k in range(2, max_k + 1):
        labels = AgglomerativeClustering(
            n_clusters=k,
            metric="precomputed",
            linkage="average",
        ).fit_predict(distances)
        smallest_cluster = min(np.bincount(labels))
        silhouette = silhouette_for_labels(np.empty((n, 0)), labels, distance_matrix=distances)
        if silhouette is None:
            continue
        fallback.append((silhouette, k))
        if smallest_cluster >= min_branch_size:
            candidates.append((silhouette, k))
    if candidates:
        return max(candidates)[1], min_branch_size
    if allow_tiny and fallback:
        return max(fallback)[1], min_branch_size
    return 1, min_branch_size


def robust_high_threshold(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values) & (values > 0)]
    if len(values) < 4:
        return None
    median = float(np.median(values))
    q1, q3 = np.percentile(values, [25, 75])
    iqr = float(q3 - q1)
    mad = float(np.median(np.abs(values - median)))

    thresholds = []
    if mad > 0:
        thresholds.append(median + 10 * 1.4826 * mad)
    if iqr > 0:
        thresholds.append(float(q3) + 8 * iqr)
    if median > 0:
        thresholds.append(median * 6)
    return max(thresholds) if thresholds else None


def detect_isolated_noise(distance_matrix: np.ndarray, min_branch_size: int) -> tuple[np.ndarray, dict]:
    n = len(distance_matrix)
    empty = np.zeros(n, dtype=bool)
    if n < max(8, min_branch_size + 2):
        return empty, {
            "noise_decks": 0,
            "noise_neighbor_k": None,
            "noise_threshold": None,
        }

    clustering_distances = np.array(distance_matrix, copy=True)
    np.fill_diagonal(clustering_distances, 0)
    split_labels = AgglomerativeClustering(
        n_clusters=2,
        metric="precomputed",
        linkage="average",
    ).fit_predict(clustering_distances)
    split_counts = np.bincount(split_labels)
    tiny_split_labels = {index for index, count in enumerate(split_counts) if count < min_branch_size}
    if not tiny_split_labels:
        return empty, {
            "noise_decks": 0,
            "noise_neighbor_k": None,
            "noise_threshold": None,
        }

    candidate_mask = np.array([label in tiny_split_labels for label in split_labels], dtype=bool)
    distances = np.array(distance_matrix, copy=True)
    np.fill_diagonal(distances, np.inf)
    neighbor_k = min(max(3, min_branch_size), n - 1)
    sorted_distances = np.sort(distances, axis=1)
    nearest_distances = sorted_distances[:, 0]
    kth_distances = sorted_distances[:, neighbor_k - 1]
    nearest_threshold = robust_high_threshold(nearest_distances)
    kth_threshold = robust_high_threshold(kth_distances)
    if nearest_threshold is None or kth_threshold is None:
        return empty, {
            "noise_decks": 0,
            "noise_neighbor_k": neighbor_k,
            "noise_threshold": None,
        }

    noise_mask = candidate_mask & (nearest_distances > nearest_threshold) & (kth_distances > kth_threshold)

    max_noise = max(1, min(8, math.floor(n * 0.03)))
    if int(noise_mask.sum()) > max_noise:
        ranked = np.argsort(kth_distances)[::-1]
        keep = [index for index in ranked if noise_mask[index]][:max_noise]
        capped_mask = np.zeros(n, dtype=bool)
        capped_mask[keep] = True
        noise_mask = capped_mask

    return noise_mask, {
        "noise_decks": int(noise_mask.sum()),
        "noise_neighbor_k": neighbor_k,
        "noise_threshold": float(kth_threshold),
        "noise_nearest_threshold": float(nearest_threshold),
    }


def hdbscan_labels(
    values: np.ndarray,
    *,
    distance_matrix: np.ndarray | None,
    scale_clusters: bool,
) -> tuple[np.ndarray, float | None, dict]:
    min_cluster_size = max(3, min(10, math.ceil(len(values) * 0.06)))
    min_samples = 1
    if distance_matrix is not None:
        distances = np.array(distance_matrix, copy=True)
        np.fill_diagonal(distances, 0)
        labels = HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            metric="precomputed",
            copy=True,
        ).fit_predict(distances)
        silhouette = silhouette_for_labels(values, labels, distance_matrix=distance_matrix)
    else:
        cluster_values = StandardScaler().fit_transform(values) if scale_clusters else values
        labels = HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            copy=True,
        ).fit_predict(cluster_values)
        silhouette = silhouette_for_labels(cluster_values, labels)
    return labels, silhouette, {"min_cluster_size": min_cluster_size, "min_samples": min_samples}


def cluster_features(
    values: np.ndarray,
    *,
    distance_matrix: np.ndarray | None,
    cluster_method: str,
    cluster_k: int,
    scale_clusters: bool,
    outlier_mode: str,
) -> tuple[np.ndarray, float | None, str, dict]:
    if cluster_method == "auto":
        if distance_matrix is None and len(values) > PAIRWISE_DECK_LIMIT:
            labels, silhouette, meta = hdbscan_labels(values, distance_matrix=None, scale_clusters=scale_clusters)
            non_noise_clusters = len({int(label) for label in labels if int(label) >= 0})
            noise_decks = int(np.sum(labels < 0))
            return labels, silhouette, "hdbscan", {
                **meta,
                "auto_fallback": "hdbscan",
                "auto_clusters": non_noise_clusters,
                "noise_decks": noise_decks,
                "pairwise_deck_limit": PAIRWISE_DECK_LIMIT,
            }

        if distance_matrix is not None:
            cluster_distance_matrix = np.array(distance_matrix, copy=True)
            np.fill_diagonal(cluster_distance_matrix, 0)
        else:
            cluster_values = StandardScaler().fit_transform(values) if scale_clusters else values
            cluster_distance_matrix = euclidean_distance_matrix(cluster_values)

        if outlier_mode == "noise":
            noise_mask, noise_meta = detect_isolated_noise(
                cluster_distance_matrix,
                auto_min_branch_size(len(cluster_distance_matrix)),
            )
        else:
            noise_mask = np.zeros(len(values), dtype=bool)
            noise_meta = {
                "noise_decks": 0,
                "noise_neighbor_k": None,
                "noise_threshold": None,
            }
        clean_indices = np.flatnonzero(~noise_mask)
        labels = np.full(len(values), -1, dtype=int)
        if len(clean_indices) == 0:
            return labels, None, "auto-linkage", {"auto_clusters": 0, "min_branch_size": 1, **noise_meta}

        clean_distance_matrix = cluster_distance_matrix[np.ix_(clean_indices, clean_indices)]
        auto_k, min_branch_size = choose_agglomerative_k(clean_distance_matrix, allow_tiny=outlier_mode == "keep")
        if auto_k <= 1:
            labels[clean_indices] = 0
            return labels, None, "auto-linkage", {"auto_clusters": 1, "min_branch_size": min_branch_size, **noise_meta}

        clean_labels = AgglomerativeClustering(
            n_clusters=auto_k,
            metric="precomputed",
            linkage="average",
        ).fit_predict(clean_distance_matrix)
        labels[clean_indices] = clean_labels
        silhouette = silhouette_for_labels(values[clean_indices], clean_labels, distance_matrix=clean_distance_matrix)
        return labels, silhouette, "auto-linkage", {"auto_clusters": auto_k, "min_branch_size": min_branch_size, **noise_meta}

    if cluster_method == "hdbscan":
        labels, silhouette, meta = hdbscan_labels(values, distance_matrix=distance_matrix, scale_clusters=scale_clusters)
        return labels, silhouette, "hdbscan", meta

    if cluster_method != "fixed":
        raise ValueError("Unknown clustering method.")

    cluster_k = min(cluster_k, len(values))
    if cluster_k <= 1:
        return np.zeros(len(values), dtype=int), None, "none", {}

    if distance_matrix is not None:
        labels = AgglomerativeClustering(
            n_clusters=cluster_k,
            metric="precomputed",
            linkage="average",
        ).fit_predict(distance_matrix)
        silhouette = silhouette_for_labels(values, labels, distance_matrix=distance_matrix)
        return labels, silhouette, "average-linkage", {}

    cluster_values = StandardScaler().fit_transform(values) if scale_clusters else values
    labels = KMeans(n_clusters=cluster_k, n_init=100, random_state=1).fit_predict(cluster_values)
    silhouette = silhouette_for_labels(cluster_values, labels)
    return labels, silhouette, "k-means", {}


def collect_decks_and_cards(
    source: str,
    source_kind: str,
    source_value: str,
    progress: Callable[[str], None] | None = None,
    fetch_stats: FetchStats | None = None,
    empty_search_message: str | None = None,
) -> tuple[list[dict], list[dict], dict]:
    if find_compare_table(source) is not None:
        decks, records = parse_compare_html(source, source_value)
        return decks, records, {"input_type": "compare"}

    first_page_decks = parse_search_rows(source)
    if not first_page_decks:
        if source_kind in {"search_builder", "post_body", "curl_post_body", "url"} and find_search_form(source):
            raise ValueError(empty_search_message or "No MTGTop8 decks matched that search.")
        raise ValueError("Could not find MTGTop8 search results or a compare table in the input.")

    total_count = parse_deck_count(source, len(first_page_decks))
    page_count = max(1, math.ceil(total_count / 25))
    form_values = extract_search_form(source)
    search_pages = [source]

    if form_values and page_count > 1:
        current_page_values = [value for name, value in form_values if name == "current_page"]
        try:
            source_page = int(current_page_values[0]) if current_page_values and current_page_values[0] else 1
        except ValueError:
            source_page = 1
        post_url = f"{MTGTOP8}/search"
        for page in range(1, page_count + 1):
            if source_kind in {"post_body", "curl_post_body", "search_builder"} and page == source_page:
                continue
            if progress:
                progress(f"Fetching search page {page}/{page_count}...")
            data = extract_search_form(source, page)
            search_pages.append(fetch_url(post_url, data=data, stats=fetch_stats))

    search_decks_by_id: dict[str, dict] = {}
    for page_html in search_pages:
        for deck in parse_search_rows(page_html):
            search_decks_by_id.setdefault(deck["deck_id"], deck)

    deck_ids = list(search_decks_by_id)
    if not deck_ids:
        raise ValueError("No deck IDs found in the search results.")

    compare_decks: dict[str, dict] = {}
    records: list[dict] = []
    compare_deck_ids = sorted(deck_ids, key=deck_id_sort_key)
    compare_batches = batched(compare_deck_ids, 25)
    for i, batch in enumerate(compare_batches, start=1):
        if progress:
            progress(f"Fetching deck comparison {i}/{len(compare_batches)}...")
        compare_url = f"{MTGTOP8}/compare?l=_" + "_".join(batch) + "_"
        compare_html = fetch_url(compare_url, stats=fetch_stats)
        batch_decks, batch_records = parse_compare_html(compare_html, compare_url)
        for deck in batch_decks:
            compare_decks[deck["deck_id"]] = deck
        records.extend(batch_records)

    decks: list[dict] = []
    for deck_id in deck_ids:
        decks.append(merge_deck_meta(compare_decks.get(deck_id, {}), search_decks_by_id[deck_id]))

    return decks, records, {"input_type": "search", "reported_count": total_count, "pages": page_count, "compare_batches": len(compare_batches)}


def criteria_search_intent(criteria: dict | None) -> tuple[bool, bool]:
    if not isinstance(criteria, dict):
        return False, False
    deckish = bool(str(criteria.get("deck") or "").strip() or str(criteria.get("archetype") or "").strip())
    cardish = bool(str(criteria.get("cards") or "").strip())
    return deckish, cardish


def no_search_results_message(criteria: dict | None) -> str:
    if not isinstance(criteria, dict):
        return "No MTGTop8 decks matched that search."

    cards = str(criteria.get("cards") or "").strip()
    format_name = str(criteria.get("format") or "").strip()
    date_start = str(criteria.get("dateStart") or "").strip()
    date_end = str(criteria.get("dateEnd") or "").strip()
    main_deck = bool(criteria.get("mainDeck", True))
    sideboard = bool(criteria.get("sideboard", False))

    zones = []
    if main_deck:
        zones.append("main deck")
    if sideboard:
        zones.append("sideboard")
    zone_text = " or ".join(zones) if zones else "selected zones"

    details = []
    if cards:
        card_text = ", ".join(line.strip() for line in re.split(r"\r?\n", cards) if line.strip())
        details.append(f"with {card_text} in the {zone_text}")
    if format_name:
        details.append(f"in {format_name}")
    if date_start or date_end:
        if date_start and date_end:
            details.append(f"from {date_start} to {date_end}")
        elif date_start:
            details.append(f"from {date_start}")
        else:
            details.append(f"through {date_end}")

    suffix = " " + " ".join(details) if details else ""
    return f"No MTGTop8 decks matched that search{suffix}. Try widening the date range or changing the card/zone filters."


def form_search_intent(values: list[tuple[str, str]]) -> tuple[bool, bool]:
    deckish = False
    cardish = False
    for name, value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if name == "deck_titre" or name.startswith("archetype_sel["):
            deckish = True
        elif name == "cards":
            cardish = True
    return deckish, cardish


def resolve_outlier_mode(requested_mode: str, search_criteria: dict | None, source: str) -> str:
    if requested_mode in {"noise", "keep"}:
        return requested_mode
    deckish, cardish = criteria_search_intent(search_criteria)
    if not deckish and not cardish:
        deckish, cardish = form_search_intent(extract_search_form(source))
    if deckish:
        return "noise"
    if cardish:
        return "keep"
    return "keep"


def analyze(payload: dict, progress: Callable[[str], None] | None = None) -> dict:
    source_value = (payload.get("source") or "").strip()
    search_criteria = payload.get("search") if isinstance(payload.get("search"), dict) else None
    fetch_stats = FetchStats()

    if not source_value and not search_criteria:
        raise ValueError("Use the MTGTop8 search builder, or paste a search URL, compare URL, POST body, or saved HTML.")

    if search_criteria and not source_value:
        if progress:
            progress("Fetching search page 1...")
        form_data = build_search_form(search_criteria)
        source = fetch_url(f"{MTGTOP8}/search", data=form_data, stats=fetch_stats)
        source_kind = "search_builder"
        source_value = urlencode(form_data, doseq=True)
    elif curl_body := extract_post_body_from_curl(source_value):
        if progress:
            progress("Fetching search page 1...")
        form_data = parse_qsl(curl_body, keep_blank_values=True)
        source = fetch_url(f"{MTGTOP8}/search", data=form_data, stats=fetch_stats)
        source_kind = "curl_post_body"
    elif looks_like_html(source_value):
        source = source_value
        source_kind = "html"
    elif looks_like_post_body(source_value):
        if progress:
            progress("Fetching search page 1...")
        form_data = parse_qsl(source_value, keep_blank_values=True)
        source = fetch_url(f"{MTGTOP8}/search", data=form_data, stats=fetch_stats)
        source_kind = "post_body"
    else:
        if progress:
            progress("Fetching MTGTop8 page...")
        source = fetch_url(source_value, stats=fetch_stats)
        source_kind = "url"

    decks, records, input_meta = collect_decks_and_cards(
        source,
        source_kind,
        source_value,
        progress,
        fetch_stats,
        empty_search_message=no_search_results_message(search_criteria),
    )
    if not records:
        raise ValueError("No card rows found for the selected decks.")

    if progress:
        progress("Analyzing...")

    deck_ids = [deck["deck_id"] for deck in decks]
    alias_map = parse_aliases(
        payload.get("aliases") or "",
        snow_basics_as_basics=bool(payload.get("snowBasicsAsBasics")),
    )
    scope = payload.get("scope") or "maindeck"
    min_decks = max(1, int(payload.get("minDecks") or 2))
    cluster_k = max(1, int(payload.get("clusterK") or 2))
    scale_clusters = bool(payload.get("scaleClusters", True))
    projection = payload.get("projection") or "umap_braycurtis"
    cluster_method_requested = payload.get("clusterMethod") or "auto"
    cluster_space = str(payload.get("clusterSpace") or "plot")
    if cluster_space not in {"deck", "plot"}:
        cluster_space = "plot"
    outlier_mode_requested = str(payload.get("outlierMode") or "auto")
    outlier_mode = resolve_outlier_mode(outlier_mode_requested, search_criteria, source)

    deck_colors = deck_colors_from_records(records)
    df = pd.DataFrame(records)
    if scope == "maindeck":
        df = df[df["section"] != "SIDEBOARDS"].copy()
    elif scope == "sideboard":
        df = df[df["section"] == "SIDEBOARDS"].copy()
    elif scope != "full75":
        raise ValueError("Unknown scope.")

    df["card_norm"] = df["card"].map(lambda card: canonical_card_name(str(card), alias_map))
    matrix = df.pivot_table(index="deck_id", columns="card_norm", values="copies", aggfunc="sum", fill_value=0)
    matrix = matrix.reindex(deck_ids, fill_value=0).astype(float)
    deck_totals = matrix.sum(axis=1).to_dict()

    present = (matrix > 0).sum(axis=0)
    features = matrix.loc[:, (present >= min_decks) & (matrix.var(axis=0) > 0)]
    if features.shape[0] < 2:
        raise ValueError(f"Need at least two decks with {scope} card data to plot.")
    if features.shape[1] < 2:
        raise ValueError(f"Need at least two variable {scope} card columns to plot.")

    coords, explained_variance, distance_matrix, projection_meta = projection_for_features(features.values, projection)
    coords, duplicate_meta = snap_duplicate_feature_coords(coords, features)
    if cluster_space == "deck":
        cluster_values = features.values
        cluster_distance_matrix = distance_matrix
    else:
        cluster_values = coords
        cluster_distance_matrix = None

    labels, silhouette, cluster_method, cluster_meta = cluster_features(
        cluster_values,
        distance_matrix=cluster_distance_matrix,
        cluster_method=cluster_method_requested,
        cluster_k=cluster_k,
        scale_clusters=scale_clusters,
        outlier_mode=outlier_mode,
    )
    labels = labels_by_descending_size(labels)

    points = []
    for i, deck_id in enumerate(features.index):
        meta = next(deck for deck in decks if deck["deck_id"] == deck_id)
        rank_info = parse_rank_info(meta.get("rank"))
        featured_finish = bool(meta.get("tournament_big_star")) and rank_is_featured_finish(meta.get("rank"))
        points.append(
            {
                **meta,
                "x": float(coords[i, 0]),
                "y": float(coords[i, 1]),
                "cluster": int(labels[i]),
                "total": float(deck_totals.get(deck_id, 0)),
                "colors": deck_colors.get(deck_id, []),
                "featured_finish": featured_finish,
                "finish_min": rank_info["finish_min"],
                "finish_max": rank_info["finish_max"],
                "event_players": rank_info["players"],
                "finish_percent": rank_info["finish_percent"],
            }
        )

    starred_decks = sum(1 for point in points if int(point.get("tournament_stars") or 0) > 0)
    big_star_decks = sum(1 for point in points if point.get("tournament_big_star"))
    featured_decks = sum(1 for point in points if point.get("featured_finish"))

    cluster_sizes = pd.Series(labels).value_counts().sort_index().to_dict()
    clustered_matrix = matrix.loc[features.index, features.columns]
    non_noise = labels >= 0
    means = clustered_matrix.loc[non_noise].groupby(labels[non_noise]).mean()
    card_differences = []
    for cluster in sorted(set(labels)):
        if int(cluster) < 0:
            continue
        comparison = means.drop(index=cluster)
        if comparison.empty:
            continue
        diff = (means.loc[cluster] - comparison.mean()).sort_values()
        card_differences.append(
            {
                "cluster": int(cluster),
                "higher": [{"card": card, "diff": float(value)} for card, value in diff.tail(12).iloc[::-1].items()],
                "lower": [{"card": card, "diff": float(value)} for card, value in diff.head(8).items()],
            }
        )

    cluster_archetypes = build_cluster_archetypes(records, features.index, labels, alias_map)
    decklists = build_decklists(records, features.index)

    return {
        "points": points,
        "diagnostics": {
            **input_meta,
            **projection_meta,
            **duplicate_meta,
            "deck_count": len(decks),
            "card_columns": int(matrix.shape[1]),
            "feature_columns": int(features.shape[1]),
            "explained_variance": explained_variance,
            "cluster_sizes": {str(key): int(value) for key, value in cluster_sizes.items()},
            "silhouette": silhouette,
            "scope": scope,
            "scale_clusters": scale_clusters,
            "cluster_method": cluster_method,
            "cluster_method_requested": cluster_method_requested,
            "cluster_space": cluster_space,
            "outlier_mode": outlier_mode,
            "outlier_mode_requested": outlier_mode_requested,
            **cluster_meta,
            "starred_decks": starred_decks,
            "big_star_decks": big_star_decks,
            "featured_decks": featured_decks,
            "cache": fetch_stats.as_dict(),
        },
        "card_differences": card_differences,
        "cluster_archetypes": cluster_archetypes,
        "decklists": decklists,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write(format % args + "\n")

    def send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/search-options":
            self.send_json(200, search_options_payload())
            return
        if path == "/":
            path = "/index.html"
        file_path = (STATIC / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC.resolve())) or not file_path.exists():
            self.send_error(404)
            return
        content_type = "text/html"
        if file_path.suffix == ".css":
            content_type = "text/css"
        elif file_path.suffix == ".js":
            content_type = "application/javascript"
        elif file_path.suffix == ".svg":
            content_type = "image/svg+xml"
        raw = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/analyze", "/api/analyze-stream"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/analyze-stream":
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                def send_event(body: dict) -> None:
                    self.wfile.write((json.dumps(body) + "\n").encode("utf-8"))
                    self.wfile.flush()

                try:
                    send_event({"status": "Starting analysis..."})
                    result = analyze(payload, progress=lambda message: send_event({"status": message}))
                    send_event({"result": result})
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:
                    if not isinstance(exc, ValueError):
                        traceback.print_exc()
                    try:
                        send_event({"error": str(exc)})
                    except (BrokenPipeError, ConnectionResetError):
                        return
                return

            self.send_json(200, analyze(payload))
        except Exception as exc:
            if not isinstance(exc, ValueError):
                traceback.print_exc()
            self.send_json(400, {"error": str(exc)})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving MTGTop8 PCA app on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
