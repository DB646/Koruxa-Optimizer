import json
import getpass
import glob
import requests
import time
import os
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone


BASE_URL = "https://koruxa.com"
CHARACTER_ID = os.environ.get("KORUXA_CHARACTER_ID", "").strip()

session = requests.Session()


# ------------------------------------------------------------
# Authentication
# ------------------------------------------------------------

def login():
    global CHARACTER_ID
    username = input("Koruxa username: ")
    password = getpass.getpass("Koruxa password: ")
    CHARACTER_ID = CHARACTER_ID or input("Koruxa character ID: ").strip()
    if not CHARACTER_ID.isdigit():
        raise SystemExit("Character ID must be numeric.")

    session.get(f"{BASE_URL}/login")

    r = session.post(
        f"{BASE_URL}/login",
        data={
            "username": username,
            "password": password,
        },
        allow_redirects=False,
    )

    if r.status_code not in (200, 302, 303):
        raise RuntimeError(f"Login failed: {r.status_code}")

    session.get(f"{BASE_URL}/characters")

    r = session.post(
        f"{BASE_URL}/characters/select",
        data={"character_id": CHARACTER_ID},
        allow_redirects=False,
    )

    if r.status_code not in (200, 302, 303):
        raise RuntimeError(f"Character selection failed: {r.status_code}")

    print("Authenticated.")


# ------------------------------------------------------------
# Utility
# ------------------------------------------------------------

def fallback_item_name(item_key):
    """
    Convert an item key into a reasonable display-name fallback.

    Examples:
        dustite_bar -> Dustite Bar
        voidstone_rune -> Voidstone Rune
        log_basic -> Log Basic

    Known names from owned items or skill outputs will override this.
    """
    if not item_key:
        return None

    return item_key.replace("_", " ").title()


# ------------------------------------------------------------
# Owned item data
# ------------------------------------------------------------

def load_owned_items():
    items = {}

    for filename in ["inventory.json", "bank.json"]:
        path = Path(filename)

        if not path.exists():
            continue

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for item in data.get("items", []):
            key = item.get("item_key")

            if not key:
                continue

            if key not in items:
                items[key] = {
                    "item_key": key,
                    "name": item.get("name"),
                    "inventory_qty": 0,
                    "bank_qty": 0,
                    "npc_sell_price": item.get("sell_price"),
                }

            if filename == "inventory.json":
                items[key]["inventory_qty"] += item.get("quantity", 0)
            else:
                items[key]["bank_qty"] += item.get("quantity", 0)

            # Fill in metadata if the other file has better information.
            if not items[key].get("name") and item.get("name"):
                items[key]["name"] = item.get("name")

            if items[key].get("npc_sell_price") is None:
                items[key]["npc_sell_price"] = item.get("sell_price")

    return items


# ------------------------------------------------------------
# Skill/action item discovery
# ------------------------------------------------------------

def load_action_items(items):
    """
    Add every ingredient and output referenced by every skill action.

    Existing owned-item metadata is preserved.
    """

    skill_files = glob.glob("skill_*.json")

    print(f"Scanning {len(skill_files)} skill files for recipe items...")

    for filename in skill_files:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)

        for action in data.get("actions", []):
            # ------------------------------------------------
            # Output item
            # ------------------------------------------------
            output_key = action.get("reward_item_key")

            if output_key:
                if output_key not in items:
                    items[output_key] = {
                        "item_key": output_key,
                        "name": None,
                        "inventory_qty": 0,
                        "bank_qty": 0,
                        "npc_sell_price": None,
                    }

                reward_label = action.get("reward_label")

                if reward_label and not items[output_key].get("name"):
                    items[output_key]["name"] = reward_label

            # ------------------------------------------------
            # Ingredient items
            # ------------------------------------------------
            for ingredient in (action.get("ingredients") or []):
                ingredient_key = ingredient.get("item_key")

                if not ingredient_key:
                    continue

                if ingredient_key not in items:
                    items[ingredient_key] = {
                        "item_key": ingredient_key,
                        "name": None,
                        "inventory_qty": 0,
                        "bank_qty": 0,
                        "npc_sell_price": None,
                    }

    # Combat loot is sourced from the scraped monster drop tables rather
    # than from the skill action endpoints.
    monster_path = Path("data/wiki/monsters.json")
    wiki_item_path = Path("data/wiki/items.json")
    wiki_items = {}
    if wiki_item_path.exists():
        with wiki_item_path.open("r", encoding="utf-8") as stream:
            wiki_items = json.load(stream)
    if monster_path.exists():
        with monster_path.open("r", encoding="utf-8") as stream:
            monster_data = json.load(stream)
        for monster in (monster_data.get("monsters") or {}).values():
            for drop in monster.get("drops", []) or []:
                key = drop.get("item_key")
                if not key:
                    continue
                wiki_item = wiki_items.get(key) or {}
                item = items.setdefault(key, {
                    "item_key": key,
                    "name": drop.get("item_name") or wiki_item.get("name"),
                    "inventory_qty": 0,
                    "bank_qty": 0,
                    "npc_sell_price": wiki_item.get("npc_sell_price"),
                })
                if not item.get("name"):
                    item["name"] = drop.get("item_name") or wiki_item.get("name")
                if item.get("npc_sell_price") is None:
                    item["npc_sell_price"] = wiki_item.get("npc_sell_price")

    # Fill missing names with key-derived names.
    for key, item in items.items():
        if not item.get("name"):
            item["name"] = fallback_item_name(key)

    return items


