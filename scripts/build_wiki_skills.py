import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pathlib import Path
import json
import re
import time


SKILLS_URL = "https://koruxa.com/wiki/skills/"
BASE_URL = "https://koruxa.com"

OUTPUT_FILE = Path("data/wiki/skills.json")

REQUEST_DELAY = 0.15


def discover_skills():

    print("Downloading skill index...")

    response = requests.get(
        SKILLS_URL,
        timeout=20
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    skills = {}

    for link in soup.find_all("a", href=True):

        href = link["href"]

        match = re.search(
            r"/wiki/skills/([^/]+)\.html$",
            href
        )

        if not match:
            continue

        skill_key = match.group(1)

        # Ignore the index itself.
        if skill_key == "index":
            continue

        skill_name = link.get_text(
            " ",
            strip=True
        )

        # The navigation text contains things like
        # "Woodcutting 14 actions".
        # Keep the actual skill name clean.
        skill_name = re.sub(
            r"\s+\d+\s+actions?.*$",
            "",
            skill_name,
            flags=re.IGNORECASE
        )

        skill_name = re.sub(
            r"\s+\d+\s+spells?.*$",
            "",
            skill_name,
            flags=re.IGNORECASE
        )

        skill_name = re.sub(
            r"\s+\d+\s+targets?.*$",
            "",
            skill_name,
            flags=re.IGNORECASE
        )

        skills[skill_key] = {
            "name": skill_name,
            "url": urljoin(
                BASE_URL,
                href
            ),
        }

    return skills


def parse_skill_page(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Preserve the complete page text.
    text = soup.get_text(
        " ",
        strip=True
    )

    # Also preserve the raw HTML-derived structure.
    tables = []

    for table in soup.find_all("table"):

        rows = []

        for tr in table.find_all("tr"):

            cells = []

            for cell in tr.find_all(
                ["th", "td"]
            ):

                cells.append(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )

            if cells:
                rows.append(cells)

        if rows:
            tables.append(rows)

    return {
        "page_text": text,
        "tables": tables,
    }


def scrape_skill(
    session,
    skill_key,
    skill_info
):

    response = session.get(
        skill_info["url"],
        timeout=20
    )

    response.raise_for_status()

    data = parse_skill_page(
        response.text
    )

    data["skill_key"] = skill_key
    data["name"] = skill_info["name"]
    data["url"] = skill_info["url"]

    return data


def main():

    skills = discover_skills()

    print()
    print(
        f"Found {len(skills)} skill pages."
    )
    print()

    results = {}
    errors = []

    session = requests.Session()

    total = len(skills)

    for number, (skill_key, skill_info) in enumerate(
        skills.items(),
        start=1
    ):

        print(
            f"[{number}/{total}] "
            f"{skill_key}"
        )

        try:

            data = scrape_skill(
                session,
                skill_key,
                skill_info
            )

            results[skill_key] = data

        except Exception as error:

            print(
                f"    ERROR: {error}"
            )

            errors.append({
                "skill_key": skill_key,
                "url": skill_info["url"],
                "error": str(error),
            })

        time.sleep(
            REQUEST_DELAY
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
            results,
            file,
            indent=2,
            ensure_ascii=False
        )

    print()
    print("=" * 60)
    print("COMPLETE")
    print("=" * 60)

    print(
        f"Successful: {len(results)}"
    )

    print(
        f"Errors:     {len(errors)}"
    )

    print(
        f"Output:     {OUTPUT_FILE}"
    )

    if errors:

        error_file = Path(
            "data/wiki/skill_errors.json"
        )

        with open(
            error_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                errors,
                file,
                indent=2,
                ensure_ascii=False
            )

        print(
            f"Errors:     {error_file}"
        )


if __name__ == "__main__":
    main()