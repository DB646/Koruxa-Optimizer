import json
import re
from pathlib import Path


# ============================================================
# Paths
# ============================================================

INPUT_PATH = Path("data/wiki/activities.json")
OUTPUT_PATH = Path("data/wiki/activities_normalized.json")


# ============================================================
# General text cleanup
# ============================================================

def clean_mojibake(value):
    """
    Attempt to repair common UTF-8 -> Windows-1252/Latin-1
    mojibake such as:

        Ã—
        â€”
        â˜…
        âœ¨

    Repeatedly attempts repair because some source strings may
    have been mangled more than once.
    """

    if not isinstance(value, str):
        return value

    replacements = {
        "Ã—": "×",
        "Ã·": "÷",
        "â€”": "—",
        "â€“": "–",
        "â€˜": "‘",
        "â€™": "’",
        "â€œ": "“",
        "â€": "”",
        "â€¦": "…",
        "â˜…": "⭐",
        "â˜†": "☆",
        "âœ¨": "✨",
        "â˜€ï¸": "☀️",
        "â˜€": "☀️",
        "ðŸŒ™": "🌙",
        "ðŸŒŸ": "🌟",
    }

    for _ in range(3):
        original = value

        for bad, good in replacements.items():
            value = value.replace(bad, good)

        # Generic repair attempt.
        if any(x in value for x in ("Ã", "Â", "â", "ð")):
            try:
                repaired = value.encode("latin1").decode("utf-8")
                value = repaired
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

        if value == original:
            break

    return value


def clean_text(value):
    if value is None:
        return ""

    value = str(value)
    value = clean_mojibake(value)

    # Normalize unusual whitespace.
    value = re.sub(r"\s+", " ", value)

    return value.strip()


# ============================================================
# Time parsing
# ============================================================

def parse_time(value):
    """
    Parse durations such as:

        12s
        2m
        1h
        27h 46m
        1h 30m
        45 seconds
        5 minutes

    Returns seconds or None.
    """

    if value is None:
        return None

    value = clean_text(value)

    if not value:
        return None

    # Explicitly reject em dash / missing-time markers.
    if value in {"—", "-", "–", "N/A", "n/a"}:
        return None

    hours = 0
    minutes = 0
    seconds = 0

    found = False

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*h(?:ours?|r)?\b",
        value,
        re.IGNORECASE,
    )

    if match:
        hours = float(match.group(1))
        found = True

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?\b",
        value,
        re.IGNORECASE,
    )

    if match:
        minutes = float(match.group(1))
        found = True

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b",
        value,
        re.IGNORECASE,
    )

    if match:
        seconds = float(match.group(1))
        found = True

    if not found:
        return None

    total = hours * 3600 + minutes * 60 + seconds

    return int(total) if total.is_integer() else total


# ============================================================
# Quantity parsing
# ============================================================

def parse_quantity_text(value):
    """
    Parse:

        1x Basic Log
        5 x Iron Ore
        1 Basic Log
        Basic Log

    Returns:

        quantity, item_name
    """

    value = clean_text(value)

    if not value:
        return None, None

    match = re.match(
        r"^\s*(\d+(?:\.\d+)?)\s*[x×]\s*(.+?)\s*$",
        value,
        re.IGNORECASE,
    )

    if match:
        quantity = float(match.group(1))
        item = match.group(2).strip()

    else:
        match = re.match(
            r"^\s*(\d+(?:\.\d+)?)\s+(.+?)\s*$",
            value,
            re.IGNORECASE,
        )

        if match:
            quantity = float(match.group(1))
            item = match.group(2).strip()
        else:
            quantity = 1
            item = value

    if quantity.is_integer():
        quantity = int(quantity)

    return quantity, item


# ============================================================
# Item name cleanup
# ============================================================

