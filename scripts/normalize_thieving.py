import json
from pathlib import Path


# ============================================================
# Paths
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

INPUT_FILE = ROOT / "skill_thieving.json"
OUTPUT_FILE = ROOT / "data" / "wiki" / "thieving_normalized.json"


# ============================================================
# Helpers
# ============================================================

def ms_to_seconds(value):
    """Convert milliseconds to seconds."""
    if value is None:
        return None

    try:
        return value / 1000
    except (TypeError, ValueError):
        return None


def normalize_chance(value):
    """
    Thieving source data stores chances as decimals.

    Example:
        0.15   -> 0.15
        1e-05  -> 0.00001

    Keep the source representation rather than converting
    to percentages so the economic model can use it directly.
    """
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_quantity(value):
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_drop(drop):
    """
    Normalize a standard loot drop while preserving
    cross-skill requirements.
    """

    result = {
        "item_key": drop.get("item_key"),
        "quantity_min": normalize_quantity(drop.get("quantity_min")),
        "quantity_max": normalize_quantity(drop.get("quantity_max")),
        "drop_chance": normalize_chance(drop.get("drop_chance")),
    }

    # Preserve requirement information only when present.
    requirements = {}

    if drop.get("locked") is not None:
        requirements["locked"] = bool(drop.get("locked"))

    if drop.get("lock_skill") is not None:
        requirements["skill"] = drop.get("lock_skill")

    if drop.get("lock_level") is not None:
        requirements["level"] = drop.get("lock_level")

    if requirements:
        result["requirements"] = requirements
    else:
        result["requirements"] = None

    return result


def normalize_rare_drop(drop):
    """
    Rare/seed drops use a different schema in skill_thieving.json.
    """
    return {
        "item_key": drop.get("item_key"),
        "chance": normalize_chance(drop.get("chance")),
    }


def normalize_coin_reward(action):
    """
    Coins are a variable reward independent of the item loot
    structure, so keep them explicitly separated.
    """

    coins_min = action.get("coins_min")
    coins_max = action.get("coins_max")

    if coins_min is None and coins_max is None:
        return None

    return {
        "min": normalize_quantity(coins_min),
        "max": normalize_quantity(coins_max),
    }


def normalize_failure_mechanics(action):
    stun_chance = action.get("stun_chance")
    damage_chance = action.get("damage_chance")

    mechanics = {
        "stun": {
            "chance": (
                stun_chance / 100
                if stun_chance is not None
                else None
            ),
            "duration_seconds": action.get("stun_seconds"),
        },
        "damage": {
            "chance": (
                damage_chance / 100
                if damage_chance is not None
                else None
            ),
            "min": normalize_quantity(action.get("damage_min")),
            "max": normalize_quantity(action.get("damage_max")),
        },
    }

    has_stun = any(
        value is not None
        for value in mechanics["stun"].values()
    )

    has_damage = any(
        value is not None
        for value in mechanics["damage"].values()
    )

    if not has_stun and not has_damage:
        return None

    return mechanics


def normalize_action(action):
    """
    Convert one raw Thieving activity into the optimizer's
    normalized representation.

    The dynamic 'calc' object is intentionally NOT copied.
    """

    loot_drops = [
        normalize_drop(drop)
        for drop in (action.get("loot_drops") or [])
    ]

    rare_drops = [
        normalize_rare_drop(drop)
        for drop in (action.get("seed_drops") or [])
    ]

    normalized = {
        "skill": "thieving",
        "activity_type": "queued",

        "key": action.get("key"),
        "level": action.get("min_level"),
        "action": action.get("label"),

        "base_time_seconds": ms_to_seconds(
            action.get("duration_ms")
        ),

        "xp": action.get("xp"),

        "coin_reward": normalize_coin_reward(action),

        "success_rate": (
            action.get("success_rate") / 100
            if action.get("success_rate") is not None
            else None
        ),

        "outputs": [],

        "loot_drops": loot_drops,

        "rare_drops": rare_drops,

        "failure_mechanics": normalize_failure_mechanics(
            action
        ),
    }

    return normalized


# ============================================================
# Validation
# ============================================================

