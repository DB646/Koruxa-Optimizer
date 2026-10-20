import json
import getpass
import argparse
import math
import re
import time
import sys
import os
from datetime import datetime, timezone
import requests
from pathlib import Path
from urllib.parse import quote
import collect_market_data as market_collector

BASE_URL = "https://koruxa.com"
CHARACTER_ID = os.environ.get("KORUXA_CHARACTER_ID", "").strip()
WIKI_PROFIT_FILE = Path("data/wiki/activity_profit.json")
WIKI_ITEMS_FILE = Path("data/wiki/items.json")
WIKI_MONSTERS_FILE = Path("data/wiki/monsters.json")

SECONDS_PER_HOUR = 3600
HOURS_TO_SIMULATE = 24

session = requests.Session()
MARKET_SNAPSHOT_AGE_HOURS = None


# --------------------------------------------------
# BASIC HELPERS
# --------------------------------------------------

def load_json(filename):
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def remaining_quantity(order):
    return max(
        0,
        order.get("quantity_wanted", 0)
        - order.get("quantity_fulfilled", 0),
    )


# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

def login(username=None, password=None, character_id=None):
    global CHARACTER_ID
    if username is None:
        username = input("Koruxa username: ")
    if password is None:
        password = getpass.getpass("Koruxa password: ")
    character_id = str(character_id or CHARACTER_ID or input("Koruxa character ID: ")).strip()
    if not character_id.isdigit():
        raise SystemExit("Character ID must be numeric.")
    CHARACTER_ID = character_id

    print("\nLogging in...")

    r = session.get(f"{BASE_URL}/login")
    print("GET /login:", r.status_code)

    r = session.post(
        f"{BASE_URL}/login",
        data={
            "username": username,
            "password": password,
        },
        allow_redirects=False,
    )

    print("POST /login:", r.status_code)

    if r.status_code not in (302, 303):
        print("Login failed.")
        print(r.text[:1000])
        raise SystemExit

    print("Login successful.")

    print("\nSelecting character...")

    r = session.get(f"{BASE_URL}/characters")
    print("GET /characters:", r.status_code)

    r = session.post(
        f"{BASE_URL}/characters/select",
        data={"character_id": character_id},
        allow_redirects=False,
    )

    print("POST /characters/select:", r.status_code)

    if r.status_code not in (302, 303):
        print("Character selection failed.")
        print(r.text[:1000])
        raise SystemExit

    print("Character selected.")


# --------------------------------------------------
# API
# --------------------------------------------------

def api_get(path):
    url = f"{BASE_URL}/api/{path}"

    r = session.get(url)

    if r.status_code != 200:
        print(f"API error: {path} -> HTTP {r.status_code}")
        print(r.text[:500])
        return None

    try:
        return r.json()
    except Exception as e:
        print(f"Could not parse JSON from {path}: {e}")
        return None


def api_post(path, payload):
    """POST JSON to a game API endpoint and return its JSON response."""
    url = f"{BASE_URL}/api/{path}"
    r = session.post(url, json=payload)
    if r.status_code != 200:
        print(f"API error: {path} -> HTTP {r.status_code}")
        print(r.text[:500])
        return None
    try:
        return r.json()
    except Exception as e:
        print(f"Could not parse JSON from {path}: {e}")
        return None


def load_farming_actions(player, skill_data=None):
    """Read current farming seed data and normalize harvests as skill actions.

    The endpoint is authoritative for grow time, yield, and level gates.
    Harvest item keys are taken from the endpoint when present; otherwise a
    conservative seed-name match is allowed only when it resolves to an
    ingredient used by the loaded production recipes.
    """
    skills = (player or {}).get("skills") or {}
    farming_level = float((skills.get("farming") or {}).get("level") or 1)
    ingredient_keys = {
        ingredient.get("item_key")
        for data in (skill_data or {}).values()
        for action in data.get("actions", []) or []
        for ingredient in action.get("ingredients", []) or []
        if ingredient.get("item_key")
    }
    actions = []
    for patch_type in ("herb", "crop", "tree", "flower"):
        data = api_post("farming/get-seeds", {"patch_type": patch_type}) or {}
        for seed in data.get("seeds", []) or []:
            output_key = next((seed.get(field) for field in (
                "harvest_item_key", "output_item_key", "reward_item_key", "crop_item_key"
            ) if seed.get(field)), None)
            seed_key = seed.get("seed_key")
            if not output_key and seed_key:
                stem = seed_key.removesuffix("_seed").removesuffix("_sapling")
                if stem in ingredient_keys:
                    output_key = stem
                else:
                    matching_outputs = sorted(key for key in ingredient_keys
                                              if key.startswith(stem + "_"))
                    if len(matching_outputs) == 1:
                        output_key = matching_outputs[0]
            grow_seconds = float(seed.get("effective_grow_time") or seed.get("eff_grow_time")
                                 or seed.get("grow_time") or 0)
            if not output_key or grow_seconds <= 0:
                continue
            min_yield = float(seed.get("effective_min_yield") or seed.get("min_yield") or 1)
            max_yield = float(seed.get("effective_max_yield") or seed.get("max_yield") or min_yield)
            actions.append({
                "key": f"farm:{patch_type}:{seed.get('seed_key', output_key)}",
                "label": seed.get("name") or output_key,
                "skill_key": "farming",
                "min_level": float(seed.get("level_required") or 1),
                "duration_ms": grow_seconds * 1000,
                "amount": (min_yield + max_yield) / 2,
                "reward_item_key": output_key,
                "reward_label": seed.get("harvest_name") or seed.get("name") or output_key,
                "ingredients": ([{"item_key": seed_key, "quantity": 1}]
                                if seed_key else []),
                "calc": {"duration_ms": grow_seconds * 1000,
                         "base_amount": (min_yield + max_yield) / 2,
                         "yield_chance": 0},
            })
    return {"skill_level": farming_level, "actions": actions}


# --------------------------------------------------
# FRESH GAME DATA
# --------------------------------------------------

def load_player():
    print("Fetching player data...")
    data = api_get("player")

    if data is None:
        raise SystemExit("Could not load player data.")

    save_json("player.json", data)
    return data


def load_skill_data(skill):
    print(f"Fetching {skill}...")
    data = api_get(f"skill?skill={skill}")

    if data is None:
        return None

    save_json(f"skill_{skill}.json", data)
    return data


def load_all_skills():
    # These are the skills currently represented by your skill JSON files.
    skills = [
        "alchemy",
        "arcana",
        "construction",
        "cooking",
        "crafting",
        "firemaking",
        "fishing",
        "fletching",
        "herblore",
        "jewelery",
        "mining",
        "smithing",
        "thieving",
        "tinkering",
        "woodcutting",
    ]

    result = {}

    for skill in skills:
        data = load_skill_data(skill)

        if data is not None:
            result[skill] = data

    return result


# --------------------------------------------------
# MARKET DATA
# --------------------------------------------------

def load_market():
    global MARKET_SNAPSHOT_AGE_HOURS
    try:
        data = load_json("market_data.json")
    except FileNotFoundError:
        print("Market snapshot not found; using Wiki estimates where available.")
        return {}

    fetched_at = data.get("fetched_at")
    try:
        snapshot_time = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        snapshot_time = datetime.fromtimestamp(
            Path("market_data.json").stat().st_mtime,
            tz=timezone.utc,
        )

    age_hours = max(
        0.0,
        (datetime.now(timezone.utc) - snapshot_time.astimezone(timezone.utc)).total_seconds() / 3600,
    )
    MARKET_SNAPSHOT_AGE_HOURS = age_hours
    print(
        f"Market listing snapshot: {snapshot_time.astimezone().strftime('%Y-%m-%d %H:%M %Z')} "
        f"({age_hours:.1f} hours old)."
    )
    if age_hours > 24:
        print(
            "WARNING: market data is over 24 hours old. Run "
            ".\\.venv\\Scripts\\python.exe optimizer.py --refresh-market "
            "before relying on gold estimates."
        )
    fetch_errors = data.get("fetch_errors", [])
    if fetch_errors:
        print(
            f"WARNING: the last market refresh had errors for "
            f"{len(fetch_errors)} item(s)."
        )
    return data.get("items", {})


def refresh_market_snapshot():
    """Refresh inventory, bank, and action-item listings using this login session."""
    global MARKET_SNAPSHOT_AGE_HOURS
    inventory = api_get("inventory")
    bank = api_get("bank")
    if inventory is None or bank is None:
        print("Could not refresh inventory and bank; keeping the saved market snapshot.")
        return load_market()

    save_json("inventory.json", inventory)
    save_json("bank.json", bank)

    items = market_collector.load_action_items(
        market_collector.load_owned_items()
    )
    market = {}
    fetch_errors = []
    total = len(items)
    print(f"Refreshing listings for {total} items...")

    for number, (item_key, item) in enumerate(sorted(items.items()), start=1):
        name = item.get("name") or market_collector.fallback_item_name(item_key)
        item["name"] = name
        try:
            item_market = market_collector.get_market_data(
                item_key,
                name,
                active_session=session,
            )
        except (requests.RequestException, ValueError, RuntimeError) as error:
            fetch_errors.append({"item_key": item_key, "error": str(error)})
            item_market = {
                "sell_listings": [],
                "buy_orders": [],
                "sell_listing_pages": {"complete": False},
                "buy_order_pages": {"complete": False},
            }

        market[item_key] = {"item": item, "market": item_market}
        if number == 1 or number % 50 == 0 or number == total:
            print(f"  {number}/{total}")
        time.sleep(0.05)

    if len(fetch_errors) > max(10, int(total * 0.05)):
        print(
            "Market refresh was too incomplete to replace the saved snapshot; "
            "continuing with the previous data."
        )
        return merge_fresh_owned_quantities(load_market())

    if fetch_errors:
        print(f"Market refresh completed with {len(fetch_errors)} item errors.")

    snapshot = {
        "character_id": CHARACTER_ID,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetch_errors": fetch_errors,
        "items": market,
    }
    save_json("market_data.json", snapshot)
    MARKET_SNAPSHOT_AGE_HOURS = 0.0
    print(
        f"Saved fresh market data for {len(market)} items; "
        f"{len(fetch_errors)} item refresh errors."
    )
    return market


def merge_fresh_owned_quantities(market):
    """Keep feasibility inventory current when prices fall back to an older snapshot."""
    owned = market_collector.load_owned_items()
    for item_key, market_entry in market.items():
        owned_entry = owned.get(item_key, {})
        item = market_entry.setdefault("item", {})
        item["inventory_qty"] = owned_entry.get("inventory_qty", 0)
        item["bank_qty"] = owned_entry.get("bank_qty", 0)
    return market


def load_wiki_profit_index():
    """Load precomputed Wiki estimates, indexed by skill and action label."""
    if not WIKI_PROFIT_FILE.exists():
        return {}

    rows = load_json(WIKI_PROFIT_FILE)
    return {
        (str(row.get("skill", "")).casefold(), str(row.get("action", "")).casefold()): row
        for row in rows
        if row.get("skill") and row.get("action")
    }


def load_wiki_items():
    if not WIKI_ITEMS_FILE.exists():
        return {}
    return load_json(WIKI_ITEMS_FILE)


def load_wiki_monsters():
    if not WIKI_MONSTERS_FILE.exists():
        return {}
    data = load_json(WIKI_MONSTERS_FILE)
    return data.get("monsters", {})


def estimate_item_sell_price(item_key, market, wiki_items):
    market_item = market.get(item_key) or {}
    bid = best_bid(market_item) if market_item else None
    if bid is not None:
        return float(bid)

    wiki_item = wiki_items.get(item_key) or {}
    for price in (
        wiki_item.get("market_price"),
        (market_item.get("item") or {}).get("npc_sell_price"),
        wiki_item.get("npc_sell_price"),
    ):
        try:
            if price is not None:
                return float(price)
        except (TypeError, ValueError):
            continue
    return None


def estimate_item_buy_price(item_key, market, wiki_items):
    market_item = market.get(item_key) or {}
    ask = lowest_ask(market_item) if market_item else None
    if ask is not None:
        return float(ask)

    wiki_item = wiki_items.get(item_key) or {}
    for price in (
        wiki_item.get("market_price"),
        (market_item.get("item") or {}).get("npc_sell_price"),
        wiki_item.get("npc_sell_price"),
    ):
        try:
            if price is not None:
                return float(price)
        except (TypeError, ValueError):
            continue
    return None


def expected_thieving_loot_value(action, market, wiki_items):
    """Expected item value per attempt, using listed marginal drop chances."""
    calc = action.get("calc", {})
    rare_mult = (calc.get("bonuses", {}) or {}).get("mastery_rare_drop_mult", 1) or 1
    per_attempt = 0.0
    unpriced = 0

    for drop in action.get("loot_drops", []) or []:
        chance = drop.get("drop_chance")
        quantity_min = drop.get("quantity_min", 1)
        quantity_max = drop.get("quantity_max", quantity_min)
        item_key = drop.get("item_key")
        try:
            chance = max(0.0, min(1.0, float(chance)))
            quantity = (float(quantity_min) + float(quantity_max)) / 2.0
        except (TypeError, ValueError):
            continue
        price = estimate_item_sell_price(item_key, market, wiki_items)
        if price is None:
            unpriced += 1
            continue
        per_attempt += chance * quantity * price

    for drop in action.get("rare_drops", action.get("seed_drops", [])) or []:
        item_key = drop.get("item_key")
        try:
            chance = max(0.0, min(1.0, float(drop.get("chance")) * float(rare_mult)))
        except (TypeError, ValueError):
            continue
        price = estimate_item_sell_price(item_key, market, wiki_items)
        if price is None:
            unpriced += 1
            continue
        per_attempt += chance * price

    return per_attempt, unpriced


def estimate_food_cost_per_hp(skill_data, market, wiki_items):
    """Find a priced cooking output with the lowest estimated replacement cost per HP."""
    cooking = skill_data.get("cooking", {})
    candidates = []
    for action in cooking.get("actions", []) or []:
        item_key = action.get("reward_item_key")
        heal = (action.get("reward_stats") or {}).get("stat_hp")
        try:
            heal = float(heal)
        except (TypeError, ValueError):
            continue
        if not item_key or heal <= 0:
            continue

        market_item = market.get(item_key) or {}
        price = lowest_ask(market_item)
        price_source = "live ask"
        if price is None:
            wiki_item = wiki_items.get(item_key) or {}
            price = wiki_item.get("market_price")
            price_source = "Wiki price"
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        candidates.append({
            "item_key": item_key,
            "name": action.get("reward_label") or action.get("label") or item_key,
            "heal": heal,
            "price": price,
            "cost_per_hp": price / heal,
            "price_source": price_source,
        })

    if not candidates:
        return None
    return min(candidates, key=lambda food: food["cost_per_hp"])