def clean_item_name(value):
    value = clean_text(value)

    if not value:
        return ""

    # Remove mechanic annotations if they somehow remain attached
    # to an item name.
    value = re.sub(
        r"\s+Burn:\s*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+Buff:\s*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+Failure:\s*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+Fail(?:ure)?:\s*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    return value.strip(" ,;·•")


# ============================================================
# Mechanic extraction
# ============================================================

MECHANIC_PATTERNS = {
    "burn": re.compile(
        r"\bBurn:\s*(.+?)(?="
        r"\s+(?:Buff|Failure|Fail):"
        r"|$)",
        re.IGNORECASE,
    ),

    "buff": re.compile(
        r"\bBuff:\s*(.+?)(?="
        r"\s+(?:Burn|Failure|Fail):"
        r"|$)",
        re.IGNORECASE,
    ),

    "failure": re.compile(
        r"\b(?:Failure|Fail):\s*(.+?)(?="
        r"\s+(?:Burn|Buff):"
        r"|$)",
        re.IGNORECASE,
    ),
}


def extract_mechanic_text(value):
    """
    Extract known mechanic annotations from a string.

    Returns:

        {
            "burn": [...],
            "buff": [...],
            "failure": [...]
        }

    The raw text is preserved exactly enough for later auditing.
    """

    value = clean_text(value)

    mechanics = {
        "burn": [],
        "buff": [],
        "failure": [],
    }

    if not value:
        return mechanics

    for mechanic_type, pattern in MECHANIC_PATTERNS.items():
        for match in pattern.finditer(value):
            text = clean_text(match.group(1))

            if text:
                mechanics[mechanic_type].append(text)

    return mechanics


def parse_burn_mechanic(raw_text):
    """
    Convert:

        20% at L1, 0% at L10

    into:

        [
            {"level": 1, "chance": 0.20},
            {"level": 10, "chance": 0.00}
        ]

    We deliberately support arbitrary level points rather than
    assuming the relationship between levels is linear.
    """

    if not raw_text:
        return None

    entries = []

    pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*%\s*at\s*L(?:evel\s*)?(\d+)",
        re.IGNORECASE,
    )

    for match in pattern.finditer(raw_text):
        chance = float(match.group(1)) / 100.0
        level = int(match.group(2))

        entries.append({
            "level": level,
            "chance": chance,
        })

    if not entries:
        return {
            "type": "unknown",
            "raw": raw_text,
        }

    entries.sort(key=lambda x: x["level"])

    return {
        "type": "level_scaled",
        "chance_by_level": entries,
    }


def parse_buff_mechanic(raw_text):
    """
    We do NOT attempt to fully interpret potion buffs yet.

    The economic engine does not currently need to know what a
    potion does to calculate manufacturing cost/value.

    Instead, preserve the raw text and expose it structurally.
    """

    if not raw_text:
        return None

    return {
        "type": "consumable_buff",
        "raw": raw_text,
    }


def parse_failure_mechanic(raw_text):
    """
    Failure mechanics are intentionally only partially parsed.

    We know that failure may produce multiple consequences such
    as:

        stun for X seconds
        damage
        etc.

    Rather than guessing at semantics, preserve the complete raw
    description and attempt to identify common effects.
    """

    if not raw_text:
        return None

    result = {
        "type": "failure",
        "raw": raw_text,
        "effects": [],
    }

    # --------------------------------------------------------
    # Stun
    # --------------------------------------------------------

    stun_pattern = re.compile(
        r"(?:stun(?:ned)?|stun\s+for)\s*"
        r"(?:(?:for)\s*)?"
        r"(\d+(?:\.\d+)?)\s*"
        r"(seconds?|secs?|s)\b",
        re.IGNORECASE,
    )

    for match in stun_pattern.finditer(raw_text):
        result["effects"].append({
            "type": "stun",
            "duration_seconds": float(match.group(1)),
        })

    # --------------------------------------------------------
    # Damage
    # --------------------------------------------------------

    damage_patterns = [
        re.compile(
            r"(?:take|takes|deal|deals|receive|receives)"
            r"\s*(\d+(?:\.\d+)?)\s*damage",
            re.IGNORECASE,
        ),
        re.compile(
            r"(\d+(?:\.\d+)?)\s*damage",
            re.IGNORECASE,
        ),
    ]

    damage_found = set()

    for pattern in damage_patterns:
        for match in pattern.finditer(raw_text):
            amount = float(match.group(1))

            if amount not in damage_found:
                damage_found.add(amount)

                result["effects"].append({
                    "type": "damage",
                    "amount": amount,
                })

    # --------------------------------------------------------
    # Keep this intentionally open-ended.
    #
    # Any effects we don't understand remain in raw.
    # --------------------------------------------------------

    return result


