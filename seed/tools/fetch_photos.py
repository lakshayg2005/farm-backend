"""Download the demo photos from Wikimedia Commons and write seed/assets/CREDITS.md.

Run once by a developer (the photos are committed, the seeder never needs the network):

    uv run --with pillow --with httpx python seed/tools/fetch_photos.py [--only tomato,rice]

For each key below the first search result that is a JPEG, at least 1000 px wide and under an open licence
(CC0, public domain, CC BY, CC BY-SA) is saved as a 1024 px JPEG. Authors and licences go to CREDITS.md,
which must ship with the photos. Re-run with --only to replace single photos; --skip-existing keeps the rest.
"""

import argparse
import html
import io
import re
import time
from pathlib import Path

import httpx
from PIL import Image

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "photos"
API = "https://commons.wikimedia.org/w/api.php"
UA = "farm-platform-demo-seed/1.0 (university course project; open licensed demo photos)"

# key -> search words. Keys are what the seeder refers to.
PHOTOS: dict[str, str] = {
    # produce
    "tomato": "tomato fruit red",
    "brinjal": "brinjal eggplant fruit",
    "okra": "okra pods bhindi",
    "onion": "red onion bulbs",
    "potato": "potato tubers",
    "cabbage": "cabbage head vegetable",
    "cauliflower": "cauliflower vegetable",
    "carrot": "carrots harvest",
    "bottle_gourd": "bottle gourd lauki",
    "spinach": "spinach leaves",
    "chilli": "green chilli pepper",
    "pumpkin": "pumpkin vegetable",
    "guava": "guava fruit",
    "banana": "banana bunch",
    "mango": "mango fruit",
    "rice": "rice paddy grains",
    "chana": "chickpea chana",
    "wheat": "wheat grain ears",
    "mustard": "mustard flowers field",
    "maize": "maize corn cobs",
    "peas": "green peas pods",
    # crop stages
    "stage_sown": "seedlings sprouting field india",
    "stage_growing_paddy": "paddy field green india",
    "stage_growing_wheat": "wheat field green young",
    "stage_ready_paddy": "paddy field golden ripe india",
    "stage_ready_wheat": "wheat field golden harvest",
    "farm_1": "farm field village india landscape",
    "farm_2": "indian farmland green fields",
    "farm_3": "vegetable farm india field rows",
    # operations
    "delivery": "box of vegetables delivery",
    "inspection": "vegetable market inspection crates",
}

# Reviewed picks: key -> exact Commons file. A key without a pin falls back to the first good search hit.
PINNED: dict[str, str] = {
    "carrot": "Carrot bunches.jpg",
    "chilli": "Dried red chillies in Jaipur.jpg",
    "stage_growing_wheat": "Green Fields in India.jpg",
    "tomato": "Organic home-grown tomatoes - unripe to ripe.jpg",
    "brinjal": "Another photo of freshly harvested Vellore Spiny Brinjals.jpg",
    "okra": "Okra (Abelmoschus esculentus) (2).jpg",
    "cabbage": "Chou cabus blanc 01.jpg",
    "pumpkin": "Pumpkin harvest-001.jpg",
    "rice": "Women Harvesting Rice Paddy.jpg",
    "chana": "Chickpea field in Tamil Nadu JEG6266.jpg",
    "wheat": "Ripe wheat ears.jpg",
    "maize": "Maize crop in rural India.jpg",
    "stage_sown": "Women Farmers Sowing in Karnataka, India.jpg",
    "stage_ready_paddy": "Paddy Field at Eravathour Thrissur.jpg",
    "farm_2": "Village Fields (16596769371).jpg",
    "farm_3": "Carrot Cabbage Fields Hullathy Nilgiris Dec24 A7CR 05419.jpg",
    "delivery": "Tomatos ready for dispatch.jpg",
    "inspection": "Tomatoes Fresh from Crates, Northern Ghana.jpg",
}

OK_LICENCES = re.compile(r"^(CC0|Public domain|PD|CC BY(-SA)? [0-9.]+|CC BY(-SA)?)$", re.I)
BAD_TITLE = re.compile(
    r"illustration|drawing|diagram|map|logo|icon|painting|stamp|svg|plate|botanical|cartoon|seed ?packet",
    re.I,
)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def search(client: httpx.Client, words: str) -> list[dict]:
    r = client.get(
        API,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {words}",
            "gsrnamespace": 6,
            "gsrlimit": 25,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": 1024,
            "format": "json",
        },
    )
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    return sorted(pages.values(), key=lambda p: p.get("index", 0))


def by_title(client: httpx.Client, title: str) -> list[dict]:
    r = client.get(
        API,
        params={
            "action": "query",
            "titles": f"File:{title}",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": 1024,
            "format": "json",
        },
    )
    r.raise_for_status()
    return list(((r.json().get("query") or {}).get("pages") or {}).values())


def pick(candidates: list[dict], pinned: bool = False) -> dict | None:
    for page in candidates:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        licence = clean(meta.get("LicenseShortName", {}).get("value", ""))
        if (
            info.get("mime") == "image/jpeg"
            and info.get("width", 0) >= (900 if pinned else 1000)
            and OK_LICENCES.match(licence)
            and (pinned or not BAD_TITLE.search(page["title"]))
            and "NonCommercial" not in clean(meta.get("UsageTerms", {}).get("value", ""))
        ):
            return {
                "title": page["title"],
                "thumb": info["thumburl"],
                "page": info["descriptionurl"],
                "licence": licence,
                "author": clean(meta.get("Artist", {}).get("value", "")) or "Unknown",
            }
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    only = {k for k in args.only.split(",") if k}
    ASSETS.mkdir(parents=True, exist_ok=True)
    credits_path = ASSETS.parent / "CREDITS.md"
    rows: dict[str, str] = {}
    if credits_path.exists():
        for line in credits_path.read_text().splitlines():
            if (
                line.startswith("| ")
                and not line.startswith("| Key")
                and not line.startswith("| ---")
            ):
                rows[line.split("|")[1].strip()] = line
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as client:
        for key, words in PHOTOS.items():
            target = ASSETS / f"{key}.jpg"
            if (only and key not in only) or (args.skip_existing and target.exists()):
                continue
            time.sleep(3)
            found = (
                pick(by_title(client, PINNED[key]), pinned=True)
                if key in PINNED
                else pick(search(client, words))
            )
            if not found:
                print(f"{key}: nothing suitable for '{words}'")
                continue
            for attempt in range(4):
                data = client.get(found["thumb"])
                if data.status_code != 429:
                    break
                time.sleep(8 * (attempt + 1))
            data.raise_for_status()
            image = Image.open(io.BytesIO(data.content)).convert("RGB")
            image.thumbnail((1024, 1024))
            image.save(target, "JPEG", quality=82, optimize=True)
            rows[key] = (
                f"| {key} | [{found['title'].removeprefix('File:')}]({found['page']}) "
                f"| {found['author'][:80].replace('|', '/')} | {found['licence']} |"
            )
            print(
                f"{key}: {found['title']} ({found['licence']}) {target.stat().st_size // 1024} kB"
            )
            time.sleep(1.5)
    body = "\n".join(rows[k] for k in PHOTOS if k in rows)
    credits_path.write_text(
        "# Demo photo credits\n\nPhotos from Wikimedia Commons under open licences, resized to 1024 px. "
        "Keep this file with the photos.\n\n| Key | Photo | Author | Licence |\n| --- | --- | --- | --- |\n"
        + body
        + "\n"
    )


if __name__ == "__main__":
    main()