def load_live_monster_drops(wiki_monsters, live_monster_data, player):
    """Fetch authenticated, character-specific drop chances for eligible monsters."""
    rows = live_monster_data.get("monsters", []) if isinstance(live_monster_data, dict) else live_monster_data
    if isinstance(rows, dict):
        rows = list(rows.values())
    by_key = {str(row.get("key") or row.get("monster_key")): row
              for row in rows or [] if isinstance(row, dict)}
    skills = player.get("skills", {}) or {}
    level = lambda skill: float((skills.get(skill) or {}).get("level", 1) or 1)
    combat_level = math.floor(
        (level("defence") + max(level("hitpoints"), 10)) / 4
        + max((level("attack") + level("strength")) * 0.325,
              level("magic") * 0.5, level("ranged") * 0.5)
    )
    area_rows = (live_monster_data.get("combat_areas", [])
                 if isinstance(live_monster_data, dict) else []) or []
    area_levels = {str(area.get("area_key")): float(area.get("min_combat_level", 0) or 0)
                   for area in area_rows if isinstance(area, dict)}
    dungeons = {str(area.get("area_key")) for area in area_rows
                if isinstance(area, dict) and area.get("is_dungeon")}
    task = live_monster_data.get("slayer_task") if isinstance(live_monster_data, dict) else None
    task_key = str((task or {}).get("monster_key", ""))
    fetched = {}
    eligible = [key for key in wiki_monsters if key in by_key]
    print(f"Fetching live personal drop data for {len(eligible)} eligible monsters...")
    for key in eligible:
        live = by_key[key]
        if live.get("is_boss") or live.get("is_superior"):
            continue
        if live.get("slayer_only") and key != task_key:
            continue
        area_key = str(live.get("area_key") or "")
        if area_key in dungeons or (area_levels.get(area_key, 0) and combat_level < area_levels[area_key]):
            continue
        data = api_get(f"monster-drops?monster={quote(str(key))}")
        if isinstance(data, dict) and isinstance(data.get("drops"), list):
            fetched[key] = data
    return fetched


