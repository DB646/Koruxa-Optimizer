import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re


ITEMS_URL = "https://koruxa.com/wiki/items/"


def discover_items():

    print(f"Downloading {ITEMS_URL}...")

    response = requests.get(ITEMS_URL, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    items = {}

    for link in soup.find_all("a", href=True):

        href = link["href"]

        # Match links such as:
        # /wiki/items/dustite_bar.html
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
                "https://koruxa.com",
                href
            ),
        }

    return items


def main():

    items = discover_items()

    print()
    print(f"Found {len(items):,} item pages.")
    print()

    for key, item in list(items.items())[:25]:

        print(
            f"{key:35} {item['name']}"
        )


if __name__ == "__main__":
    main()