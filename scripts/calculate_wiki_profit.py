import json
import re
from pathlib import Path


ACTIVITY_FILE = Path("data/wiki/activities_normalized.json")
ITEM_FILE = Path("data/wiki/items.json")
NAME_MAP_FILE = Path("data/wiki/item_name_map.json")

OUTPUT_FILE = Path("data/wiki/activity_profit.json")

HOURS_PER_DAY = 24


def normalize_name(name):
    if not name:
        return ""

    return str(name).strip().casefold()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_time_seconds(text):
    """
    Convert Wiki time strings into seconds.

    Examples:
        12s
        1m
        1m 8s
    """

    if text is None:
        return None

    text = str(text).strip().casefold()

    if not text:
        return None

    minutes = 0
    seconds = 0

    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*m", text)
    second_match = re.search(r"(\d+(?:\.\d+)?)\s*s", text)

    if minute_match:
        minutes = float(minute_match.group(1))

    if second_match:
        seconds = float(second_match.group(1))

    total = minutes * 60 + seconds

    return total if total > 0 else None


def parse_quantity(text):
    """
    Convert quantities such as:

        1
        2
        1-3

    into an expected quantity.

    For ranges, use the midpoint.
    """

    if text is None:
        return 0.0

    text = str(text).strip()

    if not text:
        return 0.0

    try:
        return float(text)
    except ValueError:
        pass

    if "-" in text:

        parts = text.split("-")

        if len(parts) == 2:

            try:
                low = float(parts[0].strip())
                high = float(parts[1].strip())

                return (low + high) / 2

            except ValueError:
                pass

    return 0.0


def get_item_price(item_key, items):

    item = items.get(item_key)

    if not item:
        return None

    market_price = item.get("market_price")

    if market_price is not None:

        try:
            return float(market_price)

        except (ValueError, TypeError):
            pass

    npc_price = item.get("npc_sell_price")

    if npc_price is not None:

        try:
            return float(npc_price)

        except (ValueError, TypeError):
            pass

    return None


def get_item_market_volume(item_key, items):
    item = items.get(item_key)
    if not item:
        return None

    volume = item.get("market_volume_7d")
    if volume is not None:
        try:
            return float(volume)
        except (ValueError, TypeError):
            pass

    return None


def parse_item_entry(text):
    """
    Parse a Wiki 'makes' or 'needs' string.

    Examples:

        Basic Log
        Dustite Bar x2
        Dustite Bar ×2
        Dustite Bar 2

    The Wiki extraction currently gives us strings, so this
    function deliberately handles the simple formats first.
    """

    if not text:
        return []

    text = str(text).strip()

    if not text:
        return []

    # Normalize multiplication symbols.
    text = text.replace("×", "x")

    # Handle comma-separated entries.
    parts = [
        part.strip()
        for part in text.split(",")
        if part.strip()
    ]

    results = []

    for part in parts:

        quantity = 1.0
        item_name = part

        # Format: Item x2
        match = re.match(
            r"^(.*?)\s+x\s*(\d+(?:\.\d+)?)$",
            part,
            flags=re.IGNORECASE
        )

        if match:

            item_name = match.group(1).strip()
            quantity = float(match.group(2))

        else:

            # Format: Item 2
            match = re.match(
                r"^(.*?)\s+(\d+(?:\.\d+)?)$",
                part
            )

            if match:

                item_name = match.group(1).strip()
                quantity = float(match.group(2))

        results.append({
            "name": item_name,
            "quantity": quantity
        })

    return results


def resolve_item(item_name, name_map):

    normalized = normalize_name(item_name)

    return name_map.get(normalized)