def research_bonus_totals(research_data, clan_research_data=None):
    """Return owned Institute bonuses using the live research-list payloads."""
    totals = {}
    names = {}
    for payload in (research_data, clan_research_data):
        rows = (payload or {}).get("nodes", []) if isinstance(payload, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bonus_type = str(row.get("bonus_type") or "").casefold()
            node_key = str(row.get("node_key") or "").casefold()
            try:
                levels = float(row.get("current_level", 0) or 0)
                per_level = float(row.get("bonus_per_level", 0) or 0)
                prestige = float(row.get("prestige_level", 0) or 0)
            except (TypeError, ValueError):
                continue
            effective = levels * per_level * (1.0 + 0.15 * prestige)
            key = bonus_type or node_key
            if effective:
                totals[key] = totals.get(key, 0.0) + effective
                names[key] = str(row.get("display_name") or node_key.replace("_", " ").title())
            # Preserve a key alias as well: API bonus_type names are stable,
            # while node keys are useful when decoding older API responses.
            if node_key and effective:
                totals[f"node:{node_key}"] = effective
                names[f"node:{node_key}"] = str(row.get("display_name") or node_key.replace("_", " ").title())
    return totals, names


def combat_xp_bonus_info(player, equipment_data, combat_status, active_buffs,
                         research_data=None, clan_research_data=None):
    """Resolve the live combat-wide XP bonus when available, with buff fallback."""
    bonus_tree = (combat_status or {}).get("bonuses", {}) or {}
    total = bonus_tree.get("total", {}) or {}
    try:
        total_xp = total.get("xp")
        if total_xp is not None:
            # The game's displayed total can include the separate mastery
            # XP contribution. Combat mastery is applied per monster below,
            # so remove that component here to avoid multiplying it twice.
            mastery_xp = float((bonus_tree.get("mastery") or {}).get("xp", 0) or 0)
            pct = max(0.0, float(total_xp) - mastery_xp)
            parts = []
            for source in ("gear", "jewelery", "research", "clan", "patron", "buff", "global", "premium"):
                source_row = bonus_tree.get(source) or {}
                value = source_row.get("xp")
                if value is not None and float(value) > 0:
                    parts.append(f"{source.title()} +{float(value):g}%")
            return pct, parts or ["Live combat XP bonuses"]
    except (AttributeError, TypeError, ValueError):
        pass

    # When no combat session is active, reconstruct permanent XP sources from
    # the same live Institute node payloads used by the game UI.
    sources = []
    seen = set()
    rows = []
    for field in ("global_buffs", "clan_buffs"):
        rows.extend((player or {}).get(field, []) or [])
    if isinstance(active_buffs, dict):
        rows.extend(active_buffs.get("buffs", []) or [])
    elif isinstance(active_buffs, list):
        rows.extend(active_buffs)
    pct = 0.0
    for row in rows:
        if not isinstance(row, dict) or row.get("buff_type") not in {"xp_boost", "shrine_xp_boost", "clan_xp_boost"}:
            continue
        try:
            value = float(row.get("buff_value", row.get("value", 0)) or 0)
        except (TypeError, ValueError):
            continue
        identity = (row.get("buff_type"), value, row.get("expires_at"))
        if value > 0 and identity not in seen:
            seen.add(identity)
            pct += value
            sources.append(f"{row.get('buff_type', 'XP buff').replace('_', ' ').title()} +{value:g}%")
    research_totals, research_names = research_bonus_totals(research_data, clan_research_data)
    # Node bonus types are supplied by the game. Include global XP and combat
    # XP nodes, plus older node-key aliases. Do not count skill-specific XP
    # research for a different combat style.
    xp_keys = {
        "xp_all", "all_xp", "xp_combat", "combat_xp", "xp",
        "node:shared_knowledge", "node:clan_shared_knowledge",
        "node:scholar", "node:sage", "node:enlightened", "node:polymath",
        "node:legend_of_koruxa",
    }
    for key, value in research_totals.items():
        if (key in xp_keys or key.endswith("_combat_xp") or
                ("xp" in key and any(scope in key for scope in ("all", "global", "combat")))):
            pct += value * 100.0
            sources.append(f"{research_names.get(key, key)} +{value * 100.0:g}%")
    gear_stats = (equipment_data or {}).get("stats", {}) or {}
    for field in ("xp", "xp_bonus", "stat_xp_bonus"):
        try:
            value = float(gear_stats.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            pct += value
            sources.append(f"Equipped gear +{value:g}%")
            break
    return pct, sources or ["No aggregate combat XP bonus data returned; estimate may be low"]


def has_live_listing_coverage(action, market):
    """Return true only when live listings price every input and output."""
    for ingredient in action.get("ingredients", []) or []:
        item = market.get(ingredient.get("item_key"))
        if not item or (lowest_ask(item) is None and best_bid(item) is None):
            return False

    output = market.get(action.get("output_key"))
    if not output:
        return False

    return best_bid(output) is not None


def wiki_fallback_result(action, wiki_index, market):
    """Adapt one cached Wiki profitability row to the live result display."""
    key = (str(action.get("skill", "")).casefold(), str(action.get("label", "")).casefold())
    estimate = wiki_index.get(key)
    if not estimate:
        return None

    executable = estimate.get("executable_profit_24h")
    theoretical = estimate.get("profit_per_hour")
    if executable is not None:
        profit_hour = float(executable) / 24.0
        source = "Wiki + volume"
    elif theoretical is not None:
        profit_hour = float(theoretical)
        source = "Wiki estimate"
    else:
        profit_hour = None
        source = "Wiki XP only"

    xp_hour = estimate.get("xp_per_hour") or (
        float(estimate.get("xp") or 0)
        * float(estimate.get("cycles_per_hour") or 0)
    )
    if profit_hour is None and not xp_hour:
        return None

    outputs = estimate.get("outputs") or []
    output_per_hour = sum(
        float(output.get("production_per_hour") or 0)
        for output in outputs
    )
    price_gap_ratio = None
    for output in outputs:
        wiki_price = output.get("price")
        item_key = output.get("item_key")
        current_ask = lowest_ask(market.get(item_key) or {})
        try:
            wiki_price = float(wiki_price)
            current_ask = float(current_ask)
        except (TypeError, ValueError):
            continue
        if wiki_price > 0 and current_ask > 0:
            ratio = max(wiki_price, current_ask) / min(wiki_price, current_ask)
            price_gap_ratio = ratio if price_gap_ratio is None else max(price_gap_ratio, ratio)

    market_share_required_pct = estimate.get("market_share_required_pct")
    price_conflict_without_bids = False
    if price_gap_ratio is not None and price_gap_ratio >= 5:
        source = "Wiki price gap"
        price_conflict_without_bids = bool(outputs) and all(
            best_bid(market.get(output.get("item_key")) or {}) is None
            for output in outputs
        )

    unverified_wiki_profit_hour = profit_hour
    unverified_wiki_profit_hour_high = profit_hour
    if price_conflict_without_bids:
        profit_hour = None
        source = "Needs price check"

    output_market_stats = [
        market_activity_stats(market.get(item_key) or {})
        for item_key in {
            output.get("item_key")
            for output in outputs
            if output.get("item_key")
        }
    ]
    active_sell_listing_count = sum(
        stats["active_sell_listing_count"] for stats in output_market_stats
    )
    active_buy_order_count = sum(
        stats["active_buy_order_count"] for stats in output_market_stats
    )
    visible_listing_count = active_sell_listing_count + active_buy_order_count
    market_data_complete = (
        None
        if not output_market_stats
        or any(stats["market_data_complete"] is None for stats in output_market_stats)
        else all(stats["market_data_complete"] for stats in output_market_stats)
    )

    try:
        input_cost = float(estimate.get("input_cost") or 0)
        if executable is not None:
            executable_cycles = estimate.get("executable_cycles_24h")
            input_cost_hour = (
                input_cost * float(executable_cycles) / 24.0
                if executable_cycles is not None
                else None
            )
        else:
            input_cost_hour = input_cost * float(estimate.get("cycles_per_hour") or 0)
    except (TypeError, ValueError):
        input_cost_hour = None

    return {
        **action,
        "production_per_hour": float(estimate.get("cycles_per_hour") or 0),
        "profit_hour": profit_hour,
        "profit_hour_high": (
            None if price_conflict_without_bids else profit_hour
        ),
        "xp_hour": xp_hour,
        "output_per_hour": output_per_hour or None,
        "bid_depth": sum(stats["bid_depth"] for stats in output_market_stats),
        "active_buy_order_count": active_buy_order_count,
        "active_sell_listing_count": active_sell_listing_count,
        "listed_sell_volume": sum(stats["listed_sell_volume"] for stats in output_market_stats),
        "listing_saturation_pct": (
            active_sell_listing_count / visible_listing_count * 100
            if visible_listing_count
            else None
        ),
        "market_data_complete": market_data_complete,
        "market_coverage_hours": estimate.get("output_coverage_hours"),
        "valuation_source": source,
        "wiki_theoretical_profit_hour": theoretical,
        "wiki_executable_profit_24h": executable,
        "input_cost_hour": input_cost_hour,
        "market_share_required_pct": market_share_required_pct,
        "price_gap_ratio": price_gap_ratio,
        "unverified_wiki_profit_hour": unverified_wiki_profit_hour,
        "unverified_wiki_profit_hour_high": unverified_wiki_profit_hour_high,
        "gold_estimate_suppressed": price_conflict_without_bids,
    }


def npc_price(market_item):
    return market_item.get("item", {}).get("npc_sell_price")


def lowest_ask(market_item):
    listings = market_item.get("market", {}).get("sell_listings", [])

    valid = [
        x
        for x in listings
        if x.get("price_per_item") is not None
        and x.get("quantity", 0) > 0
    ]

    if not valid:
        return None

    return min(x["price_per_item"] for x in valid)


def best_bid(market_item):
    orders = market_item.get("market", {}).get("buy_orders", [])

    valid = [
        o
        for o in orders
        if remaining_quantity(o) > 0
        and o.get("price_per_item") is not None
        and o.get("price_per_item") > 0
    ]

    if not valid:
        return None

    return max(o["price_per_item"] for o in valid)


def total_bid_depth(market_item):
    orders = market_item.get("market", {}).get("buy_orders", [])

    return sum(
        remaining_quantity(o)
        for o in orders
        if o.get("price_per_item") is not None
        and o.get("price_per_item") > 0
    )


# --------------------------------------------------
# MARKET ACTIVITY
# --------------------------------------------------

def market_activity_stats(market_item):
    """Summarize the active item book; saturation is based on order counts."""
    book = (market_item or {}).get("market", {}) or {}
    sell_listings = [
        listing for listing in book.get("sell_listings", []) or []
        if (listing.get("quantity") or 0) > 0
        and (listing.get("price_per_item") or 0) > 0
    ]
    buy_orders = [
        order for order in book.get("buy_orders", []) or []
        if remaining_quantity(order) > 0
        and (order.get("price_per_item") or 0) > 0
    ]
    active_sell_count = len(sell_listings)
    active_buy_count = len(buy_orders)
    visible_order_count = active_sell_count + active_buy_count
    sell_pages = book.get("sell_listing_pages") or {}
    buy_pages = book.get("buy_order_pages") or {}
    sell_complete = sell_pages.get("complete")
    buy_complete = buy_pages.get("complete")
    data_complete = (
        None
        if sell_complete is None or buy_complete is None
        else bool(sell_complete and buy_complete)
    )
    return {
        "active_sell_listing_count": active_sell_count,
        "active_buy_order_count": active_buy_count,
        "listed_sell_volume": sum(listing.get("quantity", 0) for listing in sell_listings),
        "bid_depth": sum(remaining_quantity(order) for order in buy_orders),
        "listing_saturation_pct": (
            active_sell_count / visible_order_count * 100
            if visible_order_count
            else None
        ),
        "market_data_complete": data_complete,
    }


# --------------------------------------------------
# PRODUCTION PLAN
# --------------------------------------------------

def age_plan_segments(calc):
    """
    plan_segments returned by the API are relative to the moment
    the API response was generated.

    Because we fetch the skill data immediately before using it,
    we don't need the browser's _fetchedAt correction here.

    The returned end_in_ms values are therefore treated as the
    remaining time from now.
    """

    segments = calc.get("plan_segments", [])

    result = []

    for segment in segments:
        end_ms = segment.get("end_in_ms")
        cycle_ms = segment.get("cycle_ms")

        if cycle_ms is None or cycle_ms <= 0:
            continue

        result.append({
            "end_in_ms": end_ms,
            "cycle_ms": cycle_ms,
        })

    return result


def simulate_cycles(calc, budget_ms):
    """
    Mirrors the game's walkFill() behavior closely.

    Returns the number of complete cycles that fit into budget_ms.
    """

    if budget_ms <= 0:
        return 0

    segments = age_plan_segments(calc)

    if not segments:
        duration = calc.get("duration_ms")

        if not duration or duration <= 0:
            return 0

        return int(budget_ms // duration)

    def segment_at(time_ms):
        for segment in segments:
            end_ms = segment["end_in_ms"]

            if end_ms < 0 or end_ms > time_ms:
                return segment

        return segments[-1]

    time_ms = 0
    cycles = 0
    guard = 0

    while guard < 200000:
        guard += 1

        segment = segment_at(time_ms)
        cycle_ms = segment["cycle_ms"]

        if time_ms + cycle_ms > budget_ms:
            break

        end_ms = segment["end_in_ms"]

        if end_ms >= 0 and time_ms + cycle_ms > end_ms:
            next_segment = segment_at(end_ms + 0.5)

            step_end = max(
                end_ms,
                time_ms + next_segment["cycle_ms"],
            )

            if step_end > budget_ms:
                break

            time_ms = step_end
            cycles += 1
            continue

        time_ms += cycle_ms
        cycles += 1

    return cycles


def calculate_production_rate(calc):
    """
    Calculate production over a 24-hour horizon using the actual
    plan_segments supplied by the game.
    """

    budget_ms = HOURS_TO_SIMULATE * 60 * 60 * 1000

    cycles = simulate_cycles(calc, budget_ms)

    return cycles / HOURS_TO_SIMULATE


# --------------------------------------------------
# OUTPUT / YIELD
# --------------------------------------------------

def expected_output(action):
    calc = action.get("calc", {})

    base_amount = calc.get(
        "base_amount",
        action.get("amount", 1),
    )

    yield_pct = calc.get("yield_chance", 0)

    return base_amount * (1 + yield_pct / 100)


def expected_experience_per_hour(action, cycles_per_hour):
    try:
        xp_per_cycle = float(action.get("xp"))
    except (TypeError, ValueError):
        return None

    if action.get("skill") == "thieving":
        try:
            success_rate = float(action.get("success_rate")) / 100.0
        except (TypeError, ValueError):
            success_rate = 1.0
        xp_per_cycle *= max(0.0, min(1.0, success_rate))

    return xp_per_cycle * cycles_per_hour


# --------------------------------------------------
# INPUT COST
# --------------------------------------------------

def free_craft_probability(calc):
    """
    Probability that a production cycle consumes no ingredients.

    The game stores these values inside calc["bonuses"].

    mastery_material_save and free_craft_pct are independent
    chances, so the combined probability is:

        1 - (1 - mastery_save) * (1 - free_craft)
    """

    bonuses = calc.get("bonuses", {})

    mastery_save = bonuses.get(
        "mastery_material_save",
        0,
    ) / 100.0

    other_free = bonuses.get(
        "free_craft_pct",
        0,
    ) / 100.0

    return 1.0 - (
        1.0 - mastery_save
    ) * (
        1.0 - other_free
    )

def input_opportunity_cost(ingredients, market, calc):
    total = 0

    free_craft = free_craft_probability(calc)

    for ingredient in ingredients or []:
        key = ingredient.get("item_key")
        quantity = ingredient.get("quantity", 0)

        item = market.get(key)

        if not item:
            return None

        price = lowest_ask(item)

        if price is None:
            price = best_bid(item)

        if price is None:
            price = npc_price(item)

        if price is None:
            return None

        total += quantity * (1.0 - free_craft) * price

    return total


# --------------------------------------------------
# OUTPUT MARKET ANALYSIS
# --------------------------------------------------

def sell_output(output_key, amount, market):
    """
    Sell 'amount' of an item against the currently available
    buy orders.

    Returns:

        revenue
        effective_price
        remaining_unsold
        bid_depth
    """

    if not output_key or amount <= 0:
        return 0, None, amount, 0

    item = market.get(output_key)

    if not item:
        return 0, None, amount, 0

    orders = item.get("market", {}).get("buy_orders", [])

    valid_orders = []

    for order in orders:
        quantity = remaining_quantity(order)
        price = order.get("price_per_item")

        if quantity > 0 and price is not None and price > 0:
            valid_orders.append({
                "quantity": quantity,
                "price": price,
            })

    valid_orders.sort(
        key=lambda x: x["price"],
        reverse=True,
    )

    bid_depth = sum(
        order["quantity"]
        for order in valid_orders
    )

    remaining = amount
    revenue = 0

    for order in valid_orders:
        if remaining <= 0:
            break

        sold = min(
            remaining,
            order["quantity"],
        )

        revenue += sold * order["price"]
        remaining -= sold

    # Anything not absorbed by buy orders is valued at NPC price.
    npc = npc_price(item)

    if remaining > 0 and npc is not None:
        revenue += remaining * npc
        remaining = 0

    if amount > 0:
        effective_price = revenue / amount
    else:
        effective_price = None

    return revenue, effective_price, remaining, bid_depth


MAGIC_SPELLS = [
    (1, 2, {"aetherite_rune": 1}), (5, 3, {"aetherite_rune": 2}),
    (10, 4, {"aetherite_rune": 1, "voidstone_rune": 1}),
    (15, 5, {"voidstone_rune": 2}), (20, 7, {"voidstone_rune": 2, "fluxite_rune": 1}),
    (25, 8, {"fluxite_rune": 2}), (30, 9, {"fluxite_rune": 2, "prismite_rune": 1}),
    (35, 10, {"prismite_rune": 2}), (40, 13, {"prismite_rune": 2, "celestite_rune": 1}),
    (45, 14, {"celestite_rune": 2}), (50, 15, {"celestite_rune": 2, "shadowrite_rune": 1}),
    (55, 16, {"shadowrite_rune": 2}), (60, 19, {"shadowrite_rune": 2, "nexus_rune": 1}),
    (65, 20, {"shadowrite_rune": 3, "nexus_rune": 1}), (70, 21, {"nexus_rune": 2}),
    (70, 22, {"nexus_rune": 1, "voidstone_rune": 2}), (75, 22, {"nexus_rune": 3}),
    (80, 25, {"nexus_rune": 2, "celestite_rune": 1}),
    (85, 27, {"nexus_rune": 2, "aetherite_rune": 3}),
    (85, 26, {"nexus_rune": 2, "shadowrite_rune": 1}),
    (90, 27, {"nexus_rune": 2, "prismite_rune": 2}),
    (95, 28, {"nexus_rune": 3}), (99, 32, {"nexus_rune": 3, "shadowrite_rune": 1, "celestite_rune": 1}),
    (105, 34, {"nether_rune": 2, "nexus_rune": 1}),
    (120, 37, {"drakonite_rune": 2, "nexus_rune": 1}),
    (135, 46, {"pyrethium_rune": 2, "nexus_rune": 1}),
    (145, 52, {"infernite_rune": 2}),
    (150, 60, {"infernite_rune": 3, "nexus_rune": 2}),
]


def combat_sell_value(item_key, amount, market, wiki_items):
    """Value drops by consuming live bid depth, then NPC or Wiki fallback."""
    if amount <= 0:
        return 0.0, 0.0, 0, set()
    entry = market.get(item_key) or {}
    orders = (entry.get("market") or {}).get("buy_orders", [])
    valid = sorted(
        ({"quantity": remaining_quantity(order), "price": order.get("price_per_item")}
         for order in orders
         if remaining_quantity(order) > 0 and (order.get("price_per_item") or 0) > 0),
        key=lambda order: order["price"], reverse=True,
    )
    revenue, remaining = 0.0, float(amount)
    sources = set()
    for order in valid:
        sold = min(remaining, order["quantity"])
        revenue += sold * float(order["price"])
        remaining -= sold
        if sold:
            sources.add("Active bids")
        if remaining <= 0:
            break
    item_meta = entry.get("item") or {}
    wiki_item = wiki_items.get(item_key) or {}
    fallback = item_meta.get("npc_sell_price")
    fallback_source = "NPC value"
    if fallback is None:
        fallback = wiki_item.get("npc_sell_price")
    if fallback is None:
        fallback = wiki_item.get("market_price")
        fallback_source = "Wiki price"
    unpriced = 0
    if remaining > 0:
        try:
            fallback = float(fallback)
        except (TypeError, ValueError):
            fallback = None
        if fallback is None or fallback <= 0:
            unpriced = 1
        else:
            revenue += remaining * fallback
            sources.add(fallback_source)
    return revenue, remaining, unpriced, sources


def combat_style_multiplier(player_style, monster_style, weak_to):
    triangle = {"melee": "ranged", "ranged": "magic", "magic": "melee"}
    multiplier = 1.0
    def normalize_style(value):
        value = str(value or "").casefold()
        if any(token in value for token in ("melee", "attack", "strength", "defence", "defense")):
            return "melee"
        if any(token in value for token in ("ranged", "range", "bow", "arrow")):
            return "ranged"
        if any(token in value for token in ("magic", "spell", "staff")):
            return "magic"
        return value
    monster_style = normalize_style(monster_style)
    player_style = normalize_style(player_style)
    if triangle.get(player_style) == monster_style:
        multiplier += 0.25
    elif triangle.get(monster_style) == player_style:
        multiplier -= 0.25
    triangle_multiplier = max(0.25, multiplier)
    weakness_text = str(weak_to or "").casefold()
    weakness_match = any(token in weakness_text for token in {
        "melee": ("melee", "attack", "strength"),
        "ranged": ("ranged", "arrow", "bow"),
        "magic": ("magic", "fire", "water", "earth", "air", "spell"),
    }[player_style]) if player_style in {"melee", "ranged", "magic"} else False
    return triangle_multiplier * (1.25 if weakness_match else 1.0)


def combat_equipment_bonus(equipment_data, *keys):
    """Read an aggregate combat bonus from either equipment stats or payload."""
    equipment_data = equipment_data or {}
    stats = equipment_data.get("stats", {}) or {}
    for source in (equipment_data, stats):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    return 0.0


def equipped_augment_totals(equipment_data, combat_status):
    """Read already-aggregated equipped augment stats, with capped node fallback.

    The equipment endpoint is authoritative when it provides ward/defence
    totals. The combat status augment list is used for node stacks only when
    those aggregate fields are absent.
    """
    totals = {}
    for stat, aliases in {
        "bulwark": ("bulwark_def", "augment_bulwark_def"),
        "melee_ward": ("melee_ward", "augment_melee_ward"),
        "ranged_ward": ("ranged_ward", "augment_ranged_ward"),
        "magic_ward": ("magic_ward", "augment_magic_ward"),
        "antivenom": ("antivenom", "augment_antivenom"),
        "staunch": ("staunch", "augment_staunch"),
        "frost_ward": ("frost_ward", "augment_frost_ward"),
    }.items():
        value = combat_equipment_bonus(equipment_data, *aliases)
        if value:
            totals[stat] = value

    node_values = {
        "bulwark": [10, 20, 35, 55, 80],
        "melee_ward": [5, 9, 13, 17, 20],
        "ranged_ward": [5, 9, 13, 17, 20],
        "magic_ward": [5, 9, 13, 17, 20],
        "antivenom": [15, 27, 39, 50, 60],
        "staunch": [15, 27, 39, 50, 60],
        "frost_ward": [10, 20, 30, 40, 50],
        "vitality": [20, 40, 70, 110, 160],
    }
    aliases = {
        "bulwark": "bulwark", "melee ward": "melee_ward",
        "ranged ward": "ranged_ward", "magic ward": "magic_ward",
        "antivenom": "antivenom", "staunch": "staunch",
        "frost ward": "frost_ward", "vitality": "vitality",
    }
    for augment in equipped_combat_augments(equipment_data, combat_status):
        if str(augment.get("type") or "").casefold() != "node":
            continue
        identity = augment_identity(augment)
        stat = next((value for token, value in aliases.items() if token in identity), None)
        if not stat or stat in totals:
            continue
        totals[stat] = totals.get(stat, 0.0) + node_values[stat][augment_level(augment) - 1]
    for stat, cap in {"bulwark": 150, "melee_ward": 50, "ranged_ward": 50,
                      "magic_ward": 50, "antivenom": 50, "staunch": 50,
                      "frost_ward": 50}.items():
        if stat in totals:
            totals[stat] = min(cap, totals[stat])
    return totals


def equipped_augment_offense(equipment_data, combat_status, style):
    """Extract explicit damage/DoT bonuses from the live equipped augment rows.

    Only values represented as numeric fields or unambiguous effect text are
    used; augment names alone are never treated as evidence of a bonus.
    """
    rows = (combat_status or {}).get("augments", []) or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    # combat/status already returns the complete equipped list; use per-slot
    # augments only as fallback to avoid counting the same enchant twice.
    if not rows:
        for item in ((equipment_data or {}).get("equipment", {}) or {}).values():
            if isinstance(item, dict):
                rows.extend(item.get("augments", []) or [])
    damage_pct = 0.0
    dot_damage_per_proc = 0.0
    dot_proc_chance = 0.0
    dot_ticks = 1.0
    for augment in rows:
        if not isinstance(augment, dict):
            continue
        kind = str(augment.get("type") or "").casefold()
        # Weapon specs consume a separate charge bar and have cooldown/charge
        # timing; they are not passive per-hit damage and are excluded here.
        if kind != "enchant":
            continue
        effect = str(augment.get("effect") or "")
        effect_lc = effect.casefold()
        values = augment.get("values") or augment.get("effect_values") or {}
        if isinstance(values, dict):
            for key, value in values.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                key_lc = str(key).casefold()
                if "chance" in key_lc or "proc" in key_lc:
                    if any(token in key_lc for token in ("dot", "bleed", "burn", "poison")):
                        dot_proc_chance = max(dot_proc_chance, number / 100.0 if number > 1 else number)
                elif "dot" in key_lc or any(token in key_lc for token in ("bleed", "burn", "poison")):
                    if "damage" in key_lc and ("total" in key_lc or "proc" in key_lc or "dot" in key_lc):
                        dot_damage_per_proc += max(0.0, number)
                elif "damage" in key_lc and ("pct" in key_lc or "percent" in key_lc):
                    damage_pct += number / 100.0
        if "damage over time" in effect_lc or any(token in effect_lc for token in ("bleed", "burn", "poison")):
            # Parse only a fully specified chance + total damage effect.
            chance_match = re.search(r"([\d.]+)\s*%\s*chance", effect_lc)
            damage_match = re.search(r"([\d.]+)\s*(?:total\s*)?damage\s*(?:over|for)\s*([\d.]+)\s*(?:ticks?|seconds?)", effect_lc)
            if chance_match and damage_match:
                dot_proc_chance = max(dot_proc_chance, float(chance_match.group(1)) / 100.0)
                dot_damage_per_proc += float(damage_match.group(1))
                dot_ticks = max(dot_ticks, float(damage_match.group(2)) / 4.0
                                if "second" in effect_lc else float(damage_match.group(2)))
        elif "damage" in effect_lc and "%" in effect_lc:
            match = re.search(r"([+-]?[\d.]+)\s*%\s*(?:more\s+)?damage", effect_lc)
            if match:
                damage_pct += float(match.group(1)) / 100.0
    return (max(0.0, damage_pct), max(0.0, dot_damage_per_proc),
            max(0.0, min(1.0, dot_proc_chance)), max(1.0, dot_ticks))


def equipped_combat_augments(equipment_data, combat_status):
    """Merge live combat and equipped-item augment lists without duplicates."""
    rows = (combat_status or {}).get("augments", []) or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    rows = [row for row in rows if isinstance(row, dict)]
    for item in ((equipment_data or {}).get("equipment", {}) or {}).values():
        if isinstance(item, dict):
            item_augments = item.get("augments", []) or []
            if isinstance(item_augments, dict):
                item_augments = list(item_augments.values())
            rows.extend(row for row in item_augments if isinstance(row, dict))
    unique = []
    seen = set()
    for row in rows:
        identity = tuple(str(row.get(field) or "").casefold() for field in
                         ("type", "key", "name", "level", "effect"))
        if identity not in seen:
            seen.add(identity)
            unique.append(row)
    return unique


def augment_identity(augment):
    return " ".join(str(augment.get(field) or "") for field in ("key", "name")).casefold().replace("'", "")


def augment_level(augment):
    try:
        return max(1, min(5, int(augment.get("level", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def augment_enchant_profile(augments, player_style, weak_to, max_hit, average_hit, monster_hp, is_boss,
                           dot_cap_max_hit=None, dot_resist_pct=None, dot_cap_pct=50.0):
    """Expected passive enchant effects using the player's in-game guide data."""
    proc_levels = {
        "flame brand": [12, 16, 20, 24, 30],
        "frostbite": [8, 12, 16, 20, 25],
        "rend": [10, 14, 18, 22, 28],
        "venom": [10, 15, 20, 25, 30],
    }
    damage_levels = [5, 8, 11, 14, 18]
    damage_pct = 0.0
    dot_damage_per_attack = 0.0
    freeze_chance = 0.0
    cap_hit = max_hit if dot_cap_max_hit is None else max(0.0, float(dot_cap_max_hit))
    cap_fraction = max(0.0, min(100.0, float(dot_cap_pct))) / 100.0
    resistance = (50.0 if is_boss else 0.0) if dot_resist_pct is None else max(0.0, min(100.0, float(dot_resist_pct)))
    dot_multiplier = 1.0 - resistance / 100.0
    weak_text = str(weak_to or "").casefold()
    weak_tokens = {
        "melee": ("melee", "attack", "strength"),
        "ranged": ("ranged", "arrow", "bow"),
        "magic": ("magic", "fire", "water", "earth", "air", "spell"),
    }.get(player_style, ())
    matches_weakness = any(token in weak_text for token in weak_tokens)
    for augment in augments:
        if str(augment.get("type") or "").casefold() != "enchant":
            continue
        identity = augment_identity(augment)
        li = augment_level(augment) - 1
        if "slayer" in identity and "edge" in identity and matches_weakness:
            damage_pct += damage_levels[li] / 100.0
        for name, levels in proc_levels.items():
            if name not in identity:
                continue
            chance = levels[li] / 100.0
            if name == "frostbite":
                freeze_chance = max(freeze_chance, chance)
            elif name == "flame brand":
                tick = min(average_hit * 0.20, cap_hit * cap_fraction)
                dot_damage_per_attack += chance * tick * 3 * dot_multiplier
            elif name == "rend":
                tick = min(monster_hp * 0.02, cap_hit * cap_fraction)
                dot_damage_per_attack += chance * tick * 5 * dot_multiplier
            elif name == "venom":
                tick = min(monster_hp * 0.03, cap_hit * cap_fraction)
                dot_damage_per_attack += chance * tick * 5 * dot_multiplier
    return damage_pct, dot_damage_per_attack, freeze_chance


def augment_special_profile(augments, player_style):
    """Return the style-compatible special's per-use damage multiplier."""
    specials = {
        "execute": ("melee", [1.60, 1.80, 2.00, 2.30, 2.60]),
        "frost nova": ("magic", [1.50, 1.65, 1.80, 2.00, 2.20]),
        "pyroblast": ("magic", [1.80, 2.00, 2.20, 2.40, 2.60]),
        "snipe": ("ranged", [1.50, 1.65, 1.80, 2.00, 2.20]),
        "volley": ("ranged", [1.70, 1.90, 2.10, 2.30, 2.50]),
        "whirlwind slash": ("melee", [1.80, 2.00, 2.20, 2.40, 2.60]),
    }
    for augment in augments:
        if str(augment.get("type") or "").casefold() != "spec":
            continue
        identity = augment_identity(augment)
        for name, (style, levels) in specials.items():
            if name in identity and style == player_style:
                multiplier = levels[augment_level(augment) - 1]
                return name, multiplier, multiplier * 2 if name == "execute" else None
    return None, 1.0, None


def build_combat_results(wiki_monsters, live_monster_data, equipment_data, player,
                         market, wiki_items, food_cost_per_hp, mastery_data=None,
                         live_drop_data=None, combat_status=None, active_buffs=None,
                         _owned_setup=None, research_data=None,
                         clan_research_data=None, combat_stats_data=None):
    """Estimate solo repeatable combat using current skills, gear, prices, and Wiki drops."""
    if not wiki_monsters:
        return []
    skills = player.get("skills", {}) or {}
    level = lambda skill: float((skills.get(skill) or {}).get("level", 1) or 1)
    gear = (equipment_data or {}).get("equipment", {}) or {}
    gear_stats = (equipment_data or {}).get("stats", {}) or {}
    if not gear_stats:
        return []
    combat_xp_bonus_pct, combat_xp_sources = combat_xp_bonus_info(
        player, equipment_data, combat_status, active_buffs,
        research_data, clan_research_data
    )
    research_totals, research_names = research_bonus_totals(research_data, clan_research_data)
    augment_totals = equipped_augment_totals(equipment_data, combat_status)
    combat_augments = equipped_combat_augments(equipment_data, combat_status)
    special_augment_key, special_augment_multiplier, execute_finisher_multiplier = augment_special_profile(
        combat_augments,
        "magic" if str((gear.get("main_hand") or {}).get("weapon_type") or "").casefold() in {"magic", "staff", "wand"}
        else "ranged" if str((gear.get("main_hand") or {}).get("weapon_type") or "").casefold() in {"ranged", "bow", "crossbow"}
        else "melee",
    )

    # Compare the strongest owned weapon for each style. The current armor and
    # defensive bonuses stay equipped; bank/inventory weapon stats replace the
    # currently equipped main-hand contribution for each modeled setup.
    if _owned_setup is None:
        owned_weapons = {}
        owned_ammo = []
        for filename in ("inventory.json", "bank.json"):
            try:
                with open(filename, "r", encoding="utf-8") as owned_file:
                    owned_rows = json.load(owned_file).get("items", [])
            except (OSError, ValueError, TypeError):
                owned_rows = []
            for item in owned_rows:
                if not isinstance(item, dict) or item.get("is_locked"):
                    continue
                try:
                    quantity = float(item.get("quantity", 0) or 0)
                except (TypeError, ValueError):
                    quantity = 0
                if quantity <= 0:
                    continue
                slot = str(item.get("equip_slot") or "").casefold()
                item_type = str(item.get("weapon_type") or "").casefold()
                ammo_identity = " ".join(str(item.get(field) or "") for field in ("category", "name", "item_key")).casefold()
                if slot in {"back", "ammo"} and (item_type in {"ranged", "arrow", "bolt"}
                        or "arrow" in ammo_identity or "bolt" in ammo_identity):
                    owned_ammo.append(item)
                if (item.get("is_equippable") and slot in {"weapon", "main_hand"}
                        and item.get("item_key")):
                    style = ("magic" if item_type in {"magic", "staff", "wand"} else
                             "ranged" if item_type in {"ranged", "bow", "crossbow"} else "melee")
                    skill_req = {"melee": "attack", "ranged": "ranged", "magic": "magic"}[style]
                    try:
                        required = float(item.get("required_level", 0) or 0)
                    except (TypeError, ValueError):
                        required = 0
                    if required > level(skill_req):
                        continue
                    bonus_field = {"melee": "stat_melee_atk", "ranged": "stat_ranged_atk", "magic": "stat_magic_atk"}[style]
                    score = float(item.get(bonus_field, 0) or 0)
                    current = owned_weapons.get(style)
                    if current is None or score > float(current.get(bonus_field, 0) or 0):
                        owned_weapons[style] = item

        current_weapon = gear.get("main_hand") or {}
        current_style = str(current_weapon.get("weapon_type") or "").casefold()
        current_style = ("magic" if current_style in {"magic", "staff", "wand"} else
                         "ranged" if current_style in {"ranged", "bow", "crossbow"} else "melee")
        # The live equipment endpoint is authoritative for the active style.
        # Inventory/bank snapshots can contain an older, stronger weapon that
        # is no longer equipped, so they must not replace what the player is
        # currently using.
        if current_weapon.get("item_key") or current_weapon.get("name"):
            owned_weapons[current_style] = current_weapon
        ammo = gear.get("back") or gear.get("ammo")
        if not ammo and owned_ammo:
            ammo = max(owned_ammo, key=lambda item: float(item.get("quantity", 0) or 0))

        combined = []
        for style, candidate in owned_weapons.items():
            if style == "ranged" and not ammo:
                continue
            modeled = dict(equipment_data)
            modeled["equipment"] = dict(gear)
            modeled["equipment"]["main_hand"] = candidate
            if style == "ranged":
                modeled["equipment"]["back"] = ammo
            modeled["stats"] = dict(gear_stats)
            stat_key = {"melee": "melee_atk", "ranged": "ranged_atk", "magic": "magic_atk"}[style]
            item_stat = {"melee": "stat_melee_atk", "ranged": "stat_ranged_atk", "magic": "stat_magic_atk"}[style]
            old_bonus = float(current_weapon.get(item_stat, 0) or 0)
            new_bonus = float(candidate.get(item_stat, 0) or 0)
            modeled["stats"][stat_key] = max(0.0, float(gear_stats.get(stat_key, 0) or 0) - old_bonus + new_bonus)
            rows = build_combat_results(wiki_monsters, live_monster_data, modeled, player,
                                        market, wiki_items, food_cost_per_hp, mastery_data,
                                        live_drop_data,
                                        combat_status, active_buffs,
                                        (style, candidate, ammo),
                                        research_data, clan_research_data, combat_stats_data)
            combined.extend(rows)
        return combined

    setup_style, setup_weapon, setup_ammo = _owned_setup
    weapon = gear.get("main_hand") or {}
    weapon_type = str(weapon.get("weapon_type") or "").casefold()
    if weapon_type in {"magic", "staff", "wand"}:
        player_style = "magic"
    elif weapon_type in {"ranged", "bow", "crossbow"}:
        player_style = "ranged"
    else:
        player_style = "melee"

    if player_style == "melee":
        attack_bonus = float(gear_stats.get("melee_atk", 0) or 0) + float((equipment_data or {}).get("clan_atk", 0) or 0)
        accuracy_skill = level("attack")
        damage_skill = level("strength")
        damage_stat = "strength"
        spell = None
        supplies = {}
    elif player_style == "ranged":
        attack_bonus = float(gear_stats.get("ranged_atk", 0) or 0)
        accuracy_skill = damage_skill = level("ranged")
        damage_stat = "ranged"
        ammo = gear.get("back") or gear.get("ammo") or {}
        ammo_key = ammo.get("item_key")
        if not ammo_key:
            return []
        supplies = {ammo_key: 1.0}
        spell = None
    else:
        attack_bonus = float(gear_stats.get("magic_atk", 0) or 0)
        accuracy_skill = damage_skill = level("magic")
        damage_stat = "magic"
        eligible = [entry for entry in MAGIC_SPELLS if entry[0] <= level("magic")]
        spell = max(eligible, key=lambda entry: (entry[1], entry[0])) if eligible else None
        if not spell:
            return []
        supplies = spell[2]

    combat_skills = (live_monster_data.get("combat_skills", {})
                     if isinstance(live_monster_data, dict) else {}) or {}
    potion_attack_bonus = float(combat_skills.get("_atk_buff", 0) or 0)
    attack_bonus += potion_attack_bonus
    accuracy = accuracy_skill + attack_bonus
    equipment_defence = float(gear_stats.get("def", 0) or 0) + augment_totals.get("bulwark", 0.0)
    player_defence = max(1.0, level("defence") + equipment_defence)
    player_hp = max(10.0, level("hitpoints"))
    try:
        player_hp = max(player_hp, float((player.get("hp") or {}).get("max", player_hp)))
    except (TypeError, ValueError):
        pass
    player_hp += augment_totals.get("vitality", 0.0)

    # `/monsters` is the authenticated game source; its richer records add
    # defence bonuses and access gates not published on the Wiki pages.
    if isinstance(live_monster_data, dict):
        live_rows = live_monster_data.get("monsters", []) or []
    else:
        live_rows = live_monster_data or []
    if isinstance(live_rows, dict):
        live_rows = list(live_rows.values())
    live_by_key = {str(row.get("key") or row.get("monster_key")): row
                   for row in live_rows if isinstance(row, dict)}
    combat_areas = (live_monster_data.get("combat_areas", [])
                    if isinstance(live_monster_data, dict) else []) or []
    area_levels = {str(area.get("area_key")): float(area.get("min_combat_level", 0) or 0)
                   for area in combat_areas if isinstance(area, dict)}
    dungeon_areas = {str(area.get("area_key")) for area in combat_areas
                     if isinstance(area, dict) and area.get("is_dungeon")}
    combat_level = math.floor(
        (level("defence") + max(level("hitpoints"), 10)) / 4
        + max((level("attack") + level("strength")) * 0.325,
              level("magic") * 0.5, level("ranged") * 0.5)
    )
    slayer_task = (live_monster_data or {}).get("slayer_task") if isinstance(live_monster_data, dict) else None
    slayer_task_key = str((slayer_task or {}).get("monster_key", ""))
    snapshot_payload = combat_stats_data if isinstance(combat_stats_data, dict) else {}
    snapshot_stats = snapshot_payload.get("stats", {}) or {}
    snapshot_style = str(snapshot_payload.get("attack_style") or "").casefold()
    snapshot_style = ("melee" if snapshot_style in {"attack", "strength", "defence", "defense", "melee", "melee_shared"}
                      else "ranged" if snapshot_style in {"ranged", "range"}
                      else "magic" if snapshot_style in {"magic", "magic_shared"} else snapshot_style)
    snapshot_monster = "".join(ch for ch in str(snapshot_payload.get("monster_name") or "").casefold() if ch.isalnum())
    try:
        snapshot_player_max_hit = float(snapshot_stats.get("player_max_hit", 0) or 0)
        snapshot_dot_cap_pct = float(snapshot_stats.get("dot_cap_pct", 50) or 50)
        snapshot_dot_resist_pct = float(snapshot_stats["monster_dot_resist_pct"]) if snapshot_stats.get("monster_dot_resist_pct") is not None else None
    except (TypeError, ValueError):
        snapshot_player_max_hit, snapshot_dot_cap_pct, snapshot_dot_resist_pct = 0.0, 50.0, None
    attack_tick_seconds = 4.0
    results = []
    if isinstance(mastery_data, dict):
        mastery_rows = mastery_data.get("masteries", []) or []
    else:
        mastery_rows = mastery_data or []
    if isinstance(mastery_rows, dict):
        mastery_rows = list(mastery_rows.values())
    mastery_by_monster = {}
    for row in mastery_rows:
        if not isinstance(row, dict):
            continue
        action_key = str(row.get("action_key") or "")
        if action_key.startswith("combat:"):
            mastery_by_monster[action_key.split(":", 1)[1]] = row

    for key, wiki_monster in wiki_monsters.items():
        live = live_by_key.get(key, {})
        if not live:
            live = wiki_monster
        wiki_name = "".join(ch for ch in str(wiki_monster.get("name") or "").casefold() if ch.isalnum())
        use_live_dot_snapshot = bool(
            snapshot_stats and snapshot_player_max_hit > 0
            and snapshot_style == player_style
            and snapshot_monster and snapshot_monster == wiki_name
        )
        dot_cap_max_hit = snapshot_player_max_hit if use_live_dot_snapshot else None
        dot_cap_pct = snapshot_dot_cap_pct if use_live_dot_snapshot else 50.0
        dot_resist_pct = snapshot_dot_resist_pct if use_live_dot_snapshot else None
        if not live.get("combat_level") and not wiki_monster.get("combat_level"):
            continue
        special_encounter = bool(
            live.get("is_boss") or live.get("is_superior")
            or str(live.get("type", "")).upper() in {"BOSS", "SUPERIOR"}
        )
        dot_resistant_encounter = bool(
            live.get("is_boss") or str(live.get("type", "")).upper() == "BOSS"
        )
        slayer_task_required = bool(live.get("slayer_only") and key != slayer_task_key)
        area_key = live.get("area_key") or wiki_monster.get("area_key")
        dungeon_target = str(area_key) in dungeon_areas
        required_combat = area_levels.get(str(area_key), 0)
        if not required_combat:
            area_text = str(wiki_monster.get("area") or "")
            requirement = re.search(r"unlocks? at combat level\s+(\d+)", area_text, re.I)
            if requirement:
                required_combat = float(requirement.group(1))
        if required_combat and combat_level < required_combat:
            continue

        try:
            hp = float(wiki_monster.get("hitpoints") or live.get("hitpoints"))
            monster_max_hit = float(wiki_monster.get("max_hit") or live.get("max_hit") or 0)
        except (TypeError, ValueError):
            continue
        if hp <= 0:
            continue
        monster_defence = max(
            1.0,
            float(next((live[field] for field in ("defence", "defense", "defence_level", "defence_stat")
                        if live.get(field) not in (None, "")),
                     (live.get("combat_level") or wiki_monster.get("combat_level") or 1)
                     + float(live.get("defence_bonus", 0) or 0))),
        )
        triangle = combat_style_multiplier(player_style, wiki_monster.get("attack_style"), None)
        hit_chance = max(0.05, min(0.95, (accuracy * triangle) / max(1.0, accuracy * triangle + monster_defence)))
        multiplier = combat_style_multiplier(
            player_style,
            wiki_monster.get("attack_style"),
            wiki_monster.get("weak_to"),
        )
        spell_bonus = float(spell[1]) if spell else 0.0
        if player_style == "magic":
            max_hit = (0.7 * damage_skill) + (0.4 * attack_bonus) + spell_bonus
        else:
            max_hit = damage_skill + (0.4 * attack_bonus)
        power_bonus = 0.0
        power_keys = {
            "melee": ("melee_power", "node:strike_training"),
            "ranged": ("ranged_power", "node:marksmanship"),
            "magic": ("magic_power", "node:arcane_focus"),
        }[player_style]
        for power_key in power_keys:
            power_bonus += research_totals.get(power_key, 0.0)
        max_hit *= 1.0 + max(0.0, power_bonus)
        bestiary_kills = int(live.get("bestiary_kills", 0) or 0)
        bestiary_damage_pct = (5 if bestiary_kills >= 1000 else 0) + (5 if bestiary_kills >= 5000 else 0)
        max_hit *= 1.0 + bestiary_damage_pct / 100.0
        max_hit *= multiplier
        augment_damage_pct, _, _ = augment_enchant_profile(
            combat_augments, player_style, wiki_monster.get("weak_to"),
            max_hit, max_hit, hp, dot_resistant_encounter,
            dot_cap_max_hit, dot_resist_pct, dot_cap_pct
        )
        max_hit *= 1.0 + augment_damage_pct
        average_hit = max(0.0, (max_hit + 1.0) / 2.0)
        gear_crit_chance = 0.0
        for field in ("crit_chance", "stat_crit_chance", "crit_rate"):
            try:
                value = float(gear_stats.get(field, 0) or 0)
            except (TypeError, ValueError):
                continue
            gear_crit_chance += value / 100.0 if value > 1.0 else value
        crit_chance = max(0.0, min(0.70, gear_crit_chance + sum(
            research_totals.get(k, 0.0) for k in ("crit_chance", "node:killer_instinct")
        )))
        # Wiki states crit multiplier starts at 1.25x; gear crit stats may be
        # exposed as percent points in the equipment payload.
        crit_mult_bonus = max(0.0, float(gear_stats.get("crit_mult", 0) or 0))
        crit_multiplier = 1.25 + crit_mult_bonus
        average_hit *= 1.0 + crit_chance * (crit_multiplier - 1.0)
        _, augment_dot_damage, freeze_chance = augment_enchant_profile(
            combat_augments, player_style, wiki_monster.get("weak_to"),
            max_hit, average_hit, hp, dot_resistant_encounter,
            dot_cap_max_hit, dot_resist_pct, dot_cap_pct
        )
        expected_damage_per_attack = max(
            0.01,
            (average_hit + augment_dot_damage) * hit_chance,
        )
        attacks_to_kill = max(1, math.ceil(hp / expected_damage_per_attack))
        fight_seconds = attacks_to_kill * attack_tick_seconds
        # Swift Hunter research reports a flat seconds-per-level reduction.
        # The base combat respawn is 15 seconds; global and clan nodes stack.
        respawn_reduction = max(0.0, research_totals.get("respawn_reduction", 0.0))
        respawn_seconds = max(0.0, 15.0 - respawn_reduction)
        kill_cycle_seconds = fight_seconds + respawn_seconds
        kills_per_hour = 3600.0 / kill_cycle_seconds
        damage_per_hour = hp * kills_per_hour

        incoming_model_known = "attack_bonus" in live and "strength_bonus" in live
        monster_attack = float(live.get("attack_bonus", 0) or 0)
        monster_strength = float(live.get("strength_bonus", 0) or 0)
        monster_style = str(wiki_monster.get("attack_style") or live.get("attack_style") or "").casefold()
        if any(token in monster_style for token in ("melee", "attack", "strength", "defence", "defense")):
            monster_style = "melee"
        elif any(token in monster_style for token in ("ranged", "range", "bow", "arrow")):
            monster_style = "ranged"
        elif any(token in monster_style for token in ("magic", "spell", "staff")):
            monster_style = "magic"
        incoming_triangle = combat_style_multiplier(monster_style, player_style, None)
        monster_accuracy = max(0.0, (monster_attack + 0.25 * monster_strength) * incoming_triangle)
        incoming_hit_chance = max(0.05, min(0.95, monster_accuracy / max(1.0, monster_accuracy + player_defence)))
        monster_speed = float(wiki_monster.get("attack_speed_seconds") or 4.0)
        monster_speed = max(attack_tick_seconds, monster_speed)
        # Frostbite skips one upcoming monster swing per successful proc.
        expected_freezes_per_kill = attacks_to_kill * hit_chance * freeze_chance
        incoming_attacks_per_kill = max(0.0, fight_seconds / monster_speed - expected_freezes_per_kill)
        ward_key = {"melee": "melee_ward", "ranged": "ranged_ward", "magic": "magic_ward"}.get(monster_style)
        ward_reduction = min(50.0, max(0.0, augment_totals.get(ward_key, 0.0))) if ward_key else 0.0
        incoming_max_hit = monster_max_hit * incoming_triangle
        poison_tick_damage = max(0.0, float(next((live[field] for field in (
            "poison_damage_per_tick", "poison_dot_per_tick", "poison_damage"
        ) if live.get(field) not in (None, "")), 0) or 0))
        bleed_tick_damage = max(0.0, float(next((live[field] for field in (
            "bleed_damage_per_tick", "bleed_dot_per_tick", "bleed_damage"
        ) if live.get(field) not in (None, "")), 0) or 0))
        poison_chance = max(0.0, min(1.0, float(live.get("poison_chance", 0) or 0) / (100.0 if float(live.get("poison_chance", 0) or 0) > 1 else 1)))
        bleed_chance = max(0.0, min(1.0, float(live.get("bleed_chance", 0) or 0) / (100.0 if float(live.get("bleed_chance", 0) or 0) > 1 else 1)))
        poison_ticks = max(1.0, float(live.get("poison_duration_ticks", 1) or 1))
        bleed_ticks = max(1.0, float(live.get("bleed_duration_ticks", 1) or 1))
        incoming_status_damage = incoming_attacks_per_kill * (
            poison_chance * poison_tick_damage * poison_ticks * (1.0 - min(50.0, augment_totals.get("antivenom", 0.0)) / 100.0)
            + bleed_chance * bleed_tick_damage * bleed_ticks * (1.0 - min(50.0, augment_totals.get("staunch", 0.0)) / 100.0)
        )
        expected_damage_taken_per_kill = (
            incoming_attacks_per_kill * incoming_hit_chance
            * ((incoming_max_hit + 1.0) / 2.0) * 1.0125
            * (1.0 - ward_reduction / 100.0)
        ) + incoming_status_damage
        damage_taken_hour = expected_damage_taken_per_kill * kills_per_hour if incoming_model_known else None
        food_cost_hour = (
            damage_taken_hour * food_cost_per_hp["cost_per_hp"]
            if food_cost_per_hp and damage_taken_hour is not None else None
        )

        monster_mastery = mastery_by_monster.get(key, {})
        mastery_output_mult = max(1.0, float(monster_mastery.get("output_mult", 1) or 1))
        mastery_rare_mult = max(1.0, float(monster_mastery.get("rare_drop_mult", 1) or 1))
        live_drops_response = (live_drop_data or {}).get(key) or {}
        live_drops = live_drops_response.get("drops", []) or []
        live_drop_by_key = {str(drop.get("item_key")): drop for drop in live_drops
                            if isinstance(drop, dict) and drop.get("item_key")}
        drop_data_source = "Live game drop data + Wiki fallback" if live_drop_by_key else "Wiki drop data fallback"
        drop_amounts = {}
        wiki_drops = wiki_monster.get("drops", []) or []
        wiki_item_keys = set()
        for drop in wiki_drops:
            item_key = drop.get("item_key")
            if item_key:
                wiki_item_keys.add(str(item_key))
            try:
                live_drop = live_drop_by_key.get(str(item_key))
                if live_drop:
                    quantity_min = float(live_drop.get("qty_min", drop.get("quantity_min", 1)))
                    quantity_max = float(live_drop.get("qty_max", drop.get("quantity_max", quantity_min)))
                    quantity = (quantity_min + quantity_max) / 2
                    # Live `chance` and `eff_chance` are percentages. Use the
                    # server-calculated effective chance, which already reflects
                    # personal gear, research, clan/Well, Bestiary and rare mastery.
                    raw_chance = live_drop.get("eff_chance")
                    if raw_chance is None:
                        raw_chance = live_drop.get("chance")
                    chance = max(0.0, min(1.0, float(raw_chance) / 100.0))
                else:
                    quantity = (float(drop.get("quantity_min", 1)) + float(drop.get("quantity_max", 1))) / 2
                    chance = max(0.0, min(1.0, float(drop.get("drop_chance"))))
                    # Wiki-only entries do not have an effective chance from the
                    # authenticated endpoint, so retain the known per-monster
                    # rare mastery adjustment as the conservative fallback.
                    if chance < 0.05:
                        chance = min(1.0, chance * mastery_rare_mult)
            except (TypeError, ValueError):
                continue
            if item_key:
                drop_amounts[item_key] = drop_amounts.get(item_key, 0.0) + quantity * chance * mastery_output_mult * kills_per_hour
        # Preserve live-only entries too (for example drops absent from the Wiki scrape).
        for item_key, drop in live_drop_by_key.items():
            if item_key in wiki_item_keys:
                continue
            try:
                quantity = (float(drop.get("qty_min", 1)) + float(drop.get("qty_max", 1))) / 2
                raw_chance = drop.get("eff_chance")
                if raw_chance is None:
                    raw_chance = drop.get("chance", 0)
                chance = max(0.0, min(1.0, float(raw_chance) / 100.0))
            except (TypeError, ValueError):
                continue
            drop_amounts[item_key] = drop_amounts.get(item_key, 0.0) + quantity * chance * mastery_output_mult * kills_per_hour

        loot_revenue_hour = 0.0
        unpriced_drops = 0
        price_sources = set()
        bid_depth = 0
        output_volume_hour = 0.0
        item_stats = []
        item_market_coverages = []
        market_share_requirements = []
        for item_key, amount_hour in drop_amounts.items():
            revenue, remaining, unpriced, sources = combat_sell_value(item_key, amount_hour, market, wiki_items)
            loot_revenue_hour += revenue
            unpriced_drops += unpriced
            price_sources.update(sources)
            book = market.get(item_key) or {}
            stats = market_activity_stats(book)
            item_stats.append((item_key, stats))
            bid_depth += stats["bid_depth"]
            output_volume_hour += amount_hour
            item_market_coverages.append(stats["bid_depth"] / amount_hour if amount_hour > 0 else 0)
            weekly_volume = (wiki_items.get(item_key) or {}).get("market_volume_7d")
            try:
                weekly_volume = float(weekly_volume)
            except (TypeError, ValueError):
                weekly_volume = 0
            if weekly_volume > 0:
                market_share_requirements.append(amount_hour * 24 / (weekly_volume / 7) * 100)

        consumable_cost_hour = 0.0
        consumables_known = True
        stock_hours = []
        stock_cycles = []
        ingredient_rows = []
        for item_key, quantity_per_attack in supplies.items():
            use_per_hour = quantity_per_attack * attacks_to_kill * kills_per_hour
            unit_cost = estimate_item_buy_price(item_key, market, wiki_items)
            if unit_cost is None:
                consumables_known = False
            else:
                consumable_cost_hour += unit_cost * use_per_hour
            item_entry = market.get(item_key) or {}
            item_meta = item_entry.get("item") or {}
            owned = float(item_meta.get("inventory_qty", 0) or 0) + float(item_meta.get("bank_qty", 0) or 0)
            use_per_kill = quantity_per_attack * attacks_to_kill
            ingredient_rows.append({"item_key": item_key, "quantity": use_per_kill})
            if use_per_hour > 0:
                stock_hours.append(owned / use_per_hour)
            if use_per_kill > 0:
                stock_cycles.append(owned / use_per_kill)

        net_profit_hour = None
        if not unpriced_drops and consumables_known and food_cost_hour is not None:
            net_profit_hour = loot_revenue_hour - consumable_cost_hour - food_cost_hour
        elif not unpriced_drops and consumables_known and damage_taken_hour == 0:
            net_profit_hour = loot_revenue_hour - consumable_cost_hour

        valuation_bits = sorted(price_sources)
        if not valuation_bits:
            valuation_bits = ["No priced drops"]
        source_prefix = "Stale market" if (MARKET_SNAPSHOT_AGE_HOURS or 0) > 24 else "Market estimate"
        valuation_source = f"{source_prefix} + {drop_data_source} ({', '.join(valuation_bits)})"
        if unpriced_drops:
            valuation_source += f"; {unpriced_drops} unpriced drop types"

        stats_rows = [stats for _, stats in item_stats]
        active_buy_count = sum(stats["active_buy_order_count"] for stats in stats_rows)
        active_sell_count = sum(stats["active_sell_listing_count"] for stats in stats_rows)
        listed_volume = sum(stats["listed_sell_volume"] for stats in stats_rows)
        complete_values = [stats["market_data_complete"] for stats in stats_rows]
        market_complete = (all(value is True for value in complete_values)
                           if complete_values and all(value is not None for value in complete_values)
                           else None)
        saturation = (active_sell_count / (active_sell_count + active_buy_count) * 100
                      if active_sell_count + active_buy_count else None)

        if player_style == "melee":
            xp_modes = [
                ("Melee", [("melee", 0.8, "Melee")]),
            ]
        elif player_style == "ranged":
            xp_modes = [("Ranged", [("ranged", 0.8, "Ranged")])]
        else:
            xp_modes = [("Magic", [("magic", 0.8, "Magic")])]

        monster_mastery = mastery_by_monster.get(key, {})
        mastery_xp_multiplier = max(1.0, float(monster_mastery.get("xp_multiplier", 1) or 1))
        combat_xp_multiplier = (1.0 + combat_xp_bonus_pct / 100.0) * mastery_xp_multiplier
        # Combat XP follows actual hit damage, including damage-over-time
        # procs. `damage_per_hour` is HP removed from monsters (and therefore
        # omits the final hit's overkill); use the expected hit stream for XP.
        xp_damage_per_hour = expected_damage_per_attack * attacks_to_kill * kills_per_hour
        combat_style_xp_hour = xp_damage_per_hour * 0.8 * combat_xp_multiplier
        combat_hp_xp_hour = xp_damage_per_hour * 0.33 * combat_xp_multiplier
        combat_total_xp_hour = combat_style_xp_hour + combat_hp_xp_hour
        # The supplied Melee_shared combat screenshot allocates 241 Attack,
        # 225 Strength, and 225 Defence XP for the same hit. Preserve that
        # observed split rather than assuming an even three-way division.
        shared_melee_xp_split = (241 / 691, 225 / 691, 225 / 691)
        for xp_mode, skill_splits in xp_modes:
            for target_skill, xp_factor, style_label in skill_splits:
                xp_hour = combat_total_xp_hour
                action_label = f"{wiki_monster.get('name', key)} · {xp_mode} · {style_label}"
                feasibility = "Ready now"
                if slayer_task_required:
                    feasibility = "Slayer task required"
                elif stock_hours and min(stock_hours) <= 0:
                    feasibility = "Need combat supplies"
                elif stock_hours:
                    feasibility = "Ready now"
                if incoming_model_known and expected_damage_taken_per_kill >= player_hp:
                    feasibility = "High risk: expected damage exceeds HP per kill"
                result = {
                "skill": target_skill,
                "key": f"combat:{key}:{player_style}:{xp_mode.lower().replace(' ', '_')}:{target_skill}",
                "label": action_label,
                "category": f"Combat · {player_style.title()} · {xp_mode} · {attacks_to_kill} ticks/kill",
                "min_level": live.get("slayer_level") if live.get("slayer_only") else 0,
                "profit_hour": net_profit_hour,
                "xp_hour": xp_hour,
                "output_per_hour": output_volume_hour,
                "bid_depth": bid_depth,
                "active_buy_order_count": active_buy_count,
                "active_sell_listing_count": active_sell_count,
                "listed_sell_volume": listed_volume,
                "listing_saturation_pct": saturation,
                "market_data_complete": market_complete,
                "market_coverage_hours": min(item_market_coverages) if item_market_coverages else None,
                "market_share_required_pct": max(market_share_requirements) if market_share_requirements else None,
                "valuation_source": valuation_source,
                "stock_cycles": min(stock_cycles) if stock_cycles else None,
                "stockpile_hours": min(stock_hours) if stock_hours else None,
                "stockpile_profit_hour": (loot_revenue_hour - (food_cost_hour or 0)) if not unpriced_drops and food_cost_hour is not None else None,
                "stockpile_profit_hour_high": None,
                "feasibility": feasibility,
                "missing_materials": ingredient_rows if feasibility == "Need combat supplies" else [],
                "missing_material_cost": None,
                "input_cost_hour": consumable_cost_hour if consumables_known else None,
                "price_gap_ratio": None,
                "gold_estimate_suppressed": False,
                "unverified_wiki_profit_hour": None,
                "kill_rate_per_hour": kills_per_hour,
                "combat_respawn_seconds": respawn_seconds,
                "combat_kill_cycle_seconds": kill_cycle_seconds,
                "combat_style": f"{player_style.title()} · {setup_weapon.get('name') or setup_weapon.get('item_key', 'owned weapon')} · {xp_mode} · {style_label}" + (f" · {spell[0]} Magic spell" if spell else ""),
                "estimated_damage_per_kill": hp,
                "combat_damage_taken_hour": damage_taken_hour,
                "combat_food_cost_hour": food_cost_hour,
                "combat_assumptions": "4s player ticks plus 15s base respawn reduced by live Institute/clan respawn research; kills/hr and XP/hr include fight and respawn cycle; total XP includes style and Hitpoints XP; XP uses expected damage per attack including modeled enchant DoT and final-hit overkill; shared melee XP allocation calibrated to the supplied combat screenshot; live Institute/clan research, triangle, weakness, crit and mastery bonuses; defensive augment mitigation; special multiplier shown per use but excluded from XP/hr because charge rate is unavailable; boss phases are not modeled",
                "combat_mastery_level": monster_mastery.get("level", 1),
                "combat_mastery_output_mult": mastery_output_mult,
                "combat_mastery_rare_drop_mult": mastery_rare_mult,
                "combat_mastery_xp_multiplier": mastery_xp_multiplier,
                "combat_xp_bonus_pct": combat_xp_bonus_pct,
                "combat_xp_multiplier": combat_xp_multiplier,
                "combat_xp_sources": combat_xp_sources,
                "combat_xp_attack_hour": combat_style_xp_hour * shared_melee_xp_split[0] if player_style == "melee" else None,
                "combat_xp_strength_hour": combat_style_xp_hour * shared_melee_xp_split[1] if player_style == "melee" else None,
                "combat_xp_defence_hour": combat_style_xp_hour * shared_melee_xp_split[2] if player_style == "melee" else None,
                "combat_xp_focused_hour": combat_style_xp_hour if player_style == "melee" else None,
                "combat_xp_style_hour": combat_style_xp_hour,
                "combat_xp_hitpoints_hour": combat_hp_xp_hour,
                "combat_xp_damage_hour": xp_damage_per_hour,
                "combat_xp_slayer_hour": (float(live["slayer_xp"]) * kills_per_hour * combat_xp_multiplier
                                           if live.get("slayer_xp") else None),
                "combat_modeled_max_hit": max_hit,
                "combat_modeled_hit_chance": hit_chance,
                "combat_modeled_kill_seconds": fight_seconds,
                "combat_research_power_pct": power_bonus * 100.0,
                "combat_crit_chance_pct": crit_chance * 100.0,
                "combat_triangle_multiplier": triangle,
                "combat_incoming_triangle_multiplier": incoming_triangle,
                "combat_augment_bulwark_def": augment_totals.get("bulwark", 0.0),
                "combat_augment_ward_pct": ward_reduction,
                "combat_augment_damage_pct": augment_damage_pct * 100.0,
                "combat_augment_dot_expected_per_attack": augment_dot_damage,
                "combat_dot_cap_source": (f"Live combat snapshot: {snapshot_stats.get('player_max_hit')} max hit, {dot_cap_pct:g}% cap, {dot_resist_pct if dot_resist_pct is not None else (50 if dot_resistant_encounter else 0):g}% target resistance"
                                           if use_live_dot_snapshot else "Modeled max hit; default 50% cap and inferred target resistance"),
                "combat_augment_freeze_chance_pct": freeze_chance * 100.0,
                "combat_augment_special_key": special_augment_key,
                "combat_augment_special_multiplier": special_augment_multiplier,
                "combat_augment_execute_finisher_multiplier": execute_finisher_multiplier,
                "combat_augment_antivenom_pct": augment_totals.get("antivenom", 0.0),
                "combat_augment_staunch_pct": augment_totals.get("staunch", 0.0),
                "combat_augment_frost_ward_pct": augment_totals.get("frost_ward", 0.0),
                "combat_augment_vitality_hp": augment_totals.get("vitality", 0.0),
                "combat_weakness_bonus_pct": 25 if combat_style_multiplier(
                    player_style, wiki_monster.get("attack_style"), wiki_monster.get("weak_to")
                ) > triangle else 0,
                "combat_potion_attack_bonus": potion_attack_bonus,
                "combat_bestiary_damage_pct": bestiary_damage_pct,
                "combat_context_warning": "; ".join(
                    warning for warning, active in (
                        ("special encounter phases not modeled", special_encounter),
                        ("dungeon wave/chest effects not modeled", dungeon_target),
                    ) if active
                ) or None,
                "combat_drop_data_source": drop_data_source,
                "combat_drop_bonuses": live_drops_response.get("bonuses", {}),
                "combat_food_basis": food_cost_per_hp,
                "unpriced_combat_drops": unpriced_drops,
                }
                results.append(result)

    return results


# --------------------------------------------------
# ACTION ANALYSIS
# --------------------------------------------------

def calculate_action(action, market, wiki_items=None, food_cost_per_hp=None):
    calc = action["calc"]
    wiki_items = wiki_items or {}

    production_per_hour = calculate_production_rate(calc)
    expected_stun_seconds = 0.0
    expected_damage_per_attempt = 0.0
    thieving_success_probability = 1.0
    if action.get("skill") == "thieving":
        try:
            thieving_success_probability = max(
                0.0,
                min(1.0, float(action.get("success_rate")) / 100.0),
            )
            bonuses = calc.get("bonuses", {}) or {}
            speed_bonus = float((bonuses.get("total", {}) or {}).get("speed") or 0)
            stun_probability = max(
                0.0,
                min(1.0, float(action.get("stun_chance") or 0) / 100.0),
            )
            raw_damage_probability = max(
                0.0,
                float(action.get("damage_chance") or 0) / 100.0,
            )
            damage_probability = min(
                raw_damage_probability,
                max(0.0, 1.0 - stun_probability),
            )
            stun_seconds = max(0.0, float(action.get("stun_seconds") or 0))
            if stun_seconds > 0:
                stun_seconds = max(
                    1.0,
                    math.ceil(stun_seconds * max(0.1, 1.0 - speed_bonus / 100.0)),
                )
            expected_stun_seconds = (
                (1.0 - thieving_success_probability)
                * stun_probability
                * stun_seconds
            )
            damage_min = max(0.0, float(action.get("damage_min") or 0))
            damage_max = max(damage_min, float(action.get("damage_max") or damage_min))
            expected_damage_per_attempt = (
                (1.0 - thieving_success_probability)
                * damage_probability
                * (damage_min + damage_max)
                / 2.0
            )
            if production_per_hour > 0 and expected_stun_seconds > 0:
                average_cycle_seconds = 3600.0 / production_per_hour
                production_per_hour = 3600.0 / (
                    average_cycle_seconds + expected_stun_seconds
                )
        except (TypeError, ValueError):
            thieving_success_probability = 1.0

    thieving_damage_hour = (
        production_per_hour * expected_damage_per_attempt
        if action.get("skill") == "thieving"
        else None
    )


    thieving_food_cost_hour = (
        thieving_damage_hour * food_cost_per_hp["cost_per_hp"]
        if thieving_damage_hour is not None and food_cost_per_hp
        else None
    )

    output_per_cycle = expected_output(action)

    output_per_hour = (
        production_per_hour
        * output_per_cycle
    )

    direct_coin_reward = (
        action.get("skill") == "thieving"
        and action.get("output_key") == "coins"
        and action.get("coins_min") is not None
        and action.get("coins_max") is not None
    )
    thieving_loot_low_hour = 0.0
    thieving_loot_high_hour = 0.0
    unpriced_thieving_drops = 0
    if direct_coin_reward:
        try:
            average_coins = (
                float(action["coins_min"])
                + float(action["coins_max"])
            ) / 2.0
            output_per_hour = (
                production_per_hour
                * average_coins
                * thieving_success_probability
            )
            loot_per_attempt, unpriced_thieving_drops = expected_thieving_loot_value(
                action,
                market,
                wiki_items,
            )
            thieving_loot_low_hour = (
                production_per_hour
                * thieving_success_probability
                * loot_per_attempt
            )
            thieving_loot_high_hour = production_per_hour * loot_per_attempt
        except (TypeError, ValueError):
            direct_coin_reward = False

    # What the current market can immediately absorb.
    immediate_revenue, effective_sell_price, unsold, bid_depth = (
        sell_output(
            action["output_key"],
            output_per_hour,
            market,
        )
    )
    output_market_stats = market_activity_stats(
        market.get(action.get("output_key")) or {}
    )

    input_cost_cycle = input_opportunity_cost(
        action["ingredients"],
        market,
        calc,
    )

    if input_cost_cycle is None:
        return {
            **action,
            "production_per_hour": production_per_hour,
            "xp_hour": expected_experience_per_hour(action, production_per_hour),
            "output_per_hour": output_per_hour,
            "effective_sell_price": effective_sell_price,
            "bid_depth": bid_depth,
            **output_market_stats,
            "market_coverage_hours": None,
            "thieving_damage_hour": thieving_damage_hour,
            "thieving_food_cost_hour": thieving_food_cost_hour,
            "thieving_food_basis": food_cost_per_hp,
            "input_cost_cycle": None,
            "input_cost_hour": None,
            "profit_hour": None,
        }

    input_cost_hour = (
        input_cost_cycle
        * production_per_hour
    )

    # This is a theoretical sustained rate if the market
    # replenishes at the currently observed prices.
    if direct_coin_reward:
        # Coin rewards are direct; loot value is shown as a success-gated
        # estimate with an ungated upper estimate until server behavior is proven.
        theoretical_revenue_hour = output_per_hour + thieving_loot_low_hour
        immediate_revenue = theoretical_revenue_hour
        profit_hour_high = output_per_hour + thieving_loot_high_hour
    else:
        theoretical_revenue_hour = (
            output_per_hour
            * (effective_sell_price or 0)
        )
        profit_hour_high = None

    theoretical_profit_hour = (
        theoretical_revenue_hour
        - input_cost_hour
    )
    if thieving_food_cost_hour is not None:
        theoretical_profit_hour -= thieving_food_cost_hour
        if profit_hour_high is not None:
            profit_hour_high = max(
                theoretical_profit_hour,
                profit_hour_high - thieving_food_cost_hour,
            )

    if output_per_hour > 0 and bid_depth > 0:
        market_coverage_hours = (
            bid_depth / output_per_hour
        )
    else:
        market_coverage_hours = 0
    if direct_coin_reward:
        market_coverage_hours = None

    return {
        **action,
        "production_per_hour": production_per_hour,
        "xp_hour": expected_experience_per_hour(action, production_per_hour),
        "direct_coin_reward": direct_coin_reward,
        "expected_stun_seconds_per_attempt": expected_stun_seconds,
        "thieving_damage_hour": thieving_damage_hour,
        "thieving_food_cost_hour": thieving_food_cost_hour,
        "thieving_food_basis": food_cost_per_hp,
        "thieving_loot_gp_hour_low": thieving_loot_low_hour,
        "thieving_loot_gp_hour_high": thieving_loot_high_hour,
        "unpriced_thieving_drops": unpriced_thieving_drops,
        "output_per_hour": output_per_hour,
        "effective_sell_price": effective_sell_price,
        "bid_depth": bid_depth,
        **output_market_stats,
        "market_coverage_hours": market_coverage_hours,
        "input_cost_cycle": input_cost_cycle,
        "input_cost_hour": input_cost_hour,
        "theoretical_revenue_hour": theoretical_revenue_hour,
        "profit_hour": theoretical_profit_hour,
        "profit_hour_high": max(theoretical_profit_hour, profit_hour_high)
        if profit_hour_high is not None
        else theoretical_profit_hour,
        "immediate_revenue_24h": immediate_revenue,
        "unsold_24h": unsold,
    }


# --------------------------------------------------
# LOAD ACTIONS
# --------------------------------------------------

def load_actions(skill_data, player):
    actions = []

    skills = player.get("skills", {})

    for skill, data in skill_data.items():

        current_level = skills.get(skill, {}).get(
            "level",
            0,
        )

        for action in data.get("actions", []):

            calc = action.get("calc", {})

            if not calc:
                continue

            duration_ms = calc.get("duration_ms")

            if not duration_ms or duration_ms <= 0:
                continue

            min_level = action.get(
                "min_level",
                0,
            )

            if current_level < min_level:
                continue

            xp_per_cycle = calc.get("xp_per_cycle")
            if xp_per_cycle is None:
                xp_per_cycle = action.get("xp", 0)

            actions.append({
                "skill": skill,
                "key": action.get("key"),
                "label": action.get("label"),
                "category": action.get("category"),
                "min_level": min_level,
                "duration_ms": duration_ms,
                "xp": xp_per_cycle,
                "success_rate": calc.get(
                    "success_rate",
                    action.get("success_rate"),
                ),
                "coins_min": action.get("coins_min"),
                "coins_max": action.get("coins_max"),
                "stun_chance": action.get("stun_chance"),
                "stun_seconds": action.get("stun_seconds"),
                "damage_chance": action.get("damage_chance"),
                "damage_min": action.get("damage_min"),
                "damage_max": action.get("damage_max"),
                "loot_drops": action.get("loot_drops", []),
                "rare_drops": action.get("seed_drops", []),
                "output_key": action.get(
                    "reward_item_key"
                ),
                "output_amount": expected_output(action),
                "ingredients": action.get(
                    "ingredients",
                    [],
                ),
                "action_available": action.get("available"),
                "action_bank_qty": action.get("bank_qty"),
                "seed_drops": action.get(
                    "seed_drops",
                    [],
                ),
                "calc": calc,
            })

    return actions


def add_feasibility(result, action, market, wiki_items):
    """Estimate how many full cycles can be made from owned inputs."""
    stock_cycles = None
    missing = []

    for ingredient in action.get("ingredients", []) or []:
        item_key = ingredient.get("item_key")
        try:
            quantity = float(ingredient.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue

        item = market.get(item_key) or {}
        item_data = item.get("item") or {}
        # The skill endpoint can return stale ingredient availability (for
        # example, zero for both fields while bank.json has current stock).
        # Prefer the inventory/bank quantities captured with the market
        # snapshot whenever those fields exist; use skill data only as fallback.
        if "inventory_qty" in item_data or "bank_qty" in item_data:
            inv = item_data.get("inventory_qty", ingredient.get("available"))
            bank = item_data.get("bank_qty", ingredient.get("bank_qty"))
        else:
            inv = ingredient.get("available")
            bank = ingredient.get("bank_qty")
        try:
            if inv is None:
                inv = item_data.get("inventory_qty", 0)
            if bank is None:
                bank = item_data.get("bank_qty", 0)
            owned = max(0.0, float(inv or 0)) + max(0.0, float(bank or 0))
        except (TypeError, ValueError):
            owned = 0.0

        available_cycles = int(owned // quantity)
        stock_cycles = available_cycles if stock_cycles is None else min(stock_cycles, available_cycles)

        shortfall = max(0.0, quantity - owned)
        if shortfall > 0:
            name = item_data.get("name") or (wiki_items.get(item_key) or {}).get("name") or item_key
            price = estimate_item_buy_price(item_key, market, wiki_items)
            missing.append({
                "item_key": item_key,
                "name": name,
                "quantity": shortfall,
                "price": price,
                "estimated_cost": shortfall * price if price is not None else None,
            })

    result["stock_cycles"] = stock_cycles
    result["missing_materials"] = missing
    result["missing_material_cost"] = (
        sum(item["estimated_cost"] for item in missing)
        if missing and all(item["estimated_cost"] is not None for item in missing)
        else (0.0 if not missing else None)
    )
    if not missing:
        result["feasibility"] = "Ready now"
    else:
        brief = ", ".join(
            f"{item['name']} x{item['quantity']:g}"
            for item in missing[:2]
        )
        result["feasibility"] = f"Need {brief}"
        if len(missing) > 2:
            result["feasibility"] += f" +{len(missing) - 2}"

    production_per_hour = result.get("production_per_hour")
    result["stockpile_hours"] = (
        stock_cycles / production_per_hour
        if stock_cycles is not None and production_per_hour and production_per_hour > 0
        else (0.0 if stock_cycles == 0 else None)
    )
    input_cost_hour = result.get("input_cost_hour")
    profit_hour = result.get("profit_hour")
    profit_hour_high = result.get("profit_hour_high", profit_hour)
    result["stockpile_profit_hour"] = (
        profit_hour + input_cost_hour
        if profit_hour is not None and input_cost_hour is not None
        else None
    )
    result["stockpile_profit_hour_high"] = (
        profit_hour_high + input_cost_hour
        if profit_hour_high is not None and input_cost_hour is not None
        else None
    )
    return result


# --------------------------------------------------
# DISPLAY
# --------------------------------------------------

def format_coins(value):
    if value is None:
        return "-"

    return f"{value:,.0f}"


def format_rate(value):
    return "-" if value is None else f"{value:,.1f}"


def format_hours(value):
    if value is None:
        return "-"

    if value >= 100:
        return f"{value:,.0f}h"

    if value >= 10:
        return f"{value:,.1f}h"

    return f"{value:.2f}h"


def format_percent(value):
    if value is None:
        return "-"
    return f"{value:,.0f}%"


def print_results(results, sort_key="profit_hour", title="TOP TASKS BY NET GOLD/HOUR", limit=20):

    results = sorted(
        (row for row in results if row.get(sort_key) is not None),
        key=lambda x: x[sort_key],
        reverse=True,
    )

    print()
    print("=" * 135)
    print(f"KORUXA OPTIMIZER — {title}")
    print("=" * 135)
    print()

    print(
        f"{'Skill':<14}"
        f"{'Action':<28}"
        f"{'Net Gold/hr low':>16}"
        f"{'Net Gold/hr high':>17}"
        f"{'Stockpile GP/hr':>16}"
        f"{'Stockpile h':>12}"
        f"{'XP/hr':>14}"
        f"{'Kills/hr':>12}"
        f"{'HP dmg/hr':>12}"
        f"{'Food GP/hr':>12}"
        f"{'Yield/hr':>14}"
        f"{'Active Buy Vol':>15}"
        f"{'Buy Orders':>12}"
        f"{'Sell Listings':>14}"
        f"{'Satur.':>9}"
        f"{'Book':>10}"
        f"{'Coverage':>12}"
        f"{'Mkt Share':>12}"
        f"{'Source':>18}"
            f"{'Stock cycles':>14}"
            f"{'Feasibility':>48}"
            f"{'Inputs GP/hr':>15}"
    )

    print("-" * 360)

    for r in results[:limit]:

        print(
            f"{str(r['skill']):<14}"
            f"{str(r['label'])[:27]:<28}"
            f"{format_coins(r['profit_hour']):>16}"
            f"{format_coins(r.get('profit_hour_high', r['profit_hour'])):>17}"
            f"{format_coins(r.get('stockpile_profit_hour')):>16}"
            f"{format_hours(r.get('stockpile_hours')):>12}"
            f"{format_coins(r.get('xp_hour')):>14}"
            f"{format_rate(r.get('kill_rate_per_hour')):>12}"
            f"{format_coins(r.get('combat_damage_taken_hour', r.get('thieving_damage_hour'))):>12}"
            f"{format_coins(r.get('combat_food_cost_hour', r.get('thieving_food_cost_hour'))):>12}"
            f"{format_coins(r['output_per_hour']):>14}"
            f"{format_coins(r['bid_depth']):>15}"
            f"{format_coins(r.get('active_buy_order_count')):>12}"
            f"{format_coins(r.get('active_sell_listing_count')):>14}"
            f"{format_percent(r.get('listing_saturation_pct')):>9}"
            f"{('Complete' if r.get('market_data_complete') is True else 'Partial' if r.get('market_data_complete') is False else 'Unknown'):>10}"
            f"{format_hours(r['market_coverage_hours']):>12}"
            f"{format_percent(r.get('market_share_required_pct')):>12}"
            f"{r.get('valuation_source', 'Live market'):>18}"
            f"{('∞' if r.get('stock_cycles') is None else format_coins(r['stock_cycles'])):>14}"
            f"{str(r.get('feasibility', ''))[:48]:>48}"
            f"{format_coins(r.get('input_cost_hour')):>15}"
        )

    print()

    print(
        f"Showing top {min(limit, len(results))} "
        f"of {len(results)} actions with this metric."
    )

    print()
    print(
        "Coverage = how long the currently visible buy-order "
        "depth could absorb production."
    )
    print("Active Buy Vol is the sum of unfilled quantities across active buy orders; revenue is simulated from highest bid downward, then NPC fallback.")
    print("Satur. = active sell listings / (active sell listings + active buy orders), by order count; it is a listing-pressure indicator, not unit volume.")
    print("Book shows whether all pages of sell listings and buy orders were captured in the snapshot.")
    print(
        "Mkt Share = theoretical 24-hour output divided by average daily 7-day "
        "volume; above 100% means full production exceeds observed volume."
    )
    print(
        "Wiki price gap means the Wiki output price differs from the saved "
        "lowest live ask by at least 5x; verify before acting."
    )
    print(
        "Inputs GP/hr is the estimated ingredient cost per hour, using the same "
        "price and volume assumptions as Net Gold/hr; it is deducted from Net Gold/hr. "
        "Feasibility shows missing stock."
    )
    print("Stock cycles and Feasibility count materials from both character inventory and bank.")
    print("Stockpile GP/hr excludes ingredient replacement costs while current input stock lasts; Stockpile h is the estimated run time before replenishing materials.")
    print(
        "Thieving GP range: low assumes listed item drops require success; "
        "high assumes drops may occur on any attempt. Unpriced drops are excluded; "
        "stun duration is reduced by action speed. Expected damage uses mutually "
        "exclusive stun/damage chances and does not include Iron Hide mitigation."
    )
    if any(row.get("skill") == "thieving" and row.get("thieving_food_cost_hour") is not None for row in results):
        print("Thieving gold estimates subtract the estimated food replacement cost per HP to offset expected damage.")
    elif any(row.get("skill") == "thieving" and row.get("thieving_damage_hour") for row in results):
        print("Thieving food prices were unavailable, so food cost is not included in Gold/hr.")
    print(
        "For Thieving, Yield/hr is coins earned per hour; XP/hr is experience earned per hour."
    )


def print_unverified_wiki_estimates(results):
    flagged = sorted(
        (
            row for row in results
            if row.get("gold_estimate_suppressed")
        ),
        key=lambda row: row.get("unverified_wiki_profit_hour") or 0,
        reverse=True,
    )
    if not flagged:
        return

    print()
    print("WIKI GOLD ESTIMATES WITH PRICE CONFLICTS — NOT RANKED AS ACTIONABLE")
    print("These outputs have no buy orders and Wiki prices at least 5x from current asks.")
    print(f"{'Skill':<14}{'Action':<28}{'Wiki GP/hr':>16}{'Wiki/ask gap':>16}{'Mkt Share':>14}")
    for row in flagged[:12]:
        gap = row.get("price_gap_ratio")
        gap_text = f"{gap:.1f}x" if gap else "-"
        print(
            f"{str(row['skill']):<14}"
            f"{str(row['label'])[:27]:<28}"
            f"{format_coins(row.get('unverified_wiki_profit_hour')):>16}"
            f"{gap_text:>16}"
            f"{format_percent(row.get('market_share_required_pct')):>14}"
        )
    if len(flagged) > 12:
        print(f"...and {len(flagged) - 12} more flagged estimates.")


REPORT_FIELDS = (
    "skill", "key", "label", "category", "min_level",
    "profit_hour", "profit_hour_high", "xp_hour", "output_per_hour",
    "bid_depth", "active_buy_order_count", "active_sell_listing_count",
    "listed_sell_volume", "listing_saturation_pct", "market_data_complete",
    "market_coverage_hours", "market_share_required_pct",
    "valuation_source", "stock_cycles", "stockpile_hours", "stockpile_profit_hour",
    "stockpile_profit_hour_high", "feasibility", "missing_materials",
    "missing_material_cost", "input_cost_hour", "thieving_damage_hour", "thieving_food_cost_hour",
    "thieving_food_basis", "unpriced_thieving_drops", "price_gap_ratio",
    "gold_estimate_suppressed", "unverified_wiki_profit_hour",
    "kill_rate_per_hour", "combat_style", "combat_mastery_level",
    "combat_respawn_seconds", "combat_kill_cycle_seconds",
    "combat_mastery_output_mult", "combat_mastery_rare_drop_mult",
    "combat_mastery_xp_multiplier", "combat_xp_bonus_pct",
    "combat_xp_multiplier", "combat_xp_sources",
    "combat_xp_attack_hour", "combat_xp_strength_hour",
    "combat_xp_defence_hour", "combat_xp_focused_hour",
    "combat_xp_style_hour", "combat_xp_hitpoints_hour",
    "combat_xp_damage_hour", "combat_xp_slayer_hour",
    "combat_augment_bulwark_def", "combat_augment_ward_pct",
    "combat_augment_damage_pct", "combat_augment_dot_expected_per_attack",
    "combat_dot_cap_source",
    "combat_augment_freeze_chance_pct", "combat_augment_special_key",
    "combat_augment_special_multiplier", "combat_augment_execute_finisher_multiplier",
    "combat_augment_antivenom_pct",
    "combat_augment_staunch_pct", "combat_augment_frost_ward_pct",
    "combat_augment_vitality_hp",
    "combat_modeled_max_hit", "combat_modeled_hit_chance",
    "combat_modeled_kill_seconds", "combat_potion_attack_bonus",
    "combat_bestiary_damage_pct", "combat_context_warning",
    "combat_drop_data_source", "combat_drop_bonuses", "estimated_damage_per_kill",
    "combat_damage_taken_hour", "combat_food_cost_hour", "combat_assumptions",
    "combat_food_basis", "unpriced_combat_drops",
)


def build_report(results, food_cost_per_hp, special_rankings=None):
    """Return the stable, presentation-independent report consumed by UIs."""
    def ranked(metric):
        rows = [row for row in results if row.get(metric) is not None]
        rows.sort(key=lambda row: row[metric], reverse=True)
        return [
            {field: row.get(field) for field in REPORT_FIELDS if field in row}
            for row in rows
        ]

    conflicts = [
        {field: row.get(field) for field in REPORT_FIELDS if field in row}
        for row in results
        if row.get("gold_estimate_suppressed")
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "character_id": CHARACTER_ID,
        "market_snapshot_age_hours": MARKET_SNAPSHOT_AGE_HOURS,
        "thieving_food_cost_basis": food_cost_per_hp,
        "combat_food_cost_basis": food_cost_per_hp,
        "rankings": {
            "gold_per_hour": ranked("profit_hour"),
            "xp_per_hour": ranked("xp_hour"),
        },
        "special_rankings": special_rankings or {},
        "price_conflicts": conflicts,
    }


def build_special_rankings(skill_data, wiki_items, transmute_data=None):
    """Rank food and crafted outputs using live action timings and recipe chains."""
    levels = {skill: float(data.get("skill_level") or 1) for skill, data in skill_data.items()}
    actions, all_actions = {}, {}
    for skill, data in skill_data.items():
        for action in data.get("actions", []) or []:
            all_actions[(skill, action.get("key"))] = (skill, action)
            if float(action.get("min_level") or 0) <= levels.get(skill, 1):
                actions[(skill, action.get("key"))] = (skill, action)

    def rate(action):
        calc = action.get("calc") or {}
        duration = float(calc.get("duration_ms") or action.get("duration_ms") or 0) / 1000
        amount = float(calc.get("base_amount") or action.get("amount") or 1)
        # Yield chance is the probability of an extra output; it is not success chance.
        yield_mult = 1 + float(calc.get("yield_chance") or 0) / 100
        return duration, amount * yield_mult

    def full_action_seconds(skill, action, seen=None, include_locked=False, gates=None):
        seen = set(seen or ())
        gates = gates if gates is not None else set()
        identity = (skill, action.get("key"))
        if identity in seen:
            return None
        min_level = float(action.get("min_level") or 0)
        if min_level > levels.get(skill, 1):
            if not include_locked:
                return None
            gates.add(f"{skill.title()} Lv {min_level:g}")
        seen.add(identity)
        duration, output = rate(action)
        if duration <= 0 or output <= 0:
            return None
        total = duration / output
        for ingredient in action.get("ingredients") or []:
            source_actions = all_actions if include_locked else actions
            src = source_actions.get((ingredient.get("src_skill"), ingredient.get("src_action")))
            if src:
                source_time = full_action_seconds(src[0], src[1], seen.copy(), include_locked, gates)
            else:
                source_time = gather_seconds(ingredient.get("item_key"), seen.copy(), include_locked, gates)
            if source_time is None:
                return None
            total += source_time * float(ingredient.get("quantity") or 1) / output
        return total

    def gather_seconds(item_key, seen=None, include_locked=False, gates=None):
        gates = gates if gates is not None else set()
        producer_actions = all_actions if include_locked else actions
        producers = [(s, a) for (s, _), (s, a) in producer_actions.items()
                     if a.get("reward_item_key") == item_key]
        costs = []
        for skill, action in producers:
            starting_gates = set(gates)
            candidate_gates = set(starting_gates)
            seconds = full_action_seconds(skill, action, set(seen or ()), include_locked, candidate_gates)
            if seconds is not None:
                costs.append((seconds, candidate_gates, candidate_gates - starting_gates))
        # Many gathering actions produce seeds as rare drops instead of their
        # primary reward. Include the expected cycles needed for one seed.
        for skill, action in producer_actions.values():
            chance = sum(float(drop.get("chance") or 0)
                         for drop in action.get("seed_drops") or []
                         if drop.get("item_key") == item_key)
            if chance <= 0:
                continue
            starting_gates = set(gates)
            candidate_gates = set(starting_gates)
            cycle_seconds = full_action_seconds(
                skill, action, set(seen or ()), include_locked, candidate_gates
            )
            if cycle_seconds is not None:
                costs.append((cycle_seconds / chance, candidate_gates,
                              candidate_gates - starting_gates))
        if not costs:
            return None
        best_seconds, best_gates, _ = min(costs, key=lambda value: (bool(value[2]), value[0]))
        gates.update(best_gates)
        return best_seconds

    fish_rows = []
    fishing = {a.get("reward_item_key"): a for a in skill_data.get("fishing", {}).get("actions", []) or []
               if float(a.get("min_level") or 0) <= levels.get("fishing", 1)}
    cooking_data = skill_data.get("cooking", {})
    cook_level = float(cooking_data.get("skill_level") or 1)
    for cook in cooking_data.get("actions", []) or []:
        key = cook.get("reward_item_key") or ""
        stats = cook.get("reward_stats") or {}
        if (not key.startswith("cooked_") or not stats.get("stat_hp")
                or float(cook.get("min_level") or 0) > cook_level):
            continue
        raw = next((i.get("item_key") for i in cook.get("ingredients", []) or []), None)
        fish = fishing.get(raw)
        if not fish:
            continue
        duration, _ = rate(cook)
        fish_duration, fish_output = rate(fish)
        start = float(cook.get("burn_chance_start") or 0)
        stop = float(cook.get("burn_stop_level") or 0)
        min_level = float(cook.get("min_level") or 0)
        # Burn chance falls linearly from its action-specific starting chance
        # at unlock to zero at burn_stop_level.
        burn_pct = start * max(0, min(1, (stop - cook_level) / max(stop - min_level, 1)))
        success = max(0.01, 1 - burn_pct / 100)
        total_seconds = fish_duration / max(fish_output, 0.01) + duration / success
        item = wiki_items.get(key) or {}
        fish_rows.append({"label": cook.get("reward_label") or cook.get("label") or key,
                          "skill": "cooking", "healing_hp": float(stats["stat_hp"]),
                          "burn_chance_pct": burn_pct, "catch_seconds": fish_duration / max(fish_output, 0.01),
                          "cook_seconds": duration, "seconds_per_edible": total_seconds,
                          "healing_per_hour": float(stats["stat_hp"]) * 3600 / total_seconds,
                          "value_hour": float(item.get("market_price") or item.get("npc_sell_price") or 0) * 3600 / total_seconds})
    fish_rows.sort(key=lambda r: r["healing_per_hour"], reverse=True)

    production_rows = []
    for skill, data in skill_data.items():
        for action in data.get("actions", []) or []:
            ingredients = action.get("ingredients") or []
            output_key = action.get("reward_item_key")
            if (not output_key or not ingredients
                    or float(action.get("min_level") or 0) > levels.get(skill, 1)):
                continue
            duration, output_qty = rate(action)
            if duration <= 0 or output_qty <= 0:
                continue
            gather = 0.0
            complete = True
            steps = []
            for ing in ingredients:
                src_skill, src_action_key = ing.get("src_skill"), ing.get("src_action")
                source = actions.get((src_skill, src_action_key)) if src_skill and src_action_key else None
                ingredient_time = None
                if source:
                    ingredient_time = full_action_seconds(source[0], source[1])
                else:
                    ingredient_time = gather_seconds(ing.get("item_key"))
                if ingredient_time is None:
                    complete = False
                else:
                    gather += ingredient_time * float(ing.get("quantity") or 1)
                steps.append(f"{ing.get('item_key')}: {'known' if ingredient_time is not None else 'unknown'}")
            total = duration / output_qty + gather / output_qty if complete else None
            item = wiki_items.get(output_key) or {}
            price = float(item.get("market_price") or item.get("npc_sell_price") or 0)
            row = {"label": action.get("reward_label") or action.get("label") or output_key,
                   "skill": skill, "output_key": output_key, "gather_seconds": gather / output_qty if complete else None,
                   "process_seconds": duration / output_qty, "total_seconds": total,
                   "gold_per_hour": price * 3600 / total if total and price else None,
                   "materials": ", ".join(steps), "gold_value": price}
            production_rows.append(row)
    production_rows.sort(key=lambda r: r.get("gold_per_hour") or 0, reverse=True)

    # The game has a dedicated transmute endpoint. Its gold_output already
    # includes the character's Magic-level bonus; Golden Touch is applied at
    # payout and effective_duration_ms includes active speed/mastery effects.
    transmute_rows = []
    if isinstance(transmute_data, dict):
        cast_seconds = float(transmute_data.get("effective_duration_ms")
                             or transmute_data.get("duration_ms") or 0) / 1000
        magic_gold_bonus = float(transmute_data.get("gold_bonus") or 0)
        golden_touch_bonus = float(transmute_data.get("gold_gain_bonus") or 0)
        transmute_items = {item.get("item_key"): item for item in transmute_data.get("items", []) or []
                           if item.get("item_key")}
        # The live list can be limited to items currently present in inventory
        # or bank. Add every currently unlocked recipe output as a candidate,
        # using its NPC sell value and the live character bonuses.
        for (skill, _), (_, action) in actions.items():
            key = action.get("reward_item_key")
            if not key or not action.get("ingredients") or key in transmute_items:
                continue
            item_info = wiki_items.get(key) or {}
            npc_price = item_info.get("npc_sell_price")
            if npc_price is None:
                continue
            npc_price = float(npc_price)
            transmute_items[key] = {
                "item_key": key,
                "name": action.get("reward_label") or action.get("label") or item_info.get("name") or key,
                "sell_price": npc_price,
                "gold_output": round(npc_price * (1 + magic_gold_bonus / 100)),
                "_recipe_candidate": True,
            }
        for item in transmute_items.values():
            item_key = item.get("item_key")
            base_output = float(item.get("gold_output") or 0)
            final_output = (math.ceil(base_output * (1 + golden_touch_bonus / 100))
                            if golden_touch_bonus > 0 else base_output)
            if not item_key or final_output <= 0 or cast_seconds <= 0:
                continue
            rune_inferred = bool(item.get("_rune_inferred"))
            rune_key = item.get("rune_key")
            rune_amount = float(item.get("rune_amount") or 0) if rune_key else 0
            if rune_key and rune_amount <= 0:
                rune_amount = 1
            elif not rune_key:
                # Wiki table: rune tier follows shop/NPC value, not market price.
                item_info = wiki_items.get(item_key) or {}
                shop_value = item.get("sell_price")
                if shop_value is None:
                    shop_value = item_info.get("npc_sell_price")
                try:
                    shop_value = float(shop_value)
                except (TypeError, ValueError):
                    shop_value = 0
                if shop_value > 0:
                    if shop_value <= 50:
                        rune_key = "aetherite_rune"
                    elif shop_value <= 200:
                        rune_key = "voidstone_rune"
                    elif shop_value <= 1000:
                        rune_key = "fluxite_rune"
                    elif shop_value <= 5000:
                        rune_key = "prismite_rune"
                    else:
                        rune_key = "celestite_rune"
                    rune_amount = 1
                    rune_inferred = True
            gates = set()
            item_seconds = gather_seconds(item_key, include_locked=True, gates=gates)
            rune_seconds_each = gather_seconds(rune_key, include_locked=True, gates=gates) if rune_key and rune_amount else 0
            rune_known = bool(rune_key and rune_amount)
            complete_cost = (item_seconds is not None and rune_known
                             and (not rune_key or not rune_amount or rune_seconds_each is not None))
            known_rune_cost = bool(rune_key and rune_amount and rune_seconds_each is not None)
            known_acquisition_seconds = None
            if item_seconds is not None or known_rune_cost:
                known_acquisition_seconds = ((item_seconds or 0)
                                             + ((rune_seconds_each or 0) * rune_amount
                                                if known_rune_cost else 0))
            cycle_seconds = cast_seconds + known_acquisition_seconds if complete_cost else None
            requirement = f"{item_key} ×1"
            unresolved_inputs = []
            if item_seconds is None:
                unresolved_inputs.append(item_key)
            if rune_key and rune_amount:
                requirement += f" + {rune_key} ×{rune_amount:g}"
                if rune_inferred:
                    requirement += " (Wiki shop-value bracket)"
                if rune_seconds_each is None:
                    unresolved_inputs.append(rune_key)
            else:
                requirement += " + rune requirement unknown"
                unresolved_inputs.append("rune requirement")
            transmute_rows.append({
                "label": f"Transmute {item.get('name') or item_key}",
                "skill": "alchemy", "output_key": item_key,
                "gold_per_cast": final_output,
                "npc_price": item.get("sell_price"),
                "magic_level_bonus_pct": magic_gold_bonus,
                "golden_touch_bonus_pct": golden_touch_bonus,
                "gather_seconds": known_acquisition_seconds if complete_cost else None,
                "known_acquisition_seconds": known_acquisition_seconds,
                "item_acquisition_seconds": item_seconds,
                "rune_acquisition_seconds": rune_seconds_each * rune_amount if known_rune_cost else None,
                "rune_requirement_source": "Wiki shop-value bracket" if rune_inferred else ("Live transmute data" if rune_key and rune_amount else "Unknown"),
                "unresolved_inputs": unresolved_inputs,
                "cast_seconds": cast_seconds,
                "total_seconds": cycle_seconds,
                "gold_per_hour": final_output * 3600 / cycle_seconds if cycle_seconds else None,
                "materials": requirement,
                "unlock_requirements": sorted(gates),
            })
    transmute_rows.sort(key=lambda r: r.get("gold_per_hour") or 0, reverse=True)
    return {"cooked_fish": fish_rows, "production_value": production_rows, "transmuting": transmute_rows}


def write_json_report(report, filename):
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        json.dump(report, output, indent=2, ensure_ascii=False)
        output.write("\n")


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Koruxa gold and XP optimizer")
    parser.add_argument(
        "--refresh-market",
        action="store_true",
        help="refresh inventory, bank, and item listings before ranking",
    )
    parser.add_argument(
        "--json-output",
        metavar="PATH",
        help="save the complete ranked results as JSON for a GUI or other client",
    )
    parser.add_argument(
        "--credentials-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    print("=" * 70)
    print("KORUXA PROFIT OPTIMIZER")
    print("=" * 70)

    if args.credentials_stdin:
        try:
            credentials = json.load(sys.stdin)
            login(
                credentials.get("username"),
                credentials.get("password"),
                credentials.get("character_id"),
            )
        except (ValueError, AttributeError) as exc:
            raise SystemExit(f"Could not read login details: {exc}")
    else:
        login()

    print()
    print("Fetching fresh game data...")
    print()

    player = load_player()

    print("Fetching current monsters and equipment for combat estimates...")
    live_combat_data = api_get("monsters") or {}
    combat_equipment = api_get("equipment") or {}
    combat_mastery_data = api_get("mastery/list") or {}
    combat_status_data = api_get("combat/status") or {}
    combat_stats_data = api_get("combat/stats-breakdown") or {}
    combat_active_buffs = api_get("buffs") or {}
    combat_research_data = api_get("research/list") or {}
    combat_clan_research_data = api_get("clan-research/list") or {}
    combat_wiki_monsters = load_wiki_monsters()
    combat_live_drop_data = load_live_monster_drops(
        combat_wiki_monsters, live_combat_data, player
    )

    skill_data = load_all_skills()
    farming_data = load_farming_actions(player, skill_data)
    if farming_data.get("actions"):
        skill_data["farming"] = farming_data
    transmute_data = api_get("alchemy/transmute-items") or {}

    market = refresh_market_snapshot() if args.refresh_market else load_market()
    wiki_profit = load_wiki_profit_index()
    if not wiki_profit:
        print(f"Wiki profit data not found or empty: {WIKI_PROFIT_FILE}")

    print()
    print(
        f"Loaded {len(skill_data)} skills."
    )

    actions = load_actions(
        skill_data,
        player,
    )

    print(
        f"Loaded {len(actions)} available actions."
    )

    print()
    print("Calculating profitability...")

    results = []
    wiki_fallback_count = 0
    wiki_items = load_wiki_items()
    food_cost_per_hp = estimate_food_cost_per_hp(skill_data, market, wiki_items)
    if food_cost_per_hp:
        print(
            "Thieving food cost basis: "
            f"{food_cost_per_hp['name']} ({format_coins(food_cost_per_hp['price'])} GP / "
            f"{format_coins(food_cost_per_hp['heal'])} HP; {food_cost_per_hp['price_source']})."
        )
    else:
        print("No priced healing food found; Thieving food costs cannot be estimated.")

    for action in actions:

        result = calculate_action(
            action,
            market,
            wiki_items,
            food_cost_per_hp,
        )

        candidate = None
        if has_live_listing_coverage(action, market):
            if result["profit_hour"] is not None or result.get("xp_hour") is not None:
                result["valuation_source"] = (
                    (
                        "Stale market"
                        if MARKET_SNAPSHOT_AGE_HOURS is not None
                        and MARKET_SNAPSHOT_AGE_HOURS > 24
                        else "Live market"
                    )
                    if result["profit_hour"] is not None
                    else "Unpriced"
                )
                candidate = result
        else:
            fallback = wiki_fallback_result(action, wiki_profit, market)
            if fallback is not None:
                candidate = fallback
                wiki_fallback_count += 1
            elif result["profit_hour"] is not None or result.get("xp_hour") is not None:
                if result.get("direct_coin_reward"):
                    result["valuation_source"] = (
                        "Thieving range"
                        if result.get("thieving_loot_gp_hour_high", 0) > 0
                        else "Thieving coins"
                    )
                else:
                    result["valuation_source"] = (
                        "NPC/partial" if result["profit_hour"] is not None else "Unpriced"
                    )
                candidate = result

        if candidate is not None:
            results.append(add_feasibility(candidate, action, market, wiki_items))

    combat_results = build_combat_results(
        combat_wiki_monsters,
        live_combat_data,
        combat_equipment,
        player,
        market,
        wiki_items,
        food_cost_per_hp,
        combat_mastery_data,
        combat_live_drop_data,
        combat_status_data,
        combat_active_buffs,
        research_data=combat_research_data,
        clan_research_data=combat_clan_research_data,
        combat_stats_data=combat_stats_data,
    )
    results.extend(combat_results)
    print(f"Added {len(combat_results)} combat estimates using live personal drops with Wiki fallback.")

    print(
        f"Loaded {len(results)} skill and combat tasks for gold/XP ranking."
    )
    print(
        f"Using Wiki fallback estimates for {wiki_fallback_count} actions "
        "with incomplete live listings."
    )

    if args.json_output:
        write_json_report(build_report(results, food_cost_per_hp,
                                       build_special_rankings(skill_data, wiki_items, transmute_data)), args.json_output)
        print(f"Structured results saved to {args.json_output}")

    print_results(
        results,
        sort_key="profit_hour",
        title="TOP TASKS BY NET GOLD/HOUR",
    )
    print_unverified_wiki_estimates(results)
    print_results(
        results,
        sort_key="xp_hour",
        title="TOP TASKS BY XP/HOUR",
    )


if __name__ == "__main__":
    main()
