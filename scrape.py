#!/usr/bin/env python3
"""
House Hunter Scraper
Fetches active SFH listings (<=$950k) from Redfin across target zip codes,
auto-scores from available data, and pushes data.json to GitHub.

Usage:
  GITHUB_TOKEN=ghp_xxx python3 scrape.py
  python3 scrape.py --dry-run        # no GitHub push, prints results only
  python3 scrape.py --debug          # verbose output of raw Redfin responses

Deps: pip3 install requests beautifulsoup4
"""

import requests
import json
import base64
import time
import re
import sys
import os
import hashlib
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from bs4 import BeautifulSoup


# ─── CONFIGURATION ────────────────────────────────────────────────────────────

GITHUB_REPO  = "jpherron/jphousehunter"
GITHUB_FILE  = "data.json"
DATA_URL     = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE}"
MIN_PRICE    = 700_000
MAX_PRICE    = 1_000_000
REQUEST_DELAY = 4  # seconds between Redfin requests

# Each entry: (zipcode, default_tier_key, display_name)
# Boston-proper zips default to "other-*" because sub-neighborhoods within the
# zip vary — the user should refine the tier for promising listings.
SEARCH_AREAS = [
    ("02131", "other-rozzie",  "Roslindale MA"),
    ("02132", "other-wr",      "West Roxbury MA"),
    ("02136", "other-hp",      "Hyde Park MA"),
    ("02026", "tier2-dedham",  "Dedham MA"),
    ("01760", "tier2-natick",  "Natick MA"),
    ("02062", "tier2-norwood", "Norwood MA"),
    ("02081", "tier2-walpole", "Walpole MA"),
    ("02360", "tier2-plymouth","Plymouth MA"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ─── FETCH ────────────────────────────────────────────────────────────────────

def fetch_search_page(zipcode: str, debug: bool = False) -> str | None:
    """Fetch a Redfin search results page for a zip code."""
    url = (
        f"https://www.redfin.com/zipcode/{zipcode}"
        f"/filter/property-type=house,min-price={MIN_PRICE},max-price={MAX_PRICE},status=active"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if debug:
            print(f"  [{zipcode}] HTTP {resp.status_code} — {len(resp.text)} bytes")
        if resp.status_code == 403:
            print(f"  [{zipcode}] 403 Forbidden — Redfin blocked the request")
            return None
        if resp.status_code == 429:
            print(f"  [{zipcode}] 429 Rate-limited — increase REQUEST_DELAY")
            return None
        if resp.status_code != 200:
            print(f"  [{zipcode}] HTTP {resp.status_code} — skipping")
            return None
        # Real bot-block pages say "Access Denied" or have a captcha form;
        # normal Redfin pages have <meta name="robots"> which is harmless.
        if any(phrase in resp.text for phrase in [
            "Access Denied", "cf-browser-verification", "Please verify you are a human",
            "<title>Blocked</title>", "captcha-form",
        ]):
            print(f"  [{zipcode}] Bot detection page returned")
            return None
        return resp.text
    except requests.RequestException as e:
        print(f"  [{zipcode}] Request error: {e}")
        return None


# ─── PARSE STRATEGY 1: embedded JSON ──────────────────────────────────────────

def parse_embedded_json(html: str) -> list[dict]:
    """
    Redfin embeds listing data in several possible script patterns.
    Returns a list of raw listing dicts, or [] if not found.
    """
    results = []

    # Pattern A: __reactServerState JSON blob
    m = re.search(r'window\.__reactServerState\s*=\s*(\{.+?\});\s*</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            homes = _dig_homes_from_state(data)
            if homes:
                return homes
        except json.JSONDecodeError:
            pass

    # Pattern B: large JSON array assigned to a variable containing "homes"
    for pat in [
        r'"homes"\s*:\s*(\[.*?\])\s*[,}]',
        r'"displayedHomes"\s*:\s*(\[.*?\])',
        r'"cards"\s*:\s*(\[.*?\])',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                arr = json.loads(m.group(1))
                if arr and isinstance(arr, list) and isinstance(arr[0], dict):
                    return arr
            except json.JSONDecodeError:
                continue

    # Pattern C: script tag with application/json
    for tag in re.findall(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(tag)
            homes = _dig_homes_from_state(data)
            if homes:
                return homes
        except (json.JSONDecodeError, TypeError):
            continue

    return results


def _dig_homes_from_state(obj, depth=0) -> list | None:
    """Recursively search for a 'homes' or 'displayedHomes' array."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for key in ("homes", "displayedHomes", "cards", "results"):
            if key in obj and isinstance(obj[key], list) and len(obj[key]) > 0:
                if isinstance(obj[key][0], dict):
                    return obj[key]
        for v in obj.values():
            result = _dig_homes_from_state(v, depth + 1)
            if result:
                return result
    elif isinstance(obj, list) and len(obj) > 2:
        for item in obj[:3]:
            result = _dig_homes_from_state(item, depth + 1)
            if result:
                return result
    return None


def normalize_json_home(raw: dict, zipcode: str, default_tier: str) -> dict | None:
    """Convert a raw Redfin JSON home dict to our listing format."""
    # Redfin uses various key names across versions
    def get(*keys):
        for k in keys:
            v = raw.get(k)
            if v not in (None, "", 0):
                return v
        return None

    # Address
    address = get("streetLine", "street", "address")
    city    = get("cityStateZip", "city")
    if not address:
        return None
    full_address = f"{address}, {city}" if city else f"{address}, {zipcode}"

    # Price
    price_raw = get("price", "listPrice", "soldPrice")
    if not price_raw:
        return None
    price_num = int(str(price_raw).replace(",", "").replace("$", "").strip())
    if price_num < MIN_PRICE or price_num > MAX_PRICE:
        return None

    # Optional fields
    sqft     = _to_int(get("sqFt", "sqft", "livingArea"))
    beds     = _to_int(get("beds", "bedrooms", "numBeds"))
    year     = _to_int(get("yearBuilt", "year_built", "yearbuilt"))
    url      = get("url", "propertyId")
    if url and not url.startswith("http"):
        url = "https://www.redfin.com" + url

    return _build_listing(full_address, price_num, sqft, beds, year, url, zipcode, default_tier)


# ─── PARSE STRATEGY 2: HTML card fallback ─────────────────────────────────────

def parse_html_cards(html: str, zipcode: str, default_tier: str) -> list[dict]:
    """
    Parse Redfin listing cards using BeautifulSoup.
    Primary selector: div.bp-Homecard (current Redfin layout, 2024-2025).
    Fallback selectors handle older layouts.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    card_selectors = [
        "div.bp-Homecard",
        "div.HomeCardContainer",
        "div.home-card",
        "div[data-rf-test-name='basicNode-homeCard']",
    ]
    cards = []
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            break

    for card in cards:
        try:
            listing = _parse_card(card, zipcode, default_tier)
            if listing:
                listings.append(listing)
        except Exception:
            continue

    return listings


def _parse_card(card, zipcode: str, default_tier: str) -> dict | None:
    """
    Extract data from a single bp-Homecard element.

    Current Redfin structure (2025):
    - aria-label="Property at {address}, {beds} beds, {baths} baths"
    - title="{address}"
    - Stats div text: "{beds} beds {baths} baths {sqft} sq ft"
    - Price in element with class containing 'price'
    - href="/MA/{City}/{Street}/home/{id}"
    Year built is not in search cards; it's on the property detail page.
    """
    # Address — prefer title attribute (clean), fall back to aria-label parsing
    address = card.get("title", "").strip()
    if not address:
        aria = card.get("aria-label", "")
        m = re.match(r"Property at (.+?),\s*\d+ beds", aria)
        if m:
            address = m.group(1).strip()
    if not address:
        return None

    # Price
    stats_text = card.get_text(" ", strip=True)
    price_num = None
    price_el = card.find(class_=re.compile(r"price", re.IGNORECASE))
    if price_el:
        price_num = _parse_price_str(price_el.get_text(strip=True))
    if not price_num:
        # Fallback: look for dollar sign in full card text
        m = re.search(r'\$([\d,]+)', stats_text)
        if m:
            price_num = _to_int(m.group(1).replace(",", ""))
    if not price_num or price_num < MIN_PRICE or price_num > MAX_PRICE:
        return None

    # Sqft
    sqft = None
    m = re.search(r'([\d,]+)\s*sq\s*ft', stats_text, re.IGNORECASE)
    if m:
        sqft = _to_int(m.group(1).replace(",", ""))

    # Beds — from aria-label first (most reliable), then text
    beds = None
    aria = card.get("aria-label", "")
    m = re.search(r',\s*(\d+)\s*beds?', aria, re.IGNORECASE)
    if m:
        beds = int(m.group(1))
    else:
        m = re.search(r'(\d+)\s*beds?', stats_text, re.IGNORECASE)
        if m:
            beds = int(m.group(1))

    # Year built — not in search cards; left as None
    year = None

    # URL
    url = None
    link = card.find("a", href=True)
    if link:
        href = link["href"]
        url = href if href.startswith("http") else "https://www.redfin.com" + href

    return _build_listing(address, price_num, sqft, beds, year, url, zipcode, default_tier)


# ─── BUILD LISTING ────────────────────────────────────────────────────────────

def _infer_tier(address: str, default_tier: str) -> str:
    """
    Assign neighborhood tier based on the city portion of the address.
    Addresses are formatted "{street}, {city}, {state} {zip}" — we check
    the city segment only so street names like "Dedham St" don't mis-classify.
    """
    # Extract city: everything between the first and second comma
    parts = address.split(",")
    city = parts[1].strip().lower() if len(parts) >= 2 else address.lower()

    # Tier 1 — Boston neighborhoods (user refines sub-neighborhood manually)
    if "roslindale" in city:    return "other-rozzie"
    if "west roxbury" in city:  return "other-wr"
    if "hyde park" in city:     return "other-hp"
    if "jamaica plain" in city: return "other-jp"
    if "mattapan" in city:      return "other-metro"
    # Tier 2 — target towns
    if "dedham" in city:        return "tier2-dedham"
    if "natick" in city:        return "tier2-natick"
    if "norwood" in city:       return "tier2-norwood"
    if city in ("walpole", "east walpole"): return "tier2-walpole"
    if "walpole" in city:       return "tier2-walpole"
    if "plymouth" in city:      return "tier2-plymouth"
    # Everything else: keep but mark as other-metro
    return "other-metro"


def _build_listing(
    address: str,
    price_num: int,
    sqft: int | None,
    beds: int | None,
    year: int | None,
    url: str | None,
    zipcode: str,
    default_tier: str,
) -> dict:
    listing_id = _make_id(address)
    tier = _infer_tier(address, default_tier)
    return {
        "id": listing_id,
        "address": address,
        "price": f"${price_num:,}",
        "url": url or "",
        "year": str(year) if year else "",
        "neighborhood_tier": tier,
        "added": datetime.now(timezone.utc).isoformat(),
        "scores": _auto_score(price_num, sqft, beds, year),
    }


def _auto_score(price_num: int, sqft: int | None, beds: int | None, year: int | None) -> dict:
    """
    Score what we can from structured data. Use None for anything we can't
    determine — the JS app treats None/undefined as 'not yet scored'.
    """
    scores = {k: None for k in [
        "price","property_type","t_walk","safety","sqft","beds","yard","layout",
        "condition","roof","hvac","electrical","plumbing","windows","architecture",
        "natural_light","neighborhood_feel","historic_age","garage_basement",
        "parking","restaurants_walk","nature_walk",
    ]}

    # Price (0=Full≤$950k, 1=Good $950k–$1M, 2=Over>$1M)
    if price_num <= 950_000:      scores["price"] = 0
    elif price_num <= 1_000_000:  scores["price"] = 1
    else:                         scores["price"] = 2

    # Property type — we only scrape houses, so always SFH (Full)
    scores["property_type"] = 0

    # Square footage (0=1500-2500, 1=edge, 2=small, 3=tiny)
    if sqft:
        if 1500 <= sqft <= 2500:                            scores["sqft"] = 0
        elif (1300 <= sqft < 1500) or (2500 < sqft <= 2800): scores["sqft"] = 1
        elif 1100 <= sqft < 1300:                           scores["sqft"] = 2
        else:                                               scores["sqft"] = 3

    # Bedrooms (0=3+, 1=2 could convert, 2=2 cramped, 3=1bed)
    if beds:
        if beds >= 3:   scores["beds"] = 0
        elif beds == 2: scores["beds"] = 1
        elif beds == 1: scores["beds"] = 3

    # Historic age — scored properly once year is known; left None until then
    # (enrich_year_built() fills this in after fetching detail pages)

    return scores


# ─── YEAR BUILT ENRICHMENT ───────────────────────────────────────────────────

DETAIL_WORKERS = 8   # concurrent detail-page fetches
DETAIL_DELAY   = 0.5 # seconds between requests per worker

OFF_MARKET_SENTINEL = -1  # returned when detail page shows listing is no longer active

OFF_MARKET_PHRASES = [
    '"status":"Pending"', '"status":"Contingent"', '"status":"Sold"',
    '"status":"Off Market"', '"status":"Canceled"',
    '>Pending<', '>Contingent<', '>Under Contract<', '>Off Market<',
    'listingStatus":"PENDING"', 'listingStatus":"CONTINGENT"',
    'listingStatus":"SOLD"',
]


def fetch_year_built(url: str) -> int | None:
    """
    Fetch a Redfin property detail page, check it's still active,
    and extract year built.
    Returns OFF_MARKET_SENTINEL if the listing is no longer active.
    Returns None if year can't be determined.
    """
    if not url or "redfin.com" not in url:
        return None
    try:
        time.sleep(DETAIL_DELAY)
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Check if listing went off-market since we scraped it
        if any(phrase in html for phrase in OFF_MARKET_PHRASES):
            return OFF_MARKET_SENTINEL
        # Fast path: yearBuilt in embedded JSON
        m = re.search(r'"yearBuilt"\s*:\s*(\d{4})', html)
        if m:
            return int(m.group(1))
        # Fallback: keyDetails section
        m = re.search(r'(\d{4})\s*Year\s*Built', html)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def enrich_year_built(listings: list[dict], debug: bool = False) -> None:
    """
    Fetch year built from detail pages for listings that don't have one.
    Mutates listings in-place. Uses a thread pool for speed.
    """
    need = [l for l in listings if not l.get("year") and l.get("url")]
    if not need:
        return
    print(f"\nFetching year built for {len(need)} listings ({DETAIL_WORKERS} parallel)...")

    def fetch_one(listing):
        year = fetch_year_built(listing["url"])
        return listing["id"], year

    found = 0
    off_market_ids = set()
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(fetch_one, l): l for l in need}
        for future in as_completed(futures):
            lid, year = future.result()
            if year == OFF_MARKET_SENTINEL:
                off_market_ids.add(lid)
            elif year:
                for l in listings:
                    if l["id"] == lid:
                        l["year"] = str(year)
                        l["scores"]["historic_age"] = _age_score(year)
                        found += 1
                        if debug:
                            print(f"  {l['address']}: {year}")
                        break

    if off_market_ids:
        before = len(listings)
        listings[:] = [l for l in listings if l["id"] not in off_market_ids]
        print(f"  Removed {before - len(listings)} off-market listings detected on detail page")

    print(f"  Got year built for {found}/{len(need)} listings")


def _age_score(year: int) -> int:
    """Return historic_age score index for a given year."""
    if year < 1930:  return 0  # Full
    if year >= 2016: return 1  # Good (new)
    if year < 1960:  return 2  # Low (1930-1959)
    return 3                   # None (1960-2015)


# ─── DEDUP ────────────────────────────────────────────────────────────────────

def _score_completeness(scores: dict) -> int:
    """Count non-null scores — used to prefer the more complete entry on dedup."""
    return sum(1 for v in scores.values() if v is not None)


def dedup_merge(new_listings: list[dict], existing: list[dict]) -> list[dict]:
    """
    Merge scraped listings with existing data.json listings.
    - Existing manually-scored listings are always kept as-is.
    - Scraped duplicates of existing listings are dropped (manual scores win).
    - Among scraped duplicates of each other, keep the one with more complete scores.
    - New listings are appended.
    Returns the merged list.
    """
    def norm_addr(a: str) -> str:
        return re.sub(r'\W+', ' ', a).lower().strip()

    # Build lookup from normalized address → existing listing
    existing_by_addr = {norm_addr(l["address"]): l for l in existing}
    merged = list(existing)

    # Among new scraped listings, resolve duplicates (same address, different areas)
    seen: dict[str, dict] = {}
    for l in new_listings:
        key = norm_addr(l["address"])
        if key in existing_by_addr:
            # Already in existing — skip (don't overwrite manual scores)
            continue
        if key in seen:
            # Keep whichever has more complete scores
            if _score_completeness(l["scores"]) > _score_completeness(seen[key]["scores"]):
                seen[key] = l
        else:
            seen[key] = l

    merged.extend(seen.values())
    return merged


# ─── GITHUB ───────────────────────────────────────────────────────────────────

def get_current_data() -> tuple[dict, str | None]:
    """Fetch the current data.json from GitHub. Returns (data, sha)."""
    try:
        resp = requests.get(DATA_URL, timeout=10)
        if resp.status_code == 200:
            return resp.json(), None
    except Exception:
        pass
    # If fetch fails, fall back to empty structure
    return {"listings": [], "weights": {}}, None


def get_file_sha(token: str) -> str | None:
    """Get the current SHA of data.json in the repo (needed for updates)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    resp = requests.get(url, headers={"Authorization": f"token {token}"}, timeout=10)
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def push_to_github(token: str, data: dict) -> bool:
    """Push updated data.json to GitHub. Returns True on success."""
    sha = get_file_sha(token)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    payload = {
        "message": f"scraper: update listings ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(url, json=payload, headers={"Authorization": f"token {token}"}, timeout=20)
    if resp.status_code in (200, 201):
        print(f"  Pushed data.json to GitHub ({len(data['listings'])} listings)")
        return True
    else:
        print(f"  GitHub push failed: {resp.status_code} {resp.text[:200]}")
        return False


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _parse_price_str(text: str) -> int | None:
    m = re.search(r'\$?([\d,]+)', text.replace("K", "000").replace("M", "000000"))
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _to_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _make_id(address: str) -> str:
    h = hashlib.md5(address.lower().strip().encode()).hexdigest()[:8]
    return f"s{h}"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="House Hunter Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Don't push to GitHub")
    parser.add_argument("--debug", action="store_true", help="Show raw response info")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") if not args.dry_run else None
    if not args.dry_run and not token:
        print("Error: set GITHUB_TOKEN environment variable (or use --dry-run)")
        sys.exit(1)

    # Load current data.json
    print("Loading current data.json from GitHub...")
    current_data, _ = get_current_data()
    existing_listings = current_data.get("listings", [])
    weights = current_data.get("weights", {})
    print(f"  {len(existing_listings)} existing listings")

    # Scrape each area
    all_new = []
    for zipcode, default_tier, label in SEARCH_AREAS:
        print(f"\nScraping {label} ({zipcode})...")
        html = fetch_search_page(zipcode, debug=args.debug)
        if not html:
            continue

        # Strategy 1: embedded JSON
        raw_homes = parse_embedded_json(html)
        if raw_homes:
            print(f"  Found {len(raw_homes)} homes via embedded JSON")
            for raw in raw_homes:
                listing = normalize_json_home(raw, zipcode, default_tier)
                if listing:
                    all_new.append(listing)
        else:
            # Strategy 2: HTML cards
            listings = parse_html_cards(html, zipcode, default_tier)
            print(f"  Found {len(listings)} homes via HTML parsing")
            all_new.extend(listings)

        time.sleep(REQUEST_DELAY)

    print(f"\nScraped {len(all_new)} total new listings before dedup")

    # Enrich new listings with year built from detail pages
    # (only fetches for listings not already in existing data)
    existing_addrs = {re.sub(r'\W+', ' ', l["address"]).lower().strip() for l in existing_listings}
    truly_new = [
        l for l in all_new
        if re.sub(r'\W+', ' ', l["address"]).lower().strip() not in existing_addrs
    ]
    enrich_year_built(truly_new, debug=args.debug)

    # Merge with existing
    merged = dedup_merge(all_new, existing_listings)
    added = len(merged) - len(existing_listings)
    print(f"After dedup: {len(merged)} listings total ({added:+d} new)")

    # Show new listings
    if added > 0:
        new_ids = {l["id"] for l in merged} - {l["id"] for l in existing_listings}
        print("\nNew listings:")
        for l in merged:
            if l["id"] in new_ids:
                print(f"  {l['address']} — {l['price']} ({l['neighborhood_tier']})")

    # Dry run: dump result and exit
    if args.dry_run:
        print("\n[dry-run] data.json would contain:")
        print(json.dumps({"listings": merged[:3], "weights": weights}, indent=2)[:800] + "...")
        return

    # Push to GitHub
    updated_data = {"listings": merged, "weights": weights}
    print("\nPushing to GitHub...")
    push_to_github(token, updated_data)


if __name__ == "__main__":
    main()
