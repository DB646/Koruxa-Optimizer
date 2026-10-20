import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


URL = "https://koruxa.com/wiki/skills/"


def main():

    print(f"Downloading {URL}...")

    response = requests.get(
        URL,
        timeout=20
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    print()
    print("Skill Wiki links:")
    print("-" * 70)

    seen = set()

    for link in soup.find_all("a", href=True):

        href = link["href"]
        text = link.get_text(
            " ",
            strip=True
        )

        if not text:
            continue

        full_url = urljoin(
            URL,
            href
        )

        if full_url in seen:
            continue

        seen.add(full_url)

        print(
            f"{text:35} {full_url}"
        )


if __name__ == "__main__":
    main()