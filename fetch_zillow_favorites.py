#!/usr/bin/env python3
"""
Zillow Favorites Importer
Fetches your saved homes from Zillow using browser session cookies and
syncs them into the house evaluator app (Supabase) or data.json (GitHub).

Usage:
  # Sync to the app (Supabase) — recommended
  python3 fetch_zillow_favorites.py --cookies "..." --sync-app
  python3 fetch_zillow_favorites.py --cookies "..." --sync-app --dry-run

  # Legacy: push to GitHub data.json
  python3 fetch_zillow_favorites.py --cookies "cookie_string_here"
  python3 fetch_zillow_favorites.py --cookies-file zillow_cookies.txt
  python3 fetch_zillow_favorites.py --cookies "..." --dry-run

How to get your cookie string:

  Chrome:
  1. Go to https://www.zillow.com/myzillow/favorites (logged in)
  2. Open DevTools (F12 or Cmd+Option+I) → Network tab
  3. Reload the page
  4. Click the first request in the list (the page itself) → Headers → Request Headers
  5. Right-click the "cookie:" header value → Copy value
  6. Paste it as the --cookies argument (wrap in quotes)

  Firefox:
  1. Go to https://www.zillow.com/myzillow/favorites (logged in)
  2. Open DevTools (F12 or Cmd+Option+E) → Network tab
  3. Reload the page
  4. Click the first request in the list (the page itself) → Headers → Request Headers
  5. Right-click the "Cookie:" header value → Copy Value
  6. Paste it as the --cookies argument (wrap in quotes)

  Both browsers also support: right-click any request → "Copy as cURL",
  then extract the -H 'cookie: ...' part.
"""

import requests
import json
import re
import sys
import os
import hashlib
import argparse
import base64
from datetime import datetime, timezone


GITHUB_REPO = "jpherron/jphousehunter"
GITHUB_FILE = "data.json"
DATA_URL    = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE}"

GIST_ID   = "22bc628d7e6700e2fc9aea07d6f70ee5"
GIST_FILE = "hev-data.json"


BROWSER_PROFILE = os.path.expanduser("~/.jphousehunter_browser")


def gist_get_cookies() -> str | None:
    """Read Zillow cookies stored in the app's Gist settings."""
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if r.status_code == 200:
            content = r.json().get("files", {}).get(GIST_FILE, {}).get("content", "{}")
            state = json.loads(content)
            return state.get("hev_zillow_cookies") or None
    except Exception:
        pass
    return None


def gist_set_cookies(cookie_str: str, token: str) -> bool:
    """Write Zillow cookies back to the app's Gist."""
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
        state = {}
        if r.status_code == 200:
            content = r.json().get("files", {}).get(GIST_FILE, {}).get("content", "{}")
            state = json.loads(content)
        state["hev_zillow_cookies"] = cookie_str
        r2 = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            json={"files": {GIST_FILE: {"content": json.dumps(state)}}},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        return r2.status_code == 200
    except Exception:
        return False