def build_mechanics(raw_strings):
    """
    Combine mechanics found across all relevant source fields.
    """

    combined = {
        "burn": [],
        "buff": [],
        "failure": [],
    }

    for value in raw_strings:
        extracted = extract_mechanic_text(value)

        for mechanic_type in combined:
            combined[mechanic_type].extend(
                extracted[mechanic_type]
            )

    # Remove duplicates while preserving order.
    for mechanic_type in combined:
        combined[mechanic_type] = list(
            dict.fromkeys(combined[mechanic_type])
        )

    mechanics = {}

    # --------------------------------------------------------
    # Burn
    # --------------------------------------------------------

    if combined["burn"]:
        parsed_burn = []

        for raw in combined["burn"]:
            parsed = parse_burn_mechanic(raw)

            if parsed:
                parsed_burn.append(parsed)

        mechanics["burn"] = {
            "raw": combined["burn"],
            "parsed": parsed_burn,
        }

    # --------------------------------------------------------
    # Buff
    # --------------------------------------------------------

    if combined["buff"]:
        mechanics["buff"] = {
            "raw": combined["buff"],
            "parsed": [
                parse_buff_mechanic(x)
                for x in combined["buff"]
            ],
        }

    # --------------------------------------------------------
    # Failure
    # --------------------------------------------------------

    if combined["failure"]:
        mechanics["failure"] = {
            "raw": combined["failure"],
            "parsed": [
                parse_failure_mechanic(x)
                for x in combined["failure"]
            ],
        }

    return mechanics


# ============================================================
# Remove mechanics from item strings
# ============================================================