def main():

    print("Loading Wiki data...")

    activities = load_json(ACTIVITY_FILE)
    items = load_json(ITEM_FILE)
    name_map = load_json(NAME_MAP_FILE)

    print(f"Activities: {len(activities):,}")
    print(f"Items:      {len(items):,}")
    print(f"Name map:   {len(name_map):,}")

    results = []

    unresolved_inputs = set()
    unresolved_outputs = set()

    skipped_time = 0

    for activity in activities:

        skill = activity.get("skill")
        level = activity.get("level")
        action = activity.get("action")

        # --------------------------------------------------
        # TIME
        # --------------------------------------------------

        time_seconds = activity.get("base_time_seconds")

        if time_seconds is None:
            time_seconds = activity.get("time_seconds")

        if time_seconds is None:
            time_seconds = activity.get("time")
        try:
            time_seconds = float(time_seconds)
        except (ValueError, TypeError):
            continue

        if time_seconds <= 0:
            continue

        # --------------------------------------------------
        # OUTPUT
        # --------------------------------------------------

        output_entries = activity.get("outputs", [])
        input_entries = activity.get("inputs", [])

        input_cost = 0.0
        output_value = 0.0

        input_details = []
        output_details = []

        output_coverage_hours = None

        # --------------------------------------------------
        # INPUTS
        # --------------------------------------------------

        for entry in input_entries:

            item_name = entry.get("item")
            quantity = entry.get("quantity", 1)

            item_key = resolve_item(
                item_name,
                name_map
            )

            if not item_key:

                unresolved_inputs.add(item_name)

                input_details.append({
                    "name": item_name,
                    "quantity": quantity,
                    "item_key": None,
                    "price": None,
                    "cost": None
                })

                continue

            price = get_item_price(
                item_key,
                items
            )

            market_volume_7d = get_item_market_volume(item_key, items)

            if price is None:

                unresolved_inputs.add(item_name)

                input_details.append({
                    "name": item_name,
                    "quantity": quantity,
                    "item_key": item_key,
                    "price": None,
                    "market_volume_7d": market_volume_7d,
                    "cost": None
                })

                continue

            cost = quantity * price

            input_cost += cost

            input_details.append({
                "name": item_name,
                "quantity": quantity,
                "item_key": item_key,
                "price": price,
                "market_volume_7d": market_volume_7d,
                "cost": cost
            })

        # --------------------------------------------------
        # OUTPUTS
        # --------------------------------------------------

        for entry in output_entries:
            item_name = entry.get("item")
            quantity = entry.get("quantity", 1)
            item_key = resolve_item(item_name, name_map)

            if not item_key:
                unresolved_outputs.add(item_name)
                output_details.append({
                    "name": item_name,
                    "quantity": quantity,
                    "item_key": None,
                    "price": None,
                    "market_volume_7d": None,
                    "value": None
                })
                continue

            price = get_item_price(item_key, items)
            market_volume_7d = get_item_market_volume(item_key, items)

            if price is None:
                unresolved_outputs.add(item_name)
                output_details.append({
                    "name": item_name,
                    "quantity": quantity,
                    "item_key": item_key,
                    "price": None,
                    "market_volume_7d": market_volume_7d,
                    "value": None
                })
                continue

            value = quantity * price
            output_value += value

            output_details.append({
                "name": item_name,
                "quantity": quantity,
                "item_key": item_key,
                "price": price,
                "market_volume_7d": market_volume_7d,
                "value": value
            })


        # --------------------------------------------------
        # PROFIT
        # --------------------------------------------------

        profit_per_cycle = (
            output_value - input_cost
        )

        cycles_per_hour = (
            3600.0 / time_seconds
        )

        output_coverages = []

        for detail in output_details:
            volume_7d = detail.get("market_volume_7d")
            quantity = detail.get("quantity", 0)

            if volume_7d is None or quantity <= 0:
                continue

            production_per_hour = quantity * cycles_per_hour

            if production_per_hour <= 0:
                continue

            market_coverage_hours = volume_7d / (production_per_hour * 7)

            detail["production_per_hour"] = production_per_hour
            detail["market_coverage_hours"] = market_coverage_hours

            output_coverages.append(market_coverage_hours)

        if output_coverages:
            output_coverage_hours = min(output_coverages)

        # Calculate market-volume-constrained production over a 24-hour horizon.
        executable_profit_24h = None
        executable_output_value_24h = None
        executable_cycles_24h = None
        input_limited_cycles_24h = None
        output_limited_cycles_24h = None

        if output_details:
            executable_cycle_limits = []

            for detail in output_details:
                volume_7d = detail.get("market_volume_7d")
                quantity = detail.get("quantity", 0)

                if volume_7d is None or quantity <= 0:
                    executable_cycle_limits = []
                    break

                # Average market volume available per 24 hours.
                daily_market_volume = volume_7d / 7.0

                # Number of production cycles that could be sold during one day.
                cycle_limit = daily_market_volume / quantity
                executable_cycle_limits.append(cycle_limit)

            output_limited_cycles_24h = None

            if executable_cycle_limits:
                output_limited_cycles_24h = min(executable_cycle_limits)

                executable_cycles_24h = min(
                    cycles_per_hour * HOURS_PER_DAY,
                    input_limited_cycles_24h if input_limited_cycles_24h is not None else float("inf"),
                    output_limited_cycles_24h
                )

                executable_output_value_24h = 0.0

                for detail in output_details:
                    quantity = detail.get("quantity", 0)
                    price = detail.get("price")

                    if price is None:
                        executable_output_value_24h = None
                        break

                    executable_output_value_24h += (
                        executable_cycles_24h *
                        quantity *
                        price
                    )

                if executable_output_value_24h is not None:
                    executable_input_cost_24h = (
                        input_cost * executable_cycles_24h
                    )

                    executable_profit_24h = (
                        executable_output_value_24h -
                        executable_input_cost_24h
                    )

        # Identify the limiting factor over a 24-hour horizon.
        bottleneck = None
        bottleneck_item = None


        theoretical_cycles_24h = cycles_per_hour * HOURS_PER_DAY

        if executable_cycles_24h is not None:
            if executable_cycles_24h >= theoretical_cycles_24h:
                bottleneck = "production"

            elif (
                output_limited_cycles_24h is not None
                and executable_cycles_24h == output_limited_cycles_24h
                and (
                    input_limited_cycles_24h is None
                    or output_limited_cycles_24h <= input_limited_cycles_24h
                )
            ):
                bottleneck = "output"

                limiting_outputs = [
                    detail
                    for detail in output_details
                    if detail.get("market_volume_7d") is not None
                    and detail.get("quantity", 0) > 0
                    and (
                        detail.get("market_volume_7d") / 7.0
                    ) / detail.get("quantity", 1) == output_limited_cycles_24h
                ]

                if limiting_outputs:
                    bottleneck_item = limiting_outputs[0].get("name")

            elif input_limited_cycles_24h is not None:
                bottleneck = "input"

                limiting_inputs = [
                    detail
                    for detail in input_details
                    if detail.get("market_volume_7d") is not None
                    and detail.get("quantity", 0) > 0
                    and (
                        detail.get("market_volume_7d") / 7.0
                    ) / detail.get("quantity", 1) == input_limited_cycles_24h
                ]

                if limiting_inputs:
                    bottleneck_item = limiting_inputs[0].get("name")


        # Calculate input-market capacity over a 24-hour horizon.
        input_coverage_hours = None
        input_limited_cycles_24h = None

        input_coverages = []

        for detail in input_details:
            volume_7d = detail.get("market_volume_7d")
            quantity = detail.get("quantity", 0)

            if volume_7d is None or quantity <= 0:
                continue

            consumption_per_hour = quantity * cycles_per_hour

            if consumption_per_hour <= 0:
                continue

            market_coverage_hours = volume_7d / (consumption_per_hour * 7)

            detail["consumption_per_hour"] = consumption_per_hour
            detail["market_coverage_hours"] = market_coverage_hours

            input_coverages.append(market_coverage_hours)

        if input_coverages:
            input_coverage_hours = min(input_coverages)

            input_limited_cycles_24h = min(
                (
                    detail.get("market_volume_7d") / 7.0
                ) / detail.get("quantity", 1)
                for detail in input_details
                if (
                    detail.get("market_volume_7d") is not None
                    and detail.get("quantity", 0) > 0
                )
            )

        profit_per_hour = (
            profit_per_cycle * cycles_per_hour
        )

        # Classify the activity based on profitability and liquidity.
        liquidity_classification = None

        if profit_per_hour <= 0:
            liquidity_classification = "unprofitable"

        elif executable_cycles_24h is None:
            liquidity_classification = "no_liquidity_data"

        elif bottleneck == "output":
            liquidity_classification = "output_limited"

        elif bottleneck == "input":
            liquidity_classification = "input_limited"

        elif bottleneck == "production":
            liquidity_classification = "production_limited"

        market_share_required_pct = None

        if bottleneck and bottleneck_item:
            bottleneck_details = (
                output_details
                if bottleneck == "output"
                else input_details
            )

            for detail in bottleneck_details:
                if detail.get("name") != bottleneck_item:
                    continue

                volume_7d = detail.get("market_volume_7d")
                quantity = detail.get("quantity", 0)

                if volume_7d is not None and quantity > 0:
                    theoretical_daily_quantity = (
                        quantity *
                        cycles_per_hour *
                        HOURS_PER_DAY
                    )

                    average_daily_volume = volume_7d / 7.0

                    if average_daily_volume > 0:
                        market_share_required_pct = (
                           theoretical_daily_quantity /
                            average_daily_volume
                        ) * 100.0

                break

        # Calculate theoretical and liquidity-constrained 24-hour profit.
        theoretical_profit_24h = profit_per_hour * HOURS_PER_DAY

        liquidity_realization_pct = None

        if executable_profit_24h is not None and theoretical_profit_24h > 0:
            liquidity_realization_pct = (
                executable_profit_24h /
                theoretical_profit_24h
            ) * 100.0

        results.append({

            "skill": skill,
            "level": level,
            "action": action,

            "time": activity.get("time"),
            "time_seconds": time_seconds,

            "xp": activity.get("xp"),
            "xp_per_hour": activity.get("xp_per_hour"),

            "cycles_per_hour": cycles_per_hour,

            "input_cost": input_cost,
            "output_value": output_value,

            "profit_per_cycle": profit_per_cycle,
            "profit_per_hour": profit_per_hour,
            "theoretical_profit_24h": theoretical_profit_24h,
            "output_coverage_hours": output_coverage_hours,
            "input_coverage_hours": input_coverage_hours,
            "executable_cycles_24h": executable_cycles_24h,

            "bottleneck": bottleneck,
            "bottleneck_item": bottleneck_item,
            "liquidity_classification": liquidity_classification,
            "market_share_required_pct": market_share_required_pct,
            "liquidity_realization_pct": liquidity_realization_pct,

            "input_limited_cycles_24h": input_limited_cycles_24h,
            "executable_output_value_24h": executable_output_value_24h,
            "executable_profit_24h": executable_profit_24h,
            "inputs": input_details,
            "outputs": output_details,
	    "output_limited_cycles_24h": output_limited_cycles_24h,

            "rare_rolls": activity.get("rare_rolls", "")
        })

    # ------------------------------------------------------
    # SORT
    # ------------------------------------------------------

    results.sort(
        key=lambda x: x["profit_per_hour"],
        reverse=True
    )

    # ------------------------------------------------------
    # SAVE
    # ------------------------------------------------------

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            results,
            f,
            indent=2,
            ensure_ascii=False
        )

    for result in results:
        if result.get("action") in {
            "Astralite Knowledge Amulet",
            "Crimsrite Arrows",
        }:
            print("\n" + "=" * 80)
            print(result["action"])
            print("=" * 80)
            print(json.dumps(result, indent=2))


    # ------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------

    print()
    print("=" * 60)
    print("BASE WIKI PROFIT CALCULATION COMPLETE")
    print("=" * 60)

    print()
    print(f"Activities calculated: {len(results):,}")
    print(f"Activities skipped:    {skipped_time:,}")

    print()
    print("Top 20 activities by base gold/hour:")
    print("-" * 75)
    print(
        f"{'Skill':15} {'Lv':>3} {'Activity':25} "
        f"{'Profit/hour':>14} {'Coverage':>10} {'Exec 24h':>14}"
    )
    print("-" * 75)

    for result in results[:20]:

        coverage = result.get("output_coverage_hours")

        if coverage is None:
            coverage_text = "       N/A"
        elif coverage < 1:
            coverage_text = f"{coverage * 60:6.1f} min"
        else:
            coverage_text = f"{coverage:6.1f} hr"

        exec_profit = result.get("executable_profit_24h")

        if exec_profit is None:
            exec_text = "N/A"
        else:
            exec_text = f"{exec_profit:,.0f}"

        print(
            f"{result['skill']:15} "
            f"Lv {str(result['level']):>3} "
            f"{result['action'][:25]:25} "
            f"{result['profit_per_hour']:>14,.0f} gp/h "
            f"{coverage_text:>10}"
            f"{exec_text:>14}"
        )

    print()
    print(
        f"Unresolved input items:  "
        f"{len(unresolved_inputs):,}"
    )

    print(
        f"Unresolved output items: "
        f"{len(unresolved_outputs):,}"
    )

    if unresolved_inputs:

        print()
        print("Example unresolved inputs:")

        for name in sorted(unresolved_inputs)[:20]:

            print(f"  {name}")

    if unresolved_outputs:

        print()
        print("Example unresolved outputs:")

        for name in sorted(unresolved_outputs)[:20]:

            print(f"  {name}")

    print()
    print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()