"""Normalize text in the scraped monster dataset and report validation totals."""

import json
from pathlib import Path

from build_wiki_monsters import clean_text


INPUT_FILE = Path("data/wiki/monsters.json")
OUTPUT_FILE = INPUT_FILE


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, str):
        return clean_text(value)
    return value


def validate(data):
    errors = []
    monsters = data.get("monsters")
    if not isinstance(monsters, dict):
        return ["Missing monsters object"]
    if data.get("monster_count") != len(monsters):
        errors.append("monster_count does not match the monsters object")

    for key, monster in monsters.items():
        if not monster.get("name"):
            errors.append(f"{key}: missing name")
        for index, drop in enumerate(monster.get("drops", [])):
            prefix = f"{key}.drops[{index}]"
            if not drop.get("item_key"):
                errors.append(f"{prefix}: missing item_key")
            if drop.get("quantity_min") is None or drop.get("quantity_max") is None:
                errors.append(f"{prefix}: missing quantity bounds")
            chance = drop.get("drop_chance")
            if not isinstance(chance, (int, float)) or not 0 <= chance <= 1:
                errors.append(f"{prefix}: invalid probability {chance!r}")

    def find_bad_strings(value, path="root"):
        if isinstance(value, dict):
            for key, item in value.items():
                find_bad_strings(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                find_bad_strings(item, f"{path}[{index}]")
        elif isinstance(value, str) and ("\ufffd" in value or "\u00ad" in value or "\u0090" in value):
            errors.append(f"{path}: unresolved replacement/control character")

    find_bad_strings(data)
    return errors


def main():
    with INPUT_FILE.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    data = normalize(data)
    errors = validate(data)
    with OUTPUT_FILE.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    monster_count = len(data.get("monsters", {}))
    drop_count = sum(len(monster.get("drops", [])) for monster in data.get("monsters", {}).values())
    print(f"Validated {monster_count} monsters and {drop_count} drops.")
    if errors:
        print(f"Found {len(errors)} validation issues:")
        for error in errors[:50]:
            print(f"- {error}")
        raise SystemExit(1)
    print("Text symbols and drop fields passed validation.")


if __name__ == "__main__":
    main()