# ------------------------------------------------------------
# Market API
# ------------------------------------------------------------

def get_market_data(item_key, item_name, active_session=None):
    request_session = active_session or session
    per_page = 100
    result = {
        "sell_listings": [],
        "buy_orders": [],
        "sell_listing_pages": {"fetched": 0, "total": None, "complete": False},
        "buy_order_pages": {"fetched": 0, "total": None, "complete": False},
    }

    def fetch_all_pages(url, collection_key, exact_item_key=None):
        collected = []
        page = 1
        total_pages = None
        complete = False
        seen_pages = set()
        while True:
            separator = "&" if "?" in url else "?"
            response = request_session.get(
                f"{url}{separator}page={page}",
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            page_rows = payload.get(collection_key, []) or []
            signature = tuple(
                row.get("id", json.dumps(row, sort_keys=True))
                for row in page_rows
            )
            if page > 1 and signature in seen_pages:
                break
            seen_pages.add(signature)
            if exact_item_key is None:
                collected.extend(page_rows)
            else:
                collected.extend(
                    row for row in page_rows
                    if row.get("item_key") == exact_item_key
                )

            try:
                raw_total_pages = payload.get("total_pages")
                total_pages = (
                    max(1, int(raw_total_pages))
                    if raw_total_pages is not None
                    else None
                )
            except (TypeError, ValueError):
                total_pages = None

            if total_pages is not None:
                if page >= total_pages:
                    complete = True
                    break
            elif len(page_rows) < per_page:
                complete = True
                break
            page += 1

        return collected, {
            "fetched": page,
            "total": total_pages if total_pages is not None else page,
            "complete": complete,
        }

    # --------------------------------------------------------
    # Sell listings
    # --------------------------------------------------------

    url = (
        f"{BASE_URL}/api/marketplace"
        f"?sort=price_asc"
        f"&base_item={quote(item_key)}"
        f"&per_page={per_page}"
    )
    result["sell_listings"], result["sell_listing_pages"] = fetch_all_pages(
        url,
        "listings",
    )

    # --------------------------------------------------------
    # Buy orders
    #
    # Search is fuzzy, so we MUST filter by exact item_key.
    # --------------------------------------------------------

    if item_name:
        url = (
            f"{BASE_URL}/api/marketplace/buy-orders"
            f"?sort=price_desc"
            f"&per_page={per_page}"
            f"&search={quote(item_name)}"
        )
        result["buy_orders"], result["buy_order_pages"] = fetch_all_pages(
            url,
            "buy_orders",
            exact_item_key=item_key,
        )

    return result


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    login()

    # Start with inventory/bank information.
    items = load_owned_items()

    print(f"Found {len(items)} unique owned items.")

    # Add every item referenced by every known skill action.
    items = load_action_items(items)

    print(f"Found {len(items)} total action-related items.")

    market = {}

    total = len(items)

    for i, (item_key, item) in enumerate(sorted(items.items()), 1):
        name = item.get("name") or fallback_item_name(item_key)

        print(f"[{i}/{total}] {name} ({item_key})")

        # Keep the display name in case the API/search requires it.
        item["name"] = name

        market[item_key] = {
            "item": item,
            "market": get_market_data(
                item_key,
                name,
            ),
        }
        time.sleep(0.05)

    output = {
        "character_id": CHARACTER_ID,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": market,
    }

    with open("market_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print()
    print(f"Saved market_data.json with {len(market)} items.")


if __name__ == "__main__":
    main()
