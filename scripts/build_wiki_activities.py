import json
from pathlib import Path
import re


INPUT_FILE = Path("data/wiki/skills.json")
OUTPUT_FILE = Path("data/wiki/activities.json")


ACTION_HEADERS = [
    "Lv",
    "Action",
    "XP",
    "Time",
    "XP/h",
    "Makes",
    "Needs",
    "Rare rolls",
]


def clean_text(value):
    """Clean whitespace and common Wiki encoding artifacts."""

    if value is None:
        return ""

    value = str(value)

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return value


def is_action_table(table):
    """Determine whether a table contains skill activities."""

    if not table:
        return False

    header = [
        clean_text(x)
        for x in table[0]
    ]

    return header == ACTION_HEADERS


def parse_number(value):

    value = clean_text(value)

    if not value:
        return None

    # Remove commas.
    value = value.replace(",", "")

    try:
        if "." in value:
            return float(value)

        return int(value)

    except ValueError:
        return None


def parse_activity_table(
    skill_key,
    table
):

    activities = []

    if not is_action_table(table):
        return activities

    for row in table[1:]:

        # Ignore malformed rows.
        if len(row) < 8:
            continue

        activity = {
            "skill": skill_key,
            "level": parse_number(row[0]),
            "action": clean_text(row[1]),
            "xp": parse_number(row[2]),
            "time": clean_text(row[3]),
            "xp_per_hour": parse_number(row[4]),
            "makes": clean_text(row[5]),
            "needs": clean_text(row[6]),
            "rare_rolls": clean_text(row[7]),
        }

        # Ignore completely empty rows.
        if not activity["action"]:
            continue

        activities.append(activity)

    return activities


def main():

    with open(
        INPUT_FILE,
        "r",
        encoding="utf-8"
    ) as file:

        skills = json.load(file)

    activities = []

    counts = {}

    for skill_key, skill_data in skills.items():

        skill_activities = []

        for table in skill_data.get(
            "tables",
            []
        ):

            parsed = parse_activity_table(
                skill_key,
                table
            )

            skill_activities.extend(
                parsed
            )

        counts[skill_key] = len(
            skill_activities
        )

        activities.extend(
            skill_activities
        )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            activities,
            file,
            indent=2,
            ensure_ascii=False
        )

    print()
    print("=" * 60)
    print("ACTIVITY EXTRACTION COMPLETE")
    print("=" * 60)
    print()

    total = 0

    for skill, count in counts.items():

        print(
            f"{skill:20} {count:4} activities"
        )

        total += count

    print()
    print(
        f"TOTAL: {total:,} activities"
    )
    print()
    print(
        f"Output: {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()