def remove_mechanic_annotations(value):
    """
    Remove Burn/Buff/Failure annotations from an item string
    without treating their commas as item separators.
    """

    if not value:
        return ""

    value = clean_text(value)

    # Mechanics start with one of these labels and continue to
    # the end of the current source string.
    value = re.sub(
        r"\s+(?:Burn|Buff|Failure|Fail):\s*.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    return value.strip(" ,;·•")


# ============================================================
# Item list parsing
# ============================================================

def parse_item_list(value):
    """
    Parse actual economic item entries while protecting mechanic
    annotations from comma splitting.

    Examples:

        1x Cooked Mudfish
        1x Cooked Mudfish Burn: 20% at L1, 0% at L10

        1x Combat Potion Buff: attack power +15%,
        defence power +15% for 8m

    The result contains ONLY actual items.
    """

    if value is None:
        return []

    if isinstance(value, list):
        raw_items = value

    else:
        value = clean_text(value)

        if not value:
            return []

        # ----------------------------------------------------
        # Protect known mechanic sections from comma splitting.
        # ----------------------------------------------------

        protected = value

        for label in ("Burn:", "Buff:", "Failure:", "Fail:"):

            pattern = re.compile(
                rf"{re.escape(label)}.*?(?="
                rf"\s+(?:Burn|Buff|Failure|Fail):"
                rf"|$)",
                re.IGNORECASE,
            )

            def protect(match):
                return match.group(0).replace(
                    ",",
                    "<<<COMMA>>>"
                )

            protected = pattern.sub(protect, protected)

        # ----------------------------------------------------
        # Split actual entries.
        #
        # A comma only represents a new item when followed by
        # another quantity.
        # ----------------------------------------------------

        raw_items = re.split(
            r"\s*[;·•]\s*"
            r"|"
            r"\s*,\s*(?=\d+\s*[x×]?\s*[A-Za-z])",
            protected,
            flags=re.IGNORECASE,
        )

        raw_items = [
            item.replace("<<<COMMA>>>", ",")
            for item in raw_items
        ]

    results = []

    for raw_item in raw_items:

        raw_item = clean_text(raw_item)

        if not raw_item:
            continue

        quantity, item = parse_quantity_text(raw_item)

        if item is None:
            continue

        item = remove_mechanic_annotations(item)
        item = clean_item_name(item)

        if not item:
            continue

        results.append({
            "item": item,
            "quantity": quantity,
        })

    return results


# ============================================================
# Farming parser
# ============================================================

def parse_farming_makes(value):
    """
    Parse farming strings such as:

        6 Glowroot, 3-6 Glowroot, 15m

    or source-specific variants containing growth time.

    This function is intentionally conservative. The normalized
    farming structure keeps yield and growth time separate from
    normal queued activity duration.
    """

    value = clean_text(value)

    if not value:
        return None

    # Try to identify growth time.
    growth_time_seconds = None

    time_matches = re.findall(
        r"\d+(?:\.\d+)?\s*(?:h|hours?|m|minutes?|s|seconds?)",
        value,
        flags=re.IGNORECASE,
    )

    if time_matches:
        # Usually the relevant farming time is the final time
        # expression in the source string.
        growth_time_seconds = parse_time(time_matches[-1])

    # --------------------------------------------------------
    # Yield range
    # --------------------------------------------------------

    yield_min = None
    yield_max = None

    range_match = re.search(
        r"(\d+)\s*[-–]\s*(\d+)",
        value,
    )

    if range_match:
        yield_min = int(range_match.group(1))
        yield_max = int(range_match.group(2))

    # --------------------------------------------------------
    # Identify output item.
    #
    # This is intentionally conservative. Farming records are
    # handled separately from normal outputs.
    # --------------------------------------------------------

    output_item = None

    # Remove the yield range and time from a working string.
    working = re.sub(
        r"\d+\s*[-–]\s*\d+",
        "",
        value,
    )

    working = re.sub(
        r"\d+(?:\.\d+)?\s*(?:h|hours?|m|minutes?|s|seconds?)",
        "",
        working,
        flags=re.IGNORECASE,
    )

    working = working.strip(" ,;·•")

    # The source's farming action/output name is generally the
    # meaningful item left after removing quantities.
    quantity, candidate = parse_quantity_text(working)

    if candidate:
        output_item = clean_item_name(candidate)

    return {
        "growth_time_seconds": growth_time_seconds,
        "yield_min": yield_min,
        "yield_max": yield_max,
        "output_item": output_item,
    }


# ============================================================
# Farming-specific parser
# ============================================================

def parse_farming_activity(raw):
    """
    Farming is a concurrent/background system.

    We do NOT place its growth duration into base_time_seconds.
    """

    makes = clean_text(
        raw.get("makes")
        or raw.get("output")
        or raw.get("outputs")
        or ""
    )

    action = clean_text(
        raw.get("action")
        or raw.get("name")
        or ""
    )

    # --------------------------------------------------------
    # First attempt: source string contains the relevant data.
    # --------------------------------------------------------

    parsed = parse_farming_makes(makes)

    # --------------------------------------------------------
    # Fallback: action itself may be the crop/seed name.
    # --------------------------------------------------------

    if not parsed.get("output_item"):
        parsed["output_item"] = action

    return {
        "display_quantity": None,
        "output_item": parsed.get("output_item"),
        "growth_time_seconds": parsed.get("growth_time_seconds"),
        "yield_min": parsed.get("yield_min"),
        "yield_max": parsed.get("yield_max"),
        "bonus": [],
    }


# ============================================================
# Raw-field helper
# ============================================================

def collect_source_strings(raw):
    """
    Collect every string field from an activity.

    This is important for thieving because we do not yet know
    exactly which raw field contains its failure mechanic.
    """

    values = []

    def walk(value):
        if isinstance(value, str):
            if value.strip():
                values.append(value)

        elif isinstance(value, dict):
            for child in value.values():
                walk(child)

        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(raw)

    return values


# ============================================================
# Activity normalization
# ============================================================

def normalize_activity(raw):
    skill = clean_text(
        raw.get("skill")
        or raw.get("category")
        or ""
    ).lower()

    action = clean_text(
        raw.get("action")
        or raw.get("name")
        or ""
    )

    # --------------------------------------------------------
    # Level
    # --------------------------------------------------------

    level_raw = (
        raw.get("level")
        or raw.get("required_level")
        or 0
    )

    try:
        level = int(level_raw)
    except (TypeError, ValueError):
        level_match = re.search(r"\d+", str(level_raw))

        if level_match:
            level = int(level_match.group(0))
        else:
            level = 0

    # --------------------------------------------------------
    # XP
    # --------------------------------------------------------

    xp_raw = raw.get("xp", 0)

    try:
        xp = float(xp_raw)
    except (TypeError, ValueError):
        xp = 0

    if xp.is_integer():
        xp = int(xp)

    # --------------------------------------------------------
    # Raw fields
    # --------------------------------------------------------

    makes = raw.get("makes")
    needs = raw.get("needs")

    makes_text = clean_text(makes)
    needs_text = clean_text(needs)

    source_strings = collect_source_strings(raw)

    # --------------------------------------------------------
    # Mechanics
    # --------------------------------------------------------

    mechanics = build_mechanics(source_strings)

    # --------------------------------------------------------
    # Farming
    # --------------------------------------------------------

    is_farming = skill == "farming"

    if is_farming:

        farming = parse_farming_activity(raw)

        outputs = []
        inputs = []

        activity_type = "concurrent"
        base_time_seconds = None
        wiki_xp_per_hour = None

    else:

        farming = None

        outputs = parse_item_list(makes)
        inputs = parse_item_list(needs)

        # ----------------------------------------------------
        # Time
        # ----------------------------------------------------

        time_raw = (
            raw.get("time")
            or raw.get("duration")
            or raw.get("base_time")
            or ""
        )

        base_time_seconds = parse_time(time_raw)

        activity_type = "queued"

        if base_time_seconds:
            wiki_xp_per_hour = (
                xp * 3600 / base_time_seconds
            )

        else:
            wiki_xp_per_hour = None

    # --------------------------------------------------------
    # Rare rolls
    # --------------------------------------------------------

    rare_rolls_raw = clean_text(
        raw.get("rare_rolls")
        or raw.get("rare")
        or ""
    )

    # --------------------------------------------------------
    # Preserve mechanic source information.
    #
    # This is useful while reverse-engineering the wiki.
    # --------------------------------------------------------

    mechanics_raw = {}

    for mechanic_type in ("burn", "buff", "failure"):

        values = []

        for source_string in source_strings:
            extracted = extract_mechanic_text(source_string)

            values.extend(
                extracted.get(mechanic_type, [])
            )

        # Deduplicate while preserving order.
        values = list(dict.fromkeys(values))

        if values:
            mechanics_raw[mechanic_type] = values

    # --------------------------------------------------------
    # Final normalized record
    # --------------------------------------------------------

    return {
        "skill": skill,
        "activity_type": activity_type,
        "level": level,
        "action": action,
        "xp": xp,
        "base_time_seconds": base_time_seconds,
        "wiki_xp_per_hour": wiki_xp_per_hour,
        "outputs": outputs,
        "inputs": inputs,
        "mechanics": mechanics,
        "mechanics_raw": mechanics_raw,
        "farming": farming,
        "rare_rolls_raw": rare_rolls_raw,
    }


# ============================================================
# Main
# ============================================================

def main():

    print(f"Reading: {INPUT_PATH}")

    with INPUT_PATH.open(
        "r",
        encoding="utf-8",
    ) as f:
        raw_data = json.load(f)

    if isinstance(raw_data, dict):

        # Support either:
        # {"activities": [...]}
        # or a directly keyed activity collection.

        if isinstance(raw_data.get("activities"), list):
            activities = raw_data["activities"]

        else:
            activities = list(raw_data.values())

    elif isinstance(raw_data, list):
        activities = raw_data

    else:
        raise ValueError(
            "Unsupported activities.json structure."
        )

    normalized = []

    for raw in activities:
        if not isinstance(raw, dict):
            continue

        normalized.append(
            normalize_activity(raw)
        )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT_PATH.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            normalized,
            f,
            indent=2,
            ensure_ascii=False,
        )

    # ========================================================
    # Summary
    # ========================================================

    queued = [
        x for x in normalized
        if x["activity_type"] == "queued"
    ]

    concurrent = [
        x for x in normalized
        if x["activity_type"] == "concurrent"
    ]

    cooking_burn = [
        x for x in normalized
        if "burn" in x["mechanics"]
    ]

    herblore_buff = [
        x for x in normalized
        if "buff" in x["mechanics"]
    ]

    thieving = [
        x for x in normalized
        if x["skill"] == "thieving"
    ]

    thieving_failure = [
        x for x in thieving
        if "failure" in x["mechanics"]
    ]

    print()
    print("=" * 60)
    print("NORMALIZATION COMPLETE")
    print("=" * 60)

    print(f"Total activities:        {len(normalized)}")
    print(f"Queued activities:      {len(queued)}")
    print(f"Concurrent activities:  {len(concurrent)}")

    print()
    print("Mechanics:")
    print(f"Cooking burn:            {len(cooking_burn)}")
    print(f"Herblore buffs:          {len(herblore_buff)}")
    print(f"Thieving activities:     {len(thieving)}")
    print(f"Thieving failure data:   {len(thieving_failure)}")

    print()
    print(f"Output written to:       {OUTPUT_PATH}")


if __name__ == "__main__":
    main()