def validate_action(action, index):
    errors = []

    prefix = f"[{index}] {action.get('key')}"

    if not action.get("key"):
        errors.append(f"{prefix}: missing key")

    if not action.get("label"):
        errors.append(f"{prefix}: missing label")

    if action.get("min_level") is None:
        errors.append(f"{prefix}: missing min_level")

    if action.get("duration_ms") is None:
        errors.append(f"{prefix}: missing duration_ms")

    if action.get("xp") is None:
        errors.append(f"{prefix}: missing xp")

    # Validate loot drops.
    for i, drop in enumerate(action.get("loot_drops") or []):
        if not drop.get("item_key"):
            errors.append(
                f"{prefix}: loot drop {i} missing item_key"
            )

        if drop.get("quantity_min") is None:
            errors.append(
                f"{prefix}: loot drop {i} missing quantity_min"
            )

        if drop.get("quantity_max") is None:
            errors.append(
                f"{prefix}: loot drop {i} missing quantity_max"
            )

        if drop.get("drop_chance") is None:
            errors.append(
                f"{prefix}: loot drop {i} missing drop_chance"
            )

    # Validate rare drops.
    for i, drop in enumerate(action.get("seed_drops") or []):
        if not drop.get("item_key"):
            errors.append(
                f"{prefix}: rare drop {i} missing item_key"
            )

        if drop.get("chance") is None:
            errors.append(
                f"{prefix}: rare drop {i} missing chance"
            )

    return errors


# ============================================================
# Main
# ============================================================

def main():

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found:\n{INPUT_FILE}"
        )

    with INPUT_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    actions = data.get("actions", [])

    if not isinstance(actions, list):
        raise ValueError(
            "skill_thieving.json does not contain "
            "an 'actions' list."
        )

    # --------------------------------------------------------
    # Validate source data
    # --------------------------------------------------------

    validation_errors = []

    for index, action in enumerate(actions, 1):
        validation_errors.extend(
            validate_action(action, index)
        )

    # --------------------------------------------------------
    # Normalize
    # --------------------------------------------------------

    normalized_actions = [
        normalize_action(action)
        for action in actions
    ]

    # Sort by required level.
    normalized_actions.sort(
        key=lambda x: (
            x["level"] if x["level"] is not None else 999999
        )
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    total_actions = len(normalized_actions)

    total_loot_entries = sum(
        len(action["loot_drops"])
        for action in normalized_actions
    )

    total_rare_entries = sum(
        len(action["rare_drops"])
        for action in normalized_actions
    )

    actions_with_requirements = sum(
        any(
            drop.get("requirements")
            for drop in action["loot_drops"]
        )
        for action in normalized_actions
    )

    actions_with_failure_mechanics = sum(
        action["failure_mechanics"] is not None
        for action in normalized_actions
    )

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    output = {
        "skill": "thieving",

        "source": {
            "file": "skill_thieving.json",
            "action_count": total_actions,
        },

        "normalization_notes": [
            "Dynamic calc data is intentionally excluded.",
            "Success, stun, and damage probabilities are preserved without assuming their interaction.",
            "Loot drop probabilities are preserved as decimal probabilities.",
            "Cross-skill loot requirements are preserved.",
            "Coin rewards are preserved separately from item loot.",
            "Rare drops are preserved separately from ordinary loot.",
        ],

        "activities": normalized_actions,
    }

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            output,
            f,
            indent=2,
            ensure_ascii=False
        )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    print("=" * 80)
    print("THIEVING NORMALIZATION")
    print("=" * 80)

    print(f"Source actions:              {total_actions}")
    print(f"Loot drop entries:           {total_loot_entries}")
    print(f"Rare drop entries:           {total_rare_entries}")
    print(
        f"Actions with requirements:   "
        f"{actions_with_requirements}"
    )
    print(
        f"Failure mechanics:           "
        f"{actions_with_failure_mechanics}"
    )

    print()
    print(
        f"Validation errors:           "
        f"{len(validation_errors)}"
    )

    if validation_errors:
        print()
        print("VALIDATION ERRORS")
        print("-" * 80)

        for error in validation_errors:
            print(f"  {error}")

    print()
    print(f"Output written to:")
    print(f"  {OUTPUT_FILE}")

    print()
    print("First and last activities:")
    print("-" * 80)

    if normalized_actions:
        first = normalized_actions[0]
        last = normalized_actions[-1]

        print(
            f"  {first['level']:>3} "
            f"{first['action']} "
            f"({first['base_time_seconds']}s)"
        )

        print(
            f"  {last['level']:>3} "
            f"{last['action']} "
            f"({last['base_time_seconds']}s)"
        )


if __name__ == "__main__":
    main()