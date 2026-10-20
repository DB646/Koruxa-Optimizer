import json
from pathlib import Path


ITEM_FILE = Path("data/wiki/items.json")
OUTPUT_FILE = Path("data/wiki/item_name_map.json")


def normalize_name(name):
    """
    Normalize an item name for reliable matching.
    """

    if not name:
        return ""

    return (
        str(name)
        .strip()
        .casefold()
    )


def main():

    with open(
        ITEM_FILE,
        "r",
        encoding="utf-8"
    ) as f:
        items = json.load(f)

    name_map = {}

    for item_key, item in items.items():

        name = item.get("name")

        if not name:
            continue

        normalized = normalize_name(name)

        if normalized in name_map:

            existing = name_map[normalized]

            print(
                "WARNING: duplicate item name:"
            )

            print(
                f"  {name!r}"
            )

            print(
                f"  {existing}"
            )

            print(
                f"  {item_key}"
            )

            continue

        name_map[normalized] = item_key

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
            name_map,
            f,
            indent=2,
            ensure_ascii=False,
            sort_keys=True
        )

    print()
    print("=" * 60)
    print("ITEM NAME MAP COMPLETE")
    print("=" * 60)
    print()
    print(
        f"Items in source: {len(items):,}"
    )
    print(
        f"Names mapped:    {len(name_map):,}"
    )
    print(
        f"Output:          {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()