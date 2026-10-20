import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path
import json
import re
import time


ITEMS_URL = "https://koruxa.com/wiki/items/"
BASE_URL = "https://koruxa.com"

OUTPUT_FILE = Path("data/wiki/items.json")
ERROR_FILE = Path("data/wiki/item_errors.json")

# Small delay so we don't hammer the Wiki.
REQUEST_DELAY = 0.15


def discover_items():

    print(f"Downloading item index...")

    response = requests.get(
        ITEMS_URL,
        timeout=20
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    items = {}

    for link in soup.find_all("a", href=True):

        href = link["href"]

        match = re.search(
            r"/wiki/items/([^/]+)\.html$",
            href
        )

        if not match:
            continue

        item_key = match.group(1)

        item_name = link.get_text(
            " ",
            strip=True
        )

        items[item_key] = {
            "name": item_name,
            "url": urljoin(
                BASE_URL,
                href
            ),
        }

    return items


def parse_item_page(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    text = soup.get_text(
        " ",
        strip=True
    )

    item = {
        "name": None,
        "npc_sell_price": None,
        "market_price": None,
        "market_volume_7d": None,
    }

    # Item name
    title = soup.find("h1")

    if title:
        item["name"] = title.get_text(
            strip=True
        )

    # Market price
    market_match = re.search(
        r"Market price\s*~?([\d,]+)\s*coins",
        text,
        re.IGNORECASE
    )

    if market_match:

        item["market_price"] = int(
            market_match.group(1).replace(
                ",",
                ""
            )
        )

    # 7-day trading volume
    volume_match = re.search(
        r"([\d,]+)\s*traded last 7 days",
        text,
        re.IGNORECASE
    )

    if volume_match:

        item["market_volume_7d"] = int(
            volume_match.group(1).replace(
                ",",
                ""
            )
        )

    # NPC sell price
    npc_match = re.search(
        r"Sell price\s*([\d,]+)\s*coins\s*\(NPC\)",
        text,
        re.IGNORECASE
    )

    if npc_match:

        item["npc_sell_price"] = int(
            npc_match.group(1).replace(
                ",",
                ""
            )
        )

    return item


def scrape_item(
    session,
    item_key,
    item_info
):

    response = session.get(
        item_info["url"],
        timeout=20
    )

    response.raise_for_status()

    data = parse_item_page(
        response.text
    )

    # Keep the canonical key and URL.
    data["item_key"] = item_key
    data["url"] = item_info["url"]

    return data


def main():

    items = discover_items()

    total = len(items)

    print()
    print(f"Found {total:,} item pages.")
    print()

    results = {}
    errors = []

    session = requests.Session()

    for number, (item_key, item_info) in enumerate(
        items.items(),
        start=1
    ):

        print(
            f"[{number:,}/{total:,}] "
            f"{item_key}"
        )

        try:

            data = scrape_item(
                session,
                item_key,
                item_info
            )

            results[item_key] = data

        except Exception as error:

            print(
                f"    ERROR: {error}"
            )

            errors.append({
                "item_key": item_key,
                "url": item_info["url"],
                "error": str(error),
            })

        # Don't hammer the Wiki.
        time.sleep(
            REQUEST_DELAY
        )

        # Save every 100 items.
        if number % 100 == 0:

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
                    results,
                    file,
                    indent=2,
                    ensure_ascii=False
                )

            print(
                f"    Saved {len(results):,} items."
            )

    # Final save
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
            results,
            file,
            indent=2,
            ensure_ascii=False
        )

    # Save errors separately
    with open(
        ERROR_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            errors,
            file,
            indent=2,
            ensure_ascii=False
        )

    print()
    print("=" * 60)
    print("COMPLETE")
    print("=" * 60)
    print(
        f"Successful: {len(results):,}"
    )
    print(
        f"Errors:     {len(errors):,}"
    )
    print(
        f"Output:     {OUTPUT_FILE}"
    )
    print(
        f"Errors:     {ERROR_FILE}"
    )


if __name__ == "__main__":
    main()