def browser_get_cookies() -> str:
    """
    Launch a persistent Chromium window to zillow.com/myzillow/favorites
    and extract cookies. Saves the browser profile so login persists across runs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("playwright not installed — installing now...")
        import subprocess
        subprocess.run(
            ["pip3", "install", "playwright", "--break-system-packages", "-q"],
            check=True,
        )
        subprocess.run(
            ["python3", "-m", "playwright", "install", "chromium", "--quiet"],
            check=True,
        )
        from playwright.sync_api import sync_playwright

    print("  Opening browser — log in to Zillow if prompted, then wait for your favorites to load...")
    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            BROWSER_PROFILE,
            headless=False,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.zillow.com/myzillow/favorites")
        page.wait_for_url("*zillow.com/myzillow/favorites*", timeout=120_000)
        page.wait_for_load_state("networkidle", timeout=30_000)
        cookies = ctx.cookies(["https://www.zillow.com"])
        ctx.close()

    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

MIN_PRICE = 700_000
MAX_PRICE = 1_100_000  # slightly wider net for Zillow import; filter in UI

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.zillow.com/",
    "DNT": "1",
}

NEIGHBORHOOD_TIER_MAP = {
    "02131": "other-rozzie",   # Roslindale
    "02132": "other-wr",       # West Roxbury
    "02136": "other-hp",       # Hyde Park
    "02026": "tier2-dedham",   # Dedham
    "01760": "tier2-natick",   # Natick
    "02062": "tier2-norwood",  # Norwood
    "02081": "tier2-walpole",  # Walpole
    "02360": "tier2-plymouth", # Plymouth
    "02130": "other-jp",       # Jamaica Plain
}


# ─── FETCH SAVED HOMES ────────────────────────────────────────────────────────

def fetch_saved_homes_page(cookie_str: str, debug: bool = False) -> str | None:
    """Fetch the Zillow saved-homes page HTML."""
    headers = {**HEADERS_BASE, "Cookie": cookie_str}
    url = "https://www.zillow.com/myzillow/favorites"
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if debug:
            print(f"  HTTP {resp.status_code} — {len(resp.text)} bytes")
        if resp.status_code == 200:
            return resp.text
        print(f"  HTTP {resp.status_code} — check that your cookies are fresh")
        return None
    except requests.RequestException as e:
        print(f"  Request error: {e}")
        return None


def fetch_via_graphql(cookie_str: str, debug: bool = False) -> list[dict]:
    """
    Try Zillow's internal GraphQL endpoint for saved homes.
    Zillow uses this for the saved-homes feed on some app versions.
    """
    headers = {
        **HEADERS_BASE,
        "Cookie": cookie_str,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": "https://www.zillow.com/myzillow/favorites",
        "Origin": "https://www.zillow.com",
    }
    # Zillow's internal saved-homes GraphQL query
    payload = {
        "operationName": "GetSavedHomesListView",
        "variables": {"pagination": {"currentPage": 1}},
        "query": """
