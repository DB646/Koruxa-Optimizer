import requests
from bs4 import BeautifulSoup
import json
import re
from pathlib import Path


BASE_URL = "https://koruxa.com/wiki/items/"


def get_item_page(item_key):
    """
    Download one Koruxa Wiki item page.
    """
    url = f"{BASE_URL}{item_key}.html"

    response = requests.get(url, timeout=20)

    response.raise_for_status()

    return response.text


def parse_item_page(html):
    """
    Extract the basic information we currently care about.
    """

    soup = BeautifulSoup(html, "html.parser")

    text = soup.get_text(" ", strip=True)

   # print("\n--- PAGE TEXT ---")
   # print(text[:10000])
   # print("--- END PAGE TEXT ---\n")    

    item = {
        "name": None,
        "npc_sell_price": None,
        "market_price": None,
        "market_volume_7d": None,
    }

    # Item name
    title = soup.find("h1")

    if title:
        item["name"] = title.get_text(strip=True)

    # Market price
    market_match = re.search(
        r"Market price\s*~?([\d,]+)\s*coins",
        text,
        re.IGNORECASE,
    )

    if market_match:
        item["market_price"] = int(
            market_match.group(1).replace(",", "")
        )


    # 7-day trading volume
    volume_match = re.search(
        r"([\d,]+)\s*traded last 7 days",
        text,
        re.IGNORECASE,
    )

    if volume_match:
        item["market_volume_7d"] = int(
            volume_match.group(1).replace(",", "")
        )
    
    # NPC sell price
    npc_match = re.search(
        r"Sell price\s*([\d,]+)\s*coins\s*\(NPC\)",
        text,
        re.IGNORECASE,
    )

    if npc_match:
        item["npc_sell_price"] = int(
            npc_match.group(1).replace(",", "")
        )
   
    return item


def scrape_item(item_key):

    print(f"Scraping {item_key}...")

    html = get_item_page(item_key)

    return parse_item_page(html)


def main():

    # We'll test with one item first.
    item_keys = [
        "dustite_bar",
       "dustite_arrow",
        "frostleaf_seed",
    ]

    results = {}

    for item_key in item_keys:
        try:
            results[item_key] = scrape_item(item_key)
        except Exception as e:
            print(f"ERROR scraping {item_key}: {e}")

    print()
    print(json.dumps(results, indent=4))

    output_path = Path("data/wiki/test_items.json")

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4)

    print()
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()