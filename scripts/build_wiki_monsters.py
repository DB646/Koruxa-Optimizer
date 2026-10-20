"""Scrape Koruxa Wiki monster pages and their published drop tables."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://koruxa.com"
INDEX_URL = f"{BASE_URL}/wiki/monsters/"
OUTPUT_FILE = Path("data/wiki/monsters.json")
ERROR_FILE = Path("data/wiki/monster_errors.json")
REQUEST_DELAY = 0.15


def clean_text(value):
    """Repair common wiki UTF-8 decoding artifacts while preserving symbols."""
    if not isinstance(value, str):
        return value
    value = value.replace("\ufffd\u00ad\u0090", "⭐")
    value = value.replace("â\u00ad\u0090", "⭐")
    replacements = {
        "Ã—": "×", "Ã·": "÷", "â€”": "—", "â€“": "–",
        "â€˜": "‘", "â€™": "’", "â€œ": "“", "â€": "”",
        "â€¦": "…", "â˜…": "⭐", "â˜†": "☆", "âœ¨": "✨",
        "â˜€ï¸": "☀️", "â˜€": "☀️", "ðŸŒ™": "🌙", "ðŸŒŸ": "🌟",
    }
    for _ in range(3):
        original = value
        for bad, good in replacements.items():
            value = value.replace(bad, good)
        if any(marker in value for marker in ("Ã", "Â", "â", "ð")):
            try:
                value = value.encode("latin1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        if value == original:
            break
    return value


def number(text):
    if not text:
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", str(text))
    return float(match.group(0).replace(",", "")) if match else None


def parse_probability(text):
    """Return a decimal probability from Always, percent, or 1 in N text."""
    value = (text or "").strip().casefold().replace(",", "")
    if value in {"always", "guaranteed", "100%"}:
        return 1.0
    fraction = re.search(r"1\s+in\s+(\d+(?:\.\d+)?)", value)
    if fraction:
        denominator = float(fraction.group(1))
        return 1.0 / denominator if denominator > 0 else None
    percent = re.search(r"([\d.]+)\s*%", value)
    if percent:
        return float(percent.group(1)) / 100.0
    return None


def parse_quantity(text):
    values = [float(part.replace(",", "")) for part in re.findall(r"[\d,]+(?:\.\d+)?", text or "")]
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], values[0]
    return min(values[0], values[1]), max(values[0], values[1])


def rows_from_table(table):
    rows = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if cells:
            rows.append([cell.get_text(" ", strip=True) for cell in cells])
    return rows


def parse_monster_page(html, monster_key, url):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("h1")
    monster = {
        "monster_key": monster_key,
        "name": clean_text(title.get_text(" ", strip=True)) if title else monster_key,
        "url": url,
        "area": None,
        "combat_level": None,
        "hitpoints": None,
        "max_hit": None,
        "attack_style": None,
        "weak_to": None,
        "attack_speed_seconds": None,
        "drops": [],
    }

    labels = {
        "area": "Area",
        "combat_level": "Combat level",
        "hitpoints": "Hitpoints",
        "max_hit": "Max hit",
        "attack_style": "Attack style",
        "weak_to": "Weak to",
        "attack_speed_seconds": "Attack speed",
    }
    # The wiki renders these properties as adjacent label/value cells.
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        label = cells[0].get_text(" ", strip=True).rstrip(":").casefold()
        value = cells[1].get_text(" ", strip=True)
        for field, expected in labels.items():
            if label == expected.casefold():
                if field in {"area", "attack_style", "weak_to"}:
                    monster[field] = clean_text(value)
                elif field == "attack_speed_seconds":
                    speed = number(value)
                    monster[field] = speed
                else:
                    parsed = number(value)
                    monster[field] = int(parsed) if parsed is not None else None
                break

    page_text = soup.get_text(" ", strip=True)
    text_patterns = {
        "area": r"\bArea\s+([^|]+?)\s+(?=Combat level\b)",
        "combat_level": r"\bCombat level\s+([\d,]+)",
        "hitpoints": r"\bHitpoints\s+([\d,]+)",
        "max_hit": r"\bMax hit\s+([\d,]+)",
        "attack_style": r"\bAttack style\s+([^|]+?)\s+(?=Weak to\b)",
        "weak_to": r"\bWeak to\s+([^|]+?)\s+(?=Attack speed\b)",
        "attack_speed_seconds": r"\bAttack speed\s+([\d.]+)\s*s",
    }
    for field, pattern in text_patterns.items():
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()
            if field in {"area", "attack_style", "weak_to"}:
                monster[field] = clean_text(raw)
            elif field == "attack_speed_seconds":
                monster[field] = float(raw)
            else:
                monster[field] = int(raw.replace(",", ""))

    for heading in soup.find_all(["h2", "h3"]):
        if heading.get_text(" ", strip=True).casefold() != "drops":
            continue
        table = heading.find_next("table")
        if not table:
            break
        table_rows = table.find_all("tr")
        for item_row in table_rows[1:]:
            row = [cell.get_text(" ", strip=True)
                   for cell in item_row.find_all(["th", "td"], recursive=False)]
            if len(row) < 3:
                continue
            # Resolve item key from the item link on the matching table row.
            link = next((a for a in item_row.find_all("a", href=True)
                         if re.search(r"/wiki/items/([^/]+)\.html$", a["href"])), None)
            if not link:
                continue
            item_match = re.search(r"/wiki/items/([^/]+)\.html$", link["href"])
            # Rows include a leading icon cell, then item, quantity, and chance.
            quantity_min, quantity_max = parse_quantity(row[-2])
            monster["drops"].append({
                "item_key": item_match.group(1),
                "item_name": clean_text(link.get_text(" ", strip=True)),
                "quantity_min": quantity_min,
                "quantity_max": quantity_max,
                "drop_chance": parse_probability(row[-1]),
                "chance_text": clean_text(row[-1]),
            })
        break

    return monster


def discover_monsters(session):
    response = session.get(INDEX_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = {}
    for link in soup.find_all("a", href=True):
        match = re.search(r"/wiki/monsters/([^/]+)\.html$", link["href"])
        if match:
            key = match.group(1)
            found[key] = {"name": link.get_text(" ", strip=True),
                          "url": urljoin(BASE_URL, link["href"])}
    return found


def main():
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "KoruxaOptimizerWikiScraper/1.0"})
    monsters = discover_monsters(session)
    print(f"Found {len(monsters)} monster pages.")
    results, errors = {}, []
    for index, (key, info) in enumerate(monsters.items(), 1):
        try:
            response = session.get(info["url"], timeout=30)
            response.raise_for_status()
            results[key] = parse_monster_page(response.text, key, info["url"])
            print(f"[{index}/{len(monsters)}] {results[key]['name']}: {len(results[key]['drops'])} drops")
        except Exception as exc:
            errors.append({"monster_key": key, "url": info["url"], "error": str(exc)})
            print(f"[{index}/{len(monsters)}] {key}: ERROR {exc}")
        time.sleep(REQUEST_DELAY)

    OUTPUT_FILE.write_text(json.dumps({
        "source": INDEX_URL,
        "monster_count": len(results),
        "monsters": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    ERROR_FILE.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {len(results)} monsters to {OUTPUT_FILE}; {len(errors)} errors.")


if __name__ == "__main__":
    main()