query GetSavedHomesListView($pagination: PaginationInput) {
  savedHomes(pagination: $pagination) {
    savedHomesCount
    homes {
      zpid
      hdpUrl
      price
      beds
      baths
      livingArea
      yearBuilt
      address {
        streetAddress
        city
        state
        zipcode
      }
    }
  }
}"""
    }
    try:
        resp = requests.post(
            "https://www.zillow.com/graphql",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if debug:
            print(f"  GraphQL HTTP {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            homes = (
                data.get("data", {})
                    .get("savedHomes", {})
                    .get("homes", [])
            )
            if homes:
                return homes
    except Exception as e:
        if debug:
            print(f"  GraphQL error: {e}")
    return []


# ─── PARSE ────────────────────────────────────────────────────────────────────

def extract_from_next_data(html: str, debug: bool = False) -> list[dict]:
    """
    Zillow embeds all page data in <script id="__NEXT_DATA__"> as JSON.
    Saved homes are nested somewhere inside pageProps.
    """
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        if debug:
            print("  __NEXT_DATA__ script tag not found")
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        if debug:
            print(f"  JSON parse error: {e}")
        return []

    page_props = data.get("props", {}).get("pageProps", {})
    viewer = page_props.get("viewer", {})

    if debug:
        saved_raw = viewer.get("savedHomes")
        print(f"  viewer.savedHomes type: {type(saved_raw).__name__}")
        if isinstance(saved_raw, dict):
            print(f"  viewer.savedHomes keys: {list(saved_raw.keys())}")
        elif isinstance(saved_raw, list):
            print(f"  viewer.savedHomes: list of {len(saved_raw)}")
            if saved_raw and isinstance(saved_raw[0], dict):
                print(f"    first item keys: {list(saved_raw[0].keys())}")

    # Direct path: viewer.savedHomes (may be a list or a wrapped object)
    saved_raw = viewer.get("savedHomes")
    if isinstance(saved_raw, list) and saved_raw:
        homes = saved_raw
    elif isinstance(saved_raw, dict):
        # Unwrap GraphQL-style connection: {homes:[...]} or {results:[...]} etc.
        homes = (
            saved_raw.get("homes")
            or saved_raw.get("results")
            or saved_raw.get("savedHomes")
            or []
        )
    else:
        # Fallback: generic tree walk
        homes = _dig_for_homes(data, debug=debug)

    if debug:
        print(f"  Found {len(homes)} homes in __NEXT_DATA__")
    return homes


def _dig_for_homes(obj, depth: int = 0, debug: bool = False) -> list:
    if depth > 12:
        return []
    if isinstance(obj, dict):
        for key in ("savedHomes", "homes", "results", "listings", "favoritedHomes"):
            val = obj.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                # Validate: does it look like a home object?
                sample = val[0]
                if any(k in sample for k in ("zpid", "address", "hdpUrl", "streetAddress")):
                    return val
        for v in obj.values():
            result = _dig_for_homes(v, depth + 1, debug)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj[:5]:
            result = _dig_for_homes(item, depth + 1, debug)
            if result:
                return result
    return []


def normalize_zillow_home(raw: dict) -> dict | None:
    """Convert a Zillow SavedHome dict to our listing format."""

    # Saved homes wrap the property data under a 'property' key
    prop = raw.get("property") or raw

    # Address
    addr_obj = prop.get("address") or {}
    street  = addr_obj.get("streetAddress") or prop.get("streetAddress") or prop.get("street", "")
    city    = addr_obj.get("city") or prop.get("city", "")
    state   = addr_obj.get("state") or prop.get("state", "MA")
    zipcode = addr_obj.get("zipcode") or prop.get("zipcode") or prop.get("zip", "")

    if not street:
        return None

    full_address = f"{street}, {city}, {state} {zipcode}".strip(", ")

    # Price — integer in this data structure
    price_raw = prop.get("price") or prop.get("listPrice") or prop.get("unformattedPrice")
    if isinstance(price_raw, dict):
        price_raw = price_raw.get("value") or price_raw.get("amount")
    if not price_raw:
        return None
    price_num = _to_int(str(price_raw).replace("$", "").replace(",", ""))
    if not price_num:
        return None

    # Optional fields
    sqft  = _to_int(prop.get("livingArea") or prop.get("livingAreaValue") or prop.get("sqFt"))
    beds  = _to_int(prop.get("bedrooms") or prop.get("beds"))
    year  = _to_int(prop.get("yearBuilt") or prop.get("year_built"))

    # URL
    hdp_url = prop.get("hdpUrl") or prop.get("url") or prop.get("detailUrl") or ""
    if hdp_url and not hdp_url.startswith("http"):
        hdp_url = "https://www.zillow.com" + hdp_url
    zpid = prop.get("zpid") or raw.get("propertyId")
    if not hdp_url and zpid:
        hdp_url = f"https://www.zillow.com/homes/{zpid}_zpid/"

    # Neighborhood tier
    tier = NEIGHBORHOOD_TIER_MAP.get(zipcode, "other-metro")

    # Status — pending/under contract or active
    home_status = prop.get("homeStatus", "")
    sub_type    = prop.get("listing_sub_type") or {}
    if home_status in ("PENDING", "UNDER_CONTRACT") or sub_type.get("is_pending"):
        status = "pending"
    else:
        status = "for_sale"

    # Auto-score
    scores = _auto_score(price_num, sqft, beds, year)

    listing_id = _make_id(full_address)
    return {
        "id": listing_id,
        "name": full_address,
        "price": f"${price_num:,}",
        "url": hdp_url,
        "year": str(year) if year else "",
        "neighborhood_tier": tier,
        "status": status,
        "added": datetime.now(timezone.utc).isoformat(),
        "scores": scores,
    }


# ─── SCORING ──────────────────────────────────────────────────────────────────

def _auto_score(price_num: int, sqft: int | None, beds: int | None, year: int | None) -> dict:
    scores = {k: None for k in [
        "price", "property_type", "t_walk", "safety", "sqft", "beds", "yard",
        "layout", "condition", "roof", "hvac", "electrical", "plumbing",
        "windows", "architecture", "natural_light", "neighborhood_feel",
        "historic_age", "garage_basement", "parking", "restaurants_walk",
        "nature_walk",
    ]}
    if price_num <= 950_000:      scores["price"] = 0
    elif price_num <= 1_000_000:  scores["price"] = 1
    else:                         scores["price"] = 2

    if sqft:
        if 1500 <= sqft <= 2500:                              scores["sqft"] = 0
        elif (1300 <= sqft < 1500) or (2500 < sqft <= 2800): scores["sqft"] = 1
        elif 1100 <= sqft < 1300:                             scores["sqft"] = 2
        else:                                                 scores["sqft"] = 3
    if beds:
        if beds >= 3:   scores["beds"] = 0
        elif beds == 2: scores["beds"] = 1
        elif beds == 1: scores["beds"] = 3
    if year:
        if year < 1930:   scores["historic_age"] = 0
        elif year >= 2016: scores["historic_age"] = 1
        elif year < 1960: scores["historic_age"] = 2
        else:             scores["historic_age"] = 3

    return scores


# ─── GITHUB ───────────────────────────────────────────────────────────────────

def get_current_data() -> dict:
    import time as _t
    url = f"{DATA_URL}?t={int(_t.time())}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {"listings": [], "weights": {}}


def push_to_github(token: str, data: dict) -> bool:
    sha_resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}",
        headers={"Authorization": f"token {token}"},
        timeout=10,
    )
    sha = sha_resp.json().get("sha") if sha_resp.status_code == 200 else None
    content = json.dumps(data, indent=2, ensure_ascii=False)
    payload = {
        "message": f"import: add Zillow favorites ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
        "content": base64.b64encode(content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}",
        json=payload,
        headers={"Authorization": f"token {token}"},
        timeout=20,
    )
    if resp.status_code in (200, 201):
        print(f"  Pushed data.json to GitHub ({len(data['listings'])} listings)")
        return True
    print(f"  GitHub push failed: {resp.status_code} {resp.text[:200]}")
    return False


# ─── DEDUP MERGE ─────────────────────────────────────────────────────────────

def dedup_merge(new_listings: list[dict], existing: list[dict]) -> tuple[list[dict], int]:
    """Merge new Zillow listings with existing, preserving manual scores. Returns (merged, added_count)."""
    def norm(a: str) -> str:
        return re.sub(r'\W+', ' ', a).lower().strip()

    existing_by_addr = {norm(l.get("name") or l.get("address","")): l for l in existing}
    merged = list(existing)
    added = 0

    for l in new_listings:
        key = norm(l.get("name") or l.get("address",""))
        if key in existing_by_addr:
            ex = existing_by_addr[key]
            # Upgrade URL if existing has a stale Zillow zpid URL and new one differs
            if ex.get("url") != l.get("url") and l.get("url"):
                ex["url"] = l["url"]
        else:
            merged.append(l)
            existing_by_addr[key] = l
            added += 1

    return merged, added


# ─── SUPABASE SYNC ───────────────────────────────────────────────────────────

SB_URL = "https://ovymgkyzwtobkiphpoed.supabase.co"
SB_KEY = "sb_publishable_6YYiqctG59yOTGETh262jQ_FsxtjfTi"
SB_HEADERS = {
    "Content-Type": "application/json",
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Prefer": "resolution=merge-duplicates",
}

APP_CRITERIA = [
    "t_stop","portsmouth","walkable","restaurants","nature",
    "price","sqft","type","bedrooms","ensuite","ceiling",
    "storage","workshop","fenced","move_in","ac",
    "sys_roof","sys_elec","sys_plumb","sys_siding","sys_windows","sys_hvac",
    "pre1940","hoa","arch","light",
]
SCALE5_CRITERIA = {"arch", "light"}


def format_address_for_app(address: str) -> str:
    """Convert 'Street, City, ST 00000' → 'Street, City ST' for the app's extractTown()."""
    m = re.match(r'^(.+),\s*([^,]+),\s*(MA|RI|CT|NH)\s*\d{0,5}\s*$', address, re.IGNORECASE)
    if m:
        return f"{m.group(1).strip()}, {m.group(2).strip()} {m.group(3).upper()}"
    return address


def new_app_listing(name: str, url: str, status: str = "for_sale") -> dict:
    scores = {c: (0 if c in SCALE5_CRITERIA else "unknown") for c in APP_CRITERIA}
    notes = "Status: Pending" if status == "pending" else ""
    return {"name": name, "url": url, "scores": scores, "notes": notes, "lat": None, "lng": None, "gut": None}


def supabase_get(key: str) -> str | None:
    try:
        resp = requests.get(
            f"{SB_URL}/rest/v1/hev_store?key=eq.{key}&select=value",
            headers=SB_HEADERS, timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0]["value"]
    except Exception as e:
        print(f"  Supabase read error: {e}")
    return None


def supabase_set(key: str, value: str) -> bool:
    try:
        resp = requests.post(
            f"{SB_URL}/rest/v1/hev_store",
            headers=SB_HEADERS,
            json={"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()},
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        print(f"  Supabase write error: {e}")
        return False


def sync_to_app(new_listings: list[dict], dry_run: bool = False) -> None:
    """Sync Zillow favorites into the Supabase-backed house evaluator app."""
    print("\nReading current listings from Supabase...")
    raw = supabase_get("hev_listings")
    existing = []
    if raw:
        try:
            existing = json.loads(raw)
            print(f"  {len(existing)} existing listings")
        except json.JSONDecodeError:
            print("  Could not parse existing listings, starting fresh")

    def norm(s: str) -> str:
        return re.sub(r'\W+', ' ', s).lower().strip()

    existing_by_name = {norm(l["name"]): l for l in existing}

    merged = []
    added = updated = 0

    for l in new_listings:
        app_name = format_address_for_app(l["address"])
        key = norm(app_name)
        if key in existing_by_name:
            ex = existing_by_name[key]
            # Preserve scores, gut, notes — just refresh URL and pending status
            ex["url"] = l.get("url", ex.get("url", ""))
            if l.get("status") == "pending" and "pending" not in (ex.get("notes") or "").lower():
                ex["notes"] = ("Status: Pending\n" + ex.get("notes", "")).strip()
            elif l.get("status") != "pending" and ex.get("notes", "").startswith("Status: Pending"):
                ex["notes"] = ex["notes"].replace("Status: Pending", "").strip()
            merged.append(ex)
            updated += 1
        else:
            merged.append(new_app_listing(app_name, l.get("url", ""), l.get("status", "for_sale")))
            added += 1

    pending_count = sum(1 for l in new_listings if l.get("status") == "pending")
    print(f"\nResult: {len(merged)} listings ({added} new, {updated} updated, {pending_count} pending)")
    print("\nListings:")
    for l in merged:
        flag = " [PENDING]" if "Status: Pending" in (l.get("notes") or "") else ""
        print(f"  {l['name']}{flag}")

    if dry_run:
        print("\n[dry-run] Not writing to Supabase.")
        return

    print("\nWriting to Supabase...")
    ok = supabase_set("hev_listings", json.dumps(merged, ensure_ascii=False))
    if ok:
        print(f"  Done — {len(merged)} listings synced to app")
    else:
        print("  Write failed")


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _to_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _make_id(address: str) -> str:
    h = hashlib.md5(address.lower().strip().encode()).hexdigest()[:8]
    return f"z{h}"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Zillow favorites and merge into data.json")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--cookies", help="Raw cookie string from browser DevTools")
    group.add_argument("--cookies-file", help="Path to a file containing the cookie string")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing anywhere")
    parser.add_argument("--sync-app", action="store_true", help="Sync to the Supabase-backed app (recommended)")
    parser.add_argument("--replace", action="store_true", help="(GitHub mode) Replace all listings with Zillow favorites")
    parser.add_argument("--debug", action="store_true", help="Verbose output")
    args = parser.parse_args()

    if args.cookies_file:
        with open(args.cookies_file) as f:
            cookie_str = f.read().strip()
    elif args.cookies:
        cookie_str = args.cookies
    else:
        print("No --cookies provided — checking Gist for saved cookies...")
        cookie_str = gist_get_cookies()
        if not cookie_str:
            print("  None found — launching browser to grab them automatically...")
            cookie_str = browser_get_cookies()
            gist_token = os.environ.get("GIST_TOKEN")
            if gist_token:
                if gist_set_cookies(cookie_str, gist_token):
                    print("  Cookies saved to Gist for next time.")
                else:
                    print("  Warning: couldn't save cookies to Gist (check GIST_TOKEN).")
            else:
                print("  Tip: set GIST_TOKEN env var to auto-save cookies to your Gist.")
        else:
            print("  Found cookies in Gist.")

    if not args.sync_app and not args.dry_run:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GIST_TOKEN")
        if not token:
            print("Error: set GITHUB_TOKEN or GIST_TOKEN environment variable, use --sync-app, or use --dry-run")
            sys.exit(1)
    else:
        token = None

    # Strategy 1: GraphQL
    print("Trying Zillow GraphQL API...")
    raw_homes = fetch_via_graphql(cookie_str, debug=args.debug)

    # Strategy 2: __NEXT_DATA__ from the saved-homes page
    if not raw_homes:
        print("GraphQL returned nothing — fetching saved-homes page...")
        html = fetch_saved_homes_page(cookie_str, debug=args.debug)
        if html:
            raw_homes = extract_from_next_data(html, debug=args.debug)
        if not raw_homes:
            print("\nCouldn't find any saved homes.")
            print("Make sure your cookies are fresh (copied from a recently loaded Zillow page).")
            sys.exit(1)

    print(f"Found {len(raw_homes)} raw homes from Zillow")

    # Normalize
    new_listings = []
    for raw in raw_homes:
        listing = normalize_zillow_home(raw)
        if listing:
            new_listings.append(listing)
        elif args.debug:
            print(f"  Skipped: {raw}")

    print(f"Normalized {len(new_listings)} listings")
    if not new_listings:
        print("Nothing to import.")
        sys.exit(0)

    # Print what we found
    pending = [l for l in new_listings if l.get("status") == "pending"]
    for l in new_listings:
        flag = " [PENDING]" if l.get("status") == "pending" else ""
        print(f"  {l['address']} — {l['price']} ({l['neighborhood_tier']}){flag}")
    if pending:
        print(f"\n  {len(pending)} listing(s) flagged as pending/under contract")

    if args.sync_app:
        sync_to_app(new_listings, dry_run=args.dry_run)
        return

    if args.dry_run:
        print("\n[dry-run] Not pushing to GitHub.")
        return

    # Legacy: push to GitHub data.json
    print("\nLoading current data.json from GitHub...")
    current = get_current_data()
    existing = current.get("listings", [])
    weights  = current.get("weights", {})
    print(f"  {len(existing)} existing listings")

    if args.replace:
        merged = new_listings
        print(f"Replace mode: keeping only {len(merged)} Zillow favorites")
        added = len(merged)
    else:
        merged, added = dedup_merge(new_listings, existing)
        print(f"After merge: {len(merged)} listings ({added:+d} new from Zillow)")

    print("\nPushing to GitHub...")
    push_to_github(token, {"listings": merged, "weights": weights})


if __name__ == "__main__":
    main()
