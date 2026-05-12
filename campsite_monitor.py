#!/usr/bin/env python3
"""
Campsite Availability Monitor v2
Watches Hipcamp, Airbnb, VRBO, Recreation.gov, Booking.com, Vacasa, and GlampingHub
for new listings near Ionia, MI for Aug 28-30, 2026.

Runs on macOS via launchd  OR  in the cloud via GitHub Actions — no Mac required.
"""

import base64
import json
import math
import os
import random
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth
_stealth = Stealth()

# ─────────────────────────────────────────────────────────────
#  Load .env file for local/Mac runs
#  (GitHub Actions injects secrets as real env vars — no .env needed there)
# ─────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────

GMAIL_ADDRESS      = "mattkaz@icloud.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL       = "mattkaz@icloud.com"
NOTIFY_EMAILS      = [NOTIFY_EMAIL, "jmartello@gmail.com"]  # all alert recipients

# GitHub — dashboard (Pages) + Issues alerts + state persistence
# In GitHub Actions: GITHUB_TOKEN is automatically provided by the runner (scoped to this repo)
# On Mac: set GH_TOKEN in your .env file
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
# DEPLOY_TOKEN is the personal access token used to push to the separate public dashboard repo.
# In Actions it comes from the GH_TOKEN secret; on Mac it falls back to GH_TOKEN from .env.
DEPLOY_TOKEN  = os.environ.get("GH_TOKEN", GITHUB_TOKEN)
GITHUB_REPO   = "mattckaz/campsite-monitor"   # private repo — code + Actions + Issues
DASHBOARD_REPO = "mattckaz/campsite-status"   # separate public repo — GitHub Pages only
DASHBOARD_URL = "https://mattckaz.github.io/campsite-status/"

# ntfy.sh — free push notifications  (https://ntfy.sh)
# Install the free ntfy app, subscribe to your topic, paste it here
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Monitor schedule
EXPIRY_DATE = date(2026, 7, 31)
TRIP_START  = date(2026, 8, 28)

# ~10-mile bounding box around Ionia, MI  (42.982743, -85.063192)
_NE_LAT, _NE_LNG = 43.1276, -84.8656
_SW_LAT, _SW_LNG = 42.8378, -85.2608
_CTR_LAT, _CTR_LNG = 42.982743, -85.063192

SEARCHES = [
    {
        "name": "Hipcamp — Ionia, MI (Aug 28-30)",
        "url": (
            "https://www.hipcamp.com/en-US/search"
            "?groupSize=2&arrive=2026-08-28&depart=2026-08-30"
            "&adults=2&children=0"
            f"&lat={_CTR_LAT}&lng={_CTR_LNG}&radius=10"
            "&placeId=place.160082156"
            "&placeName=Ionia%2C+Michigan%2C+United+States"
            "&q=Ionia&searchSource=search-again-home"
            "&mapLayers=nationalParkSystem&mapLayers=nationalForests"
            "&mapLayers=publicCampgrounds"
        ),
        "type": "hipcamp",
    },
    {
        "name": "Airbnb — Ionia, MI (Aug 28-30)",
        "url": (
            "https://www.airbnb.com/s/Ionia--Michigan--United-States/homes"
            "?checkin=2026-08-28&checkout=2026-08-30&adults=2&children=0"
            f"&ne_lat={_NE_LAT}&ne_lng={_NE_LNG}"
            f"&sw_lat={_SW_LAT}&sw_lng={_SW_LNG}"
            "&search_by_map=true"
        ),
        "type": "airbnb",
    },
    {
        "name": "VRBO — Ionia, MI (Aug 28-30)",
        "url": (
            "https://www.vrbo.com/search"
            "?destination=Ionia%2C+Michigan%2C+United+States"
            "&startDate=2026-08-28&endDate=2026-08-30&adults=2"
            f"&neLat={_NE_LAT}&neLng={_NE_LNG}"
            f"&swLat={_SW_LAT}&swLng={_SW_LNG}"
        ),
        "type": "vrbo",
    },
    {
        "name": "Recreation.gov — Near Ionia, MI (Aug 28-30)",
        "url": (
            f"https://www.recreation.gov/search"
            f"?lat={_CTR_LAT}&lng={_CTR_LNG}&radius=10&entity_type=campground"
        ),
        "type": "recreation_gov",
    },
    {
        # No property-type filter = hotels, motels, B&Bs, cabins, apartments, everything
        "name": "Booking.com — Ionia, MI (Aug 28-30)",
        "url": (
            "https://www.booking.com/searchresults.html"
            "?ss=Ionia%2C+Michigan%2C+United+States"
            "&checkin=2026-08-28&checkout=2026-08-30"
            "&group_adults=2&no_rooms=1"
            "&radius=16&radiusUnit=km"   # ~10 miles
            "&order=price"
        ),
        "type": "booking",
    },
    {
        "name": "Vacasa — Ionia, MI (Aug 28-30)",
        "url": (
            "https://www.vacasa.com/search"
            "?adults=2&children=0&pets=0"
            "&arrival=2026-08-28&departure=2026-08-30"
            "&location=Ionia%2C+Michigan"
        ),
        "type": "vacasa",
    },
    {
        "name": "GlampingHub — Near Ionia, MI (Aug 28-30)",
        "url": (
            "https://glampinghub.com/rentalsearch/"
            "?checkin=2026-08-28&checkout=2026-08-30&guests=2"
            "&q=Ionia%2C+Michigan%2C+United+States"
        ),
        "type": "glamping_hub",
    },
    {
        # Covers Modern Campground (1-100), Auxiliary, and Beechwood
        "name": "Michigan DNR — Ionia SRA + Beechwood (Aug 28-30)",
        "url": "https://midnrreservations.com/camping/search#resourceLocationId=-2147483575",
        "type": "michigan_dnr",
    },
    # NOTE: Campspot removed — their Angular SPA ignores URL date params in fresh
    # sessions and the API does not expose date-filtered availability publicly.
    # Alerts would always fire regardless of whether your specific dates are booked.
    # Alice Springs RV Park and Lakeside Resort are on Campspot; check manually at
    # campspot.com if the other sources show nothing.
    #
    # NOTE: Snow Lake Kampground removed — camping.com ASP.NET form does not
    # reliably return date-specific availability without a full browser session.
]

# Scrapers that use the API directly (no Playwright page needed)
API_SCRAPERS = {"recreation_gov"}

STATE_FILE  = Path(__file__).parent / "monitor_state.json"
LOG_FILE    = Path(__file__).parent / "monitor.log"
STATUS_FILE = Path(__file__).parent / "campsite_status.html"

# ─────────────────────────────────────────────────────────────
#  SCRAPERS — existing sites
# ─────────────────────────────────────────────────────────────

def _parse_distance_miles(dist_str: str) -> float:
    try:
        return float(dist_str.lower().replace("mi", "").strip())
    except Exception:
        return 9999.0


def scrape_hipcamp(page, url: str) -> list[dict]:
    graphql_edges = []

    def handle_response(resp):
        if "graphql/search" in resp.url:
            try:
                body = resp.json()
                edges = (body.get("data", {})
                             .get("lands", {})
                             .get("privateLands", {})
                             .get("edges", []))
                graphql_edges.extend(edges)
            except Exception:
                pass

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3_000)

    if graphql_edges:
        results = []
        for edge in graphql_edges:
            dist_str  = edge.get("distanceToSearchPlace", "9999mi")
            avail     = edge.get("availableCampsitesCount", 0)
            node      = edge.get("node", {})
            name      = node.get("name", "")
            masked_id = node.get("maskedId", "")
            if _parse_distance_miles(dist_str) > 10 or avail < 1 or not masked_id:
                continue
            results.append({
                "title": f"{name} ({dist_str} away, {avail} site{'s' if avail != 1 else ''} avail.)",
                "href":  f"https://www.hipcamp.com/en-US/search?q={masked_id}",
                "masked_id": masked_id,
            })
        cards = page.query_selector_all('a[href*="/en-US/land/"]')
        for card in cards:
            href = card.get_attribute("href") or ""
            for r in results:
                if r.get("masked_id", "") in href:
                    r["href"] = f"https://www.hipcamp.com{href.split('?')[0]}"
                    break
        return results

    results = []
    UNAVAIL = {"not available", "unavailable", "no availability", "sold out",
               "fully booked", "no sites available", "no spots available", "closed"}
    cards = page.query_selector_all('a[href*="/en-US/land/"]')
    seen  = set()
    for card in cards:
        href = card.get_attribute("href") or ""
        if not href or href in seen:
            continue
        seen.add(href)
        try:
            card_text = card.inner_text().strip()
            title = card_text.split("\n")[0][:120]
        except Exception:
            card_text, title = "", href
        if any(sig in card_text.lower() for sig in UNAVAIL):
            continue
        if title:
            results.append({"title": title, "href": f"https://www.hipcamp.com{href}"})
    if not results:
        body_text = page.inner_text("body").lower()
        if any(s in body_text for s in ["no places", "0 places", "no results"]):
            return []
        results = [{"title": "(listings detected — check manually)", "href": url}]
    return results


def scrape_airbnb(page, url: str) -> list[dict]:
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(4_000)
    body_text = page.inner_text("body").lower()
    NO_RESULTS = ["no homes in this area", "no exact matches", "no results",
                  "0 homes", "adjust your search",
                  "try changing or removing some of your filters"]
    if any(s in body_text for s in NO_RESULTS):
        return []
    results = []
    for sel in ['[data-testid="card-container"]', '[itemprop="itemListElement"]',
                'div[class*="c4mnd7m"]']:
        cards = page.query_selector_all(sel)
        if cards:
            for card in cards[:20]:
                try:
                    text = card.inner_text().strip()[:120]
                    if not text:
                        continue
                    # Try to grab a direct link to the individual room listing
                    link = (
                        card.query_selector('a[href*="/rooms/"]') or
                        card.query_selector("a[href]")
                    )
                    href = link.get_attribute("href") if link else url
                    if href and href.startswith("/rooms/"):
                        href = "https://www.airbnb.com" + href
                    # Strip tracking params; append our dates so the link lands on
                    # the correct availability calendar
                    if href and "/rooms/" in href:
                        href = (href.split("?")[0]
                                + "?check_in=2026-08-28&check_out=2026-08-30&adults=2")
                    results.append({"title": text, "href": href or url})
                except Exception:
                    pass
            break
    return results


def _human_delay(lo=800, hi=2200):
    """Sleep for a random human-like interval (ms converted to seconds)."""
    import time
    time.sleep(random.randint(lo, hi) / 1000)


def _simulate_human_scroll(page):
    """Scroll down gradually, then back up, like a human scanning results."""
    try:
        page.evaluate("""() => {
            return new Promise(resolve => {
                let total = document.body.scrollHeight;
                let step  = Math.floor(total / 6);
                let pos   = 0;
                let t = setInterval(() => {
                    pos = Math.min(pos + step + Math.floor(Math.random()*120), total);
                    window.scrollTo(0, pos);
                    if (pos >= total) { clearInterval(t); resolve(); }
                }, 350 + Math.floor(Math.random()*200));
            });
        }""")
        _human_delay(400, 900)
        page.evaluate("window.scrollTo(0, 0)")
        _human_delay(300, 700)
    except Exception:
        pass


def scrape_vrbo(page, url: str) -> list[dict]:
    """
    VRBO scraper with layered anti-bot hardening:
      1. Warm-up visit to vrbo.com homepage first
      2. Random human-like delays throughout
      3. Mouse movement simulation before navigating to search
      4. Scrolling simulation after page load
      5. Consent/cookie dialog dismissal
      6. Multiple selector strategies for results
      7. Falls back to Expedia API (VRBO parent) on bot-detect
    """
    BOT_SIGNALS = [
        "show us your human side", "can't tell if you're a human or a bot",
        "human or a bot", "access denied", "please verify you are a human",
        "enable javascript and cookies", "verify your identity", "robot",
        "unusual traffic", "403 forbidden",
    ]
    NO_RESULTS = [
        "we don't have any available properties",
        "no properties found", "no results found",
        "try changing your dates", "0 properties",
    ]

    def _bot_blocked(text: str) -> bool:
        t = text.lower()
        return any(s in t for s in BOT_SIGNALS)

    def _no_results(text: str) -> bool:
        t = text.lower()
        return any(s in t for s in NO_RESULTS)

    def _extract_cards(pg, src_url) -> list[dict]:
        results = []
        selectors = [
            '[data-stid="property-listing"]',
            '[data-stid="open-hotel-information"]',
            'li[class*="PropertyCardstyles"]',
            'div[data-testid="property-card"]',
            '[class*="uitk-card"][class*="property"]',
            'li[class*="property"]',
        ]
        for sel in selectors:
            cards = pg.query_selector_all(sel)
            if cards:
                for card in cards[:20]:
                    try:
                        t = card.inner_text().strip()[:140]
                        if t:
                            # Try to find a real link inside the card
                            a_el  = card.query_selector("a[href]")
                            href  = a_el.get_attribute("href") if a_el else ""
                            if href and not href.startswith("http"):
                                href = "https://www.vrbo.com" + href
                            results.append({"title": t, "href": href or src_url})
                    except Exception:
                        pass
                break
        return results

    # ── Step 1: warm up on the VRBO homepage ──────────────────────────────────
    try:
        page.goto("https://www.vrbo.com", wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        pass
    _human_delay(1500, 3000)

    # Dismiss cookie/consent dialog if present
    for btn_sel in ['button[data-stid*="accept"]', 'button[id*="onetrust-accept"]',
                    'button[aria-label*="Accept"]', '#onetrust-accept-btn-handler']:
        try:
            btn = page.query_selector(btn_sel)
            if btn and btn.is_visible():
                btn.click()
                _human_delay(600, 1200)
                break
        except Exception:
            pass

    # Move mouse around the page like a real user
    try:
        w = page.viewport_size or {"width": 1280, "height": 900}
        for _ in range(3):
            page.mouse.move(
                random.randint(100, w["width"] - 100),
                random.randint(100, w["height"] - 100),
            )
            _human_delay(200, 500)
    except Exception:
        pass

    # ── Step 2: navigate to the search URL ───────────────────────────────────
    _human_delay(800, 1800)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeout:
        pass
    _human_delay(2000, 4000)

    # Try to wait for actual property cards before giving up
    for sel in ['[data-stid="property-listing"]', 'li[class*="PropertyCard"]',
                '[data-testid="property-card"]']:
        try:
            page.wait_for_selector(sel, timeout=8_000)
            break
        except Exception:
            pass

    _simulate_human_scroll(page)
    _human_delay(800, 1500)

    body_text = page.inner_text("body")

    if _bot_blocked(body_text):
        # ── Step 3: fallback — try Expedia/VRBO via a lighter API endpoint ──
        try:
            api_url = (
                "https://www.vrbo.com/serp/g"
                "?adultsCount=2&petIncluded=false"
                "&filterByTotalPrice=false"
                f"&neLat={_NE_LAT}&neLong={_NE_LNG}"
                f"&swLat={_SW_LAT}&swLong={_SW_LNG}"
                "&startDate=2026-08-28&endDate=2026-08-30"
                "&mapBounds=true&resultsStartingIndex=0&resultsSize=40"
                "&sort=RECOMMENDED&theme=VRBO"
            )
            page.goto(api_url, wait_until="domcontentloaded", timeout=40_000)
            _human_delay(2500, 4000)
            body_text = page.inner_text("body")
        except Exception:
            pass
        if _bot_blocked(body_text):
            return None   # Still blocked — give up gracefully

    if _no_results(body_text):
        return []

    return _extract_cards(page, url)

# ─────────────────────────────────────────────────────────────
#  SCRAPERS — new sites
# ─────────────────────────────────────────────────────────────

def scrape_recreation_gov(url: str) -> list[dict]:
    """
    Queries Recreation.gov's public API for campground availability.
    No browser needed — pure HTTP. Checks for sites available both
    Aug 28 AND Aug 29 (the two nights of the stay).
    """
    API_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    results = []
    try:
        search_url = (
            f"https://www.recreation.gov/api/search"
            f"?lat={_CTR_LAT}&lng={_CTR_LNG}&radius=10"
            f"&entity_type=campground&size=50"
        )
        req = urllib.request.Request(search_url, headers=API_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        campgrounds = data.get("results", [])
        log(f"  Recreation.gov: {len(campgrounds)} campground(s) within 10mi")

        for cg in campgrounds:
            entity_id = cg.get("entity_id", "")
            name      = cg.get("name", "Unknown")
            if not entity_id:
                continue

            avail_url = (
                f"https://www.recreation.gov/api/camps/availability"
                f"/campground/{entity_id}/month"
                f"?start_date=2026-08-01T00:00:00.000Z"
            )
            try:
                req2 = urllib.request.Request(avail_url, headers=API_HEADERS)
                with urllib.request.urlopen(req2, timeout=15) as r2:
                    avail_data = json.loads(r2.read())
            except Exception:
                continue

            available_count = 0
            for site_info in avail_data.get("campsites", {}).values():
                avails = site_info.get("availabilities", {})
                n1 = avails.get("2026-08-28T00:00:00Z", "")
                n2 = avails.get("2026-08-29T00:00:00Z", "")
                if n1 == "Available" and n2 == "Available":
                    available_count += 1

            if available_count > 0:
                results.append({
                    "title": f"{name} ({available_count} site{'s' if available_count != 1 else ''} avail.)",
                    "href":  f"https://www.recreation.gov/camping/campgrounds/{entity_id}",
                })

    except Exception as e:
        log(f"  Recreation.gov error: {e}")

    return results


def scrape_booking(page, url: str) -> list[dict]:
    """Scrape Booking.com vacation rentals near Ionia, MI."""
    try:
        page.goto("https://www.booking.com", wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2_000)
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(5_000)

    body_text = page.inner_text("body").lower()
    BOT_SIGNALS = ["are you a human", "captcha", "access denied", "security check"]
    if any(s in body_text for s in BOT_SIGNALS):
        return None

    NO_RESULTS = ["no properties found", "no results", "0 properties",
                  "we couldn't find any results", "we have no availability"]
    if any(s in body_text for s in NO_RESULTS):
        return []

    results = []
    for sel in ['[data-testid="property-card"]', '[data-testid="property-card-container"]',
                'div[class*="PropertyCard"]']:
        cards = page.query_selector_all(sel)
        if cards:
            for card in cards[:20]:
                try:
                    text = card.inner_text().strip()[:120]
                    link = (card.query_selector("a[href*='/hotel/']") or
                            card.query_selector("a[data-testid='title-link']") or
                            card.query_selector("a"))
                    href = link.get_attribute("href") if link else url
                    if href and not href.startswith("http"):
                        href = "https://www.booking.com" + href
                    if text:
                        results.append({"title": text, "href": (href or url).split("?")[0]})
                except Exception:
                    pass
            break

    return results


def scrape_vacasa(page, url: str) -> list[dict]:
    """Scrape Vacasa vacation rentals near Ionia, MI."""
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(5_000)

    body_text = page.inner_text("body").lower()
    NO_RESULTS = ["no properties", "no results", "no rentals", "0 homes",
                  "couldn't find any", "no vacation rentals found"]
    if any(s in body_text for s in NO_RESULTS):
        return []

    results = []
    for sel in ['[data-testid="listing-card"]', '[class*="PropertyCard"]',
                '[class*="listing-card"]', 'article[class*="card"]',
                'a[href*="/vacation-rentals/"]']:
        cards = page.query_selector_all(sel)
        if cards:
            for card in cards[:20]:
                try:
                    text = card.inner_text().strip()[:120]
                    link = card if card.get_attribute("href") else card.query_selector("a")
                    href = (link.get_attribute("href") if link else None) or url
                    if href and not href.startswith("http"):
                        href = "https://www.vacasa.com" + href
                    if text:
                        results.append({"title": text, "href": href or url})
                except Exception:
                    pass
            break

    if not results and "vacasa" not in body_text:
        return None
    return results


def scrape_glamping_hub(page, url: str) -> list[dict]:
    """Scrape GlampingHub for glamping sites near Ionia, MI."""
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(5_000)

    body_text = page.inner_text("body").lower()
    BOT_SIGNALS = ["access denied", "captcha", "are you human"]
    if any(s in body_text for s in BOT_SIGNALS):
        return None

    NO_RESULTS = ["no results", "no glamping", "0 results", "no properties found",
                  "couldn't find", "no listings"]
    if any(s in body_text for s in NO_RESULTS):
        return []

    results = []
    for sel in ['[class*="listing-card"]', '[class*="property-card"]',
                '[class*="SearchResult"]', 'a[href*="/glamping/"]']:
        cards = page.query_selector_all(sel)
        if cards:
            for card in cards[:20]:
                try:
                    text = card.inner_text().strip()[:120]
                    link = card if card.get_attribute("href") else card.query_selector("a")
                    href = (link.get_attribute("href") if link else None) or url
                    if href and not href.startswith("http"):
                        href = "https://glampinghub.com" + href
                    if text and len(text) > 10:
                        results.append({"title": text, "href": href or url})
                except Exception:
                    pass
            if results:
                break

    return results


def scrape_michigan_dnr(page, url: str) -> list[dict]:
    """
    Queries Michigan DNR's GoingToCamp API for Ionia Recreation Area campsite availability.
    Checks Modern Campground (sites 1-50, 51-100), Auxiliary, and Beechwood for Aug 28-30.
    Availability code 0 = open/available; 1 = reserved; 3 = closed; 4-5 = not reservable.
    resourceLocationId -2147483575 = Ionia Recreation Area

    Makes API calls via page.evaluate() (fetch from inside the browser) so the request
    carries the full browser context — cookies, headers, fingerprint — bypassing 403s.
    """
    BOOK_URL = "https://midnrreservations.com/camping/search#resourceLocationId=-2147483575"
    RESOURCE_LOCATION_ID = -2147483575
    MAPS = {
        "Modern Campground (sites 1–50)":   -2147483378,
        "Modern Campground (sites 51–100)": -2147483377,
        "Auxiliary Campground":             -2147483373,
        "Beechwood Campground":             -2147482934,
    }

    # Navigate to establish a real browser session
    try:
        page.goto("https://midnrreservations.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        log(f"  Michigan DNR: failed to load session page — {e}")
        return []

    results = []
    for name, map_id in MAPS.items():
        params = (
            f"mapId={map_id}"
            f"&resourceLocationId={RESOURCE_LOCATION_ID}"
            f"&startDate=2026-08-28&endDate=2026-08-30"
            f"&bookingCategoryId=0&partySize=2"
            f"&equipmentCategoryId=-32768&subEquipmentCategoryId=-32768"
            f"&numEquipment=1&isReserving=true&filterData=%5B%5D"
        )
        avail_url = f"https://midnrreservations.com/api/availability/map?{params}"
        try:
            # Make the fetch request FROM INSIDE the browser — carries full session context
            data = page.evaluate("""async (url) => {
                const r = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Referer': 'https://midnrreservations.com/',
                        'sec-fetch-dest': 'empty',
                        'sec-fetch-mode': 'cors',
                        'sec-fetch-site': 'same-origin'
                    }
                });
                if (!r.ok) return {error: r.status};
                return await r.json();
            }""", avail_url)

            if data.get("error"):
                log(f"  Michigan DNR error ({name}): HTTP {data['error']}")
                continue

            ra = data.get("resourceAvailabilities", {})
            open_count = sum(
                1 for arr in ra.values()
                if any(a.get("availability") == 0 for a in arr)
            )
            if open_count > 0:
                results.append({
                    "title": (f"Ionia SRA — {name} "
                              f"({open_count} site{'s' if open_count != 1 else ''} avail.)"),
                    "href":  BOOK_URL,
                })
                log(f"  Michigan DNR: {open_count} open site(s) in {name}")
            else:
                log(f"  Michigan DNR: {name} — no availability")
        except Exception as e:
            log(f"  Michigan DNR error ({name}): {e}")

    return results


def scrape_campspot(page, url: str) -> list[dict]:
    """
    Scrape a Campspot park page for site availability.

    LIMITATION: Campspot's Angular app ignores URL date params in fresh sessions
    and the gator-core API does not expose date-filtered availability publicly.
    This scraper detects whether the park is listed on Campspot with bookable
    site types, but CANNOT verify if those sites are available for your specific
    dates. Results are labeled accordingly. The user should click Search and
    manually enter dates to confirm availability.
    """
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(5_000)

    body_text = page.inner_text("body")
    body_lower = body_text.lower()

    BOT_SIGNALS = ["access denied", "captcha", "are you human", "verify you are"]
    if any(s in body_lower for s in BOT_SIGNALS):
        return None

    # If the page explicitly says 0 available, trust it
    if "0 available sites" in body_lower:
        return []

    import re
    m = re.search(r'(\d+)\s+available\s+sites', body_lower)
    if not m or int(m.group(1)) == 0:
        if "available" not in body_lower:
            return []
        return [{"title": "Sites on Campspot — verify dates manually", "href": url}]

    # Use filter-panel counts: "All Sites 168 Lodging 10 RV Sites 158 Tent Sites 8"
    # These reflect total park capacity, NOT date-filtered availability.
    results = []
    fp = re.search(
        r'All Sites\s+\d+\s+Lodging\s+(\d+)\s+RV Sites\s+(\d+)\s+Tent Sites\s+(\d+)',
        body_text, re.IGNORECASE
    )
    if fp:
        lodging, rv, tent = int(fp.group(1)), int(fp.group(2)), int(fp.group(3))
        if lodging > 0:
            results.append({"title": f"Lodging ({lodging} sites — verify dates)", "href": url})
        if rv > 0:
            results.append({"title": f"RV Sites ({rv} sites — verify dates)", "href": url})
        if tent > 0:
            results.append({"title": f"Tent Sites ({tent} sites — verify dates)", "href": url})

    if not results:
        total = int(m.group(1))
        results.append({
            "title": f"{total} site{'s' if total != 1 else ''} on Campspot — verify dates",
            "href":  url,
        })
    return results


def scrape_snow_lake(page, url: str) -> list[dict]:
    """
    Scrape Snow Lake Kampground via camping.com (ASP.NET reservation system).
    Fills in Aug 28/30 dates, submits the search form, and reads availability results.
    """
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(3_000)

    body_text = page.inner_text("body").lower()
    BOT_SIGNALS = ["access denied", "captcha", "are you human"]
    if any(s in body_text for s in BOT_SIGNALS):
        return None

    # Try filling the date form and submitting
    try:
        for ci_sel in ["#txtCheckInDate", "input[id*='CheckIn']", "input[name*='CheckIn']"]:
            ci = page.query_selector(ci_sel)
            if ci:
                ci.triple_click()
                ci.type("08/28/2026", delay=50)
                break
        for co_sel in ["#txtCheckOutDate", "input[id*='CheckOut']", "input[name*='CheckOut']"]:
            co = page.query_selector(co_sel)
            if co:
                co.triple_click()
                co.type("08/30/2026", delay=50)
                break
        # Submit
        for btn_sel in ["input[type='submit']", "button[type='submit']",
                        "a[id*='Search']", "input[id*='Search']"]:
            btn = page.query_selector(btn_sel)
            if btn and btn.is_visible():
                btn.click()
                break
        page.wait_for_timeout(6_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        body_text = page.inner_text("body").lower()
    except Exception:
        pass

    NO_RESULTS = ["no sites available", "no availability", "0 sites", "no results",
                  "no campsites", "unavailable", "nothing available"]
    if any(s in body_text for s in NO_RESULTS):
        return []

    AVAIL_SIGS = ["add to cart", "select site", "book now", "available sites",
                  "site type", "nightly rate", "per night"]
    if any(s in body_text for s in AVAIL_SIGS):
        # Try to count site entries
        import re
        counts = re.findall(r'(\d+)\s*(?:site|space|spot)s?\s*available', body_text)
        total = sum(int(c) for c in counts) if counts else None
        title = (f"{total} site{'s' if total != 1 else ''} available"
                 if total else "Sites may be available — check booking page")
        return [{"title": title, "href": url}]

    # No clear availability signal — return empty so we don't false-positive
    return []


SCRAPERS = {
    "hipcamp":        scrape_hipcamp,
    "airbnb":         scrape_airbnb,
    "vrbo":           scrape_vrbo,
    "recreation_gov": scrape_recreation_gov,
    "booking":        scrape_booking,
    "vacasa":         scrape_vacasa,
    "glamping_hub":   scrape_glamping_hub,
    "michigan_dnr":   scrape_michigan_dnr,
    "campspot":       scrape_campspot,
    "snow_lake":      scrape_snow_lake,
}

# ─────────────────────────────────────────────────────────────
#  STATE PERSISTENCE
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─────────────────────────────────────────────────────────────
#  STATUS PAGE
# ─────────────────────────────────────────────────────────────

def write_status_page(state: dict):
    log_lines = []
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            log_lines = f.readlines()[-40:]

    total_count = sum(state.get(s["name"], {}).get("count", 0) for s in SEARCHES)
    all_checks  = [state.get(s["name"], {}).get("last_checked", "") for s in SEARCHES]
    all_checks  = [x for x in all_checks if x]

    # Timestamps are stored as UTC ISO strings; JS will reformat to local time.
    last_check_utc = max(all_checks) if all_checks else ""
    now_utc        = datetime.now(timezone.utc).isoformat()

    days_to_trip = (TRIP_START - date.today()).days
    if days_to_trip > 1:
        days_to_trip_str, days_to_trip_sub = str(days_to_trip), "days away"
    elif days_to_trip == 1:
        days_to_trip_str, days_to_trip_sub = "Tomorrow", ""
    elif days_to_trip == 0:
        days_to_trip_str, days_to_trip_sub = "Today!", ""
    else:
        days_to_trip_str, days_to_trip_sub = "Past", ""

    days_left = (EXPIRY_DATE - date.today()).days

    # Build alert history HTML
    alert_history = list(reversed(state.get("alert_history", [])))
    if alert_history:
        history_rows = ""
        for h in alert_history[:20]:
            try:
                dt = datetime.fromisoformat(h["found_at"]).strftime("%b %d at %I:%M %p UTC")
            except Exception:
                dt = h.get("found_at", "")[:16]
            history_rows += f"""
            <div style="padding:10px 0;border-bottom:1px solid #f3f4f6;">
              <div style="font-size:13px;font-weight:600;color:#111827;">
                <a href="{h['href']}" target="_blank" style="color:#16a34a;text-decoration:none;">
                  {h['title'][:90]}
                </a>
              </div>
              <div style="font-size:11px;color:#9ca3af;margin-top:2px;">
                {h['source']} &middot; Found {dt}
              </div>
            </div>"""
        alert_history_html = f"""
        <div class="section-header" style="margin-top:32px">
          <h2 class="section-title">&#x2605; Listings Found</h2>
          <p class="section-sub">Alerts that were sent — listings may no longer be available</p>
        </div>
        <div class="card">{history_rows}</div>"""
    else:
        alert_history_html = ""

    rows_html = ""
    for search in SEARCHES:
        name  = search["name"]
        url   = search["url"]
        info  = state.get(name, {})
        count = info.get("count", 0)
        keys  = info.get("keys", [])
        site_name   = name.split("--")[0].strip() if "--" in name else name.split("-")[0].strip()
        # Handle em dash in display name
        parts = name.split("--")
        if len(parts) >= 2:
            site_name   = parts[0].strip()
            search_desc = parts[1].strip()
        else:
            site_name   = name
            search_desc = ""
        # Pass the raw UTC ISO string; JS will render it in the user's local tz
        checked_utc = info.get("last_checked", "")

        has_results = count > 0
        badge_class = "badge badge-green" if has_results else "badge badge-gray"
        badge_dot   = "●" if has_results else "○"
        badge_text  = f"{count} listing{'s' if count != 1 else ''}"
        card_class  = "search-card has-results" if has_results else "search-card"

        listing_items = "".join(
            f'<li class="listing-item"><span>{k[:100]}</span></li>' for k in keys[:8])
        listing_block = (f'<ul class="listing-list">{listing_items}</ul>'
                         if listing_items else
                         '<p class="no-listings">No listings found yet.</p>')

        rows_html += f"""
    <div class="{card_class}">
      <div class="search-card-header">
        <div class="search-info">
          <div class="search-name">{site_name}</div>
          <div class="search-meta">{search_desc} &nbsp;&middot;&nbsp; Checked <time class="local-time" data-utc="{checked_utc}" data-fmt="time">–</time></div>
        </div>
        <div class="search-right">
          <span class="{badge_class}">{badge_dot} {badge_text}</span>
          <a href="{url.replace('&', '&amp;')}" target="_blank" class="view-link">Search</a>
        </div>
      </div>
      {listing_block}
    </div>"""

    log_html = ""
    for line in reversed(log_lines):
        line = line.strip()
        if not line:
            continue
        ll = line.lower()
        cls = ("log-line error"   if "error"   in ll else
               "log-line warn"    if "warning" in ll else
               "log-line success" if any(x in ll for x in ("email sent", "deploy", "listing")) else
               "log-line sep"     if "---" in line else
               "log-line")
        log_html += f'<div class="{cls}">{line}</div>\n'
    if not log_html:
        log_html = '<div class="log-line">No log entries yet.</div>'

    expiry_chip = ""
    if days_left <= 0:
        expiry_chip = '<span class="chip chip-warn">Monitor expired</span>'
    elif days_left <= 14:
        expiry_chip = f'<span class="chip chip-warn">Expires in {days_left} days</span>'

    last_run = state.get("_last_run", {})
    if last_run:
        errors   = last_run.get("errors", [])
        bot_hits = last_run.get("bot_hits", [])
        if last_run.get("ok"):
            health_chip = '<span class="chip">&#10003; Last run healthy</span>'
        elif errors and bot_hits:
            health_chip = (f'<span class="chip chip-warn">&#9888; Errors: {", ".join(errors)}'
                           f' &middot; Bot blocked: {", ".join(bot_hits)}</span>')
        elif errors:
            health_chip = f'<span class="chip chip-warn">&#9888; Errors: {", ".join(errors)}</span>'
        else:
            health_chip = f'<span class="chip chip-warn">&#128274; Bot blocked: {", ".join(bot_hits)}</span>'
    else:
        health_chip = ""

    css = """
      :root {
        --green:#16a34a;--green-100:#dcfce7;--green-700:#15803d;
        --gray-100:#f3f4f6;--gray-400:#9ca3af;--gray-500:#6b7280;
        --gray-700:#374151;--gray-800:#1f2937;--gray-900:#111827;
        --shadow-md:0 4px 8px -2px rgba(0,0,0,.08),0 2px 4px -2px rgba(0,0,0,.04);
      }
      *{box-sizing:border-box;margin:0;padding:0}
      body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",sans-serif;
           background:#eef2f7;color:var(--gray-900);min-height:100vh}
      .hero{background:linear-gradient(135deg,#052e16 0%,#064e3b 55%,#065f46 100%);
            padding:44px 24px 64px;position:relative;overflow:hidden}
      .hero::before{content:'';position:absolute;top:-60px;right:-60px;width:360px;height:360px;
                    background:radial-gradient(circle,rgba(255,255,255,.07) 0%,transparent 65%);
                    border-radius:50%;pointer-events:none}
      .hero-inner{max-width:900px;margin:auto;position:relative;z-index:1}
      .hero h1{font-size:30px;font-weight:700;color:#fff;letter-spacing:-.5px;margin-bottom:8px}
      .hero-sub{color:rgba(255,255,255,.65);font-size:14px;margin-bottom:22px}
      .hero-chips{display:flex;gap:8px;flex-wrap:wrap}
      .chip{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.11);
            border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.88);border-radius:20px;
            padding:5px 14px;font-size:12px;font-weight:500}
      .chip-warn{background:rgba(220,38,38,.18);border-color:rgba(220,38,38,.35);color:#fca5a5}
      .live-dot{width:7px;height:7px;background:#4ade80;border-radius:50%;
                animation:pulse 2.2s ease-in-out infinite}
      @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.45)}}
      .container{max-width:900px;margin:-26px auto 0;padding:0 24px 56px;position:relative;z-index:2}
      .stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px}
      .stat-card{background:#fff;border-radius:14px;padding:18px 20px 16px;box-shadow:var(--shadow-md)}
      .stat-label{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;
                  color:var(--gray-400);margin-bottom:6px}
      .stat-value{font-size:26px;font-weight:700;color:var(--gray-900);line-height:1.1}
      .stat-value.sm{font-size:15px;font-weight:600}
      .stat-value.green{color:var(--green)}
      .stat-sub{font-size:11px;color:var(--gray-400);margin-top:2px}
      .section-header{margin:28px 0 12px}
      .section-title{font-size:15px;font-weight:600;color:var(--gray-800)}
      .section-sub{font-size:12px;color:var(--gray-400);margin-top:3px}
      .search-card{background:#fff;border-radius:14px;box-shadow:var(--shadow-md);
                   padding:20px 22px;margin-bottom:12px;border-left:4px solid var(--gray-100);
                   transition:border-color .2s,box-shadow .2s}
      .search-card.has-results{border-left-color:var(--green);
                                box-shadow:0 4px 14px rgba(22,163,74,.13)}
      .search-card-header{display:flex;justify-content:space-between;
                           align-items:flex-start;gap:16px}
      .search-name{font-size:15px;font-weight:600;color:var(--gray-800);margin-bottom:3px}
      .search-meta{font-size:12px;color:var(--gray-400)}
      .search-right{display:flex;align-items:center;gap:10px;flex-shrink:0}
      .badge{display:inline-flex;align-items:center;gap:5px;border-radius:20px;
             padding:4px 12px;font-size:12px;font-weight:600;white-space:nowrap}
      .badge-green{background:var(--green-100);color:var(--green-700)}
      .badge-gray{background:var(--gray-100);color:var(--gray-500)}
      .view-link{font-size:12px;color:var(--green);text-decoration:none;font-weight:500}
      .view-link:hover{text-decoration:underline}
      .listing-list{list-style:none;margin-top:14px;padding-top:12px;
                    border-top:1px solid var(--gray-100)}
      .listing-item{display:flex;align-items:center;gap:10px;padding:7px 0;font-size:13px;
                    color:var(--gray-700);border-bottom:1px solid var(--gray-100)}
      .listing-item:last-child{border-bottom:none}
      .listing-item::before{content:'';width:6px;height:6px;background:var(--green);
                             border-radius:50%;flex-shrink:0}
      .no-listings{margin-top:12px;font-size:13px;color:var(--gray-400);font-style:italic}
      .card{background:#fff;border-radius:14px;box-shadow:var(--shadow-md);
            padding:20px 22px;margin-bottom:16px}
      .log-terminal{background:#0d1117;border-radius:8px;padding:16px;max-height:300px;
                    overflow-y:auto;font-family:"SF Mono","Cascadia Code","Fira Code",
                    ui-monospace,monospace;scrollbar-width:thin;scrollbar-color:#30363d transparent}
      .log-line{font-size:12px;line-height:1.75;color:#8b949e}
      .log-line.error{color:#f85149}.log-line.warn{color:#d29922}
      .log-line.success{color:#3fb950}.log-line.sep{color:#2d3748}
      .footer{text-align:center;font-size:12px;color:var(--gray-400);padding-bottom:36px}
      .footer a{color:var(--green);text-decoration:none}
      @media(max-width:600px){
        .hero{padding:28px 16px 52px}.hero h1{font-size:24px}
        .container{padding:0 16px 36px}
        .stats-grid{grid-template-columns:repeat(2,1fr)}
        .search-card-header{flex-direction:column}
        .search-right{width:100%;justify-content:flex-end}
      }
    """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="7200">
  <title>Campsite Monitor</title>
  <style>{css}</style>
</head>
<body>
<header class="hero">
  <div class="hero-inner">
    <h1>&#127957;&#65039; Campsite Monitor</h1>
    <p class="hero-sub">Watching 8 platforms &#183; within 10 miles of Ionia, MI &#183; Aug 28-30, 2026</p>
    <div class="hero-chips">
      <span class="chip"><span class="live-dot"></span>Monitoring active</span>
      <span class="chip">Checks every 2 hours</span>
      <span class="chip">Updated <time class="local-time" data-utc="{now_utc}" data-fmt="datetime">–</time></span>
      {health_chip}
      {expiry_chip}
    </div>
  </div>
</header>
<main class="container">
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Total Listings</div>
      <div class="stat-value green">{total_count}</div>
      <div class="stat-sub">across 8 sites</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Last Check</div>
      <div class="stat-value sm"><time class="local-time" data-utc="{last_check_utc}" data-fmt="time">–</time></div>
      <div class="stat-sub">runs every 2 hours</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Days to Trip</div>
      <div class="stat-value">{days_to_trip_str}</div>
      <div class="stat-sub">{days_to_trip_sub}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Monitor Expires</div>
      <div class="stat-value sm">{EXPIRY_DATE.strftime("%b %d, %Y")}</div>
      <div class="stat-sub">{days_left} days remaining</div>
    </div>
  </div>
  <div class="section-header">
    <h2 class="section-title">Active Searches</h2>
    <p class="section-sub">Email + GitHub Issue created when new listings appear</p>
  </div>
  {rows_html}
  {alert_history_html}
  <div class="section-header" style="margin-top:32px">
    <h2 class="section-title">Recent Activity</h2>
  </div>
  <div class="card">
    <div class="log-terminal">{log_html}</div>
  </div>
</main>
<footer class="footer">
  <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>
  &nbsp;&#183;&nbsp; Expires {EXPIRY_DATE.strftime("%B %d, %Y")}
  &nbsp;&#183;&nbsp; {days_left} days remaining
  &nbsp;&#183;&nbsp; <a href="https://github.com/{GITHUB_REPO}/issues">GitHub Issues</a>
</footer>
<script>
  // Convert every [data-utc] element to the viewer's local timezone.
  // data-fmt="time"     → "Mar 18 - 2:38 PM"  (used in cards + stat)
  // data-fmt="datetime" → "Mar 18, 2026 at 2:38 PM"  (used in hero chip)
  (function() {{
    document.querySelectorAll('time.local-time[data-utc]').forEach(function(el) {{
      var raw = el.dataset.utc;
      if (!raw) return;
      try {{
        var d = new Date(raw);
        if (isNaN(d)) return;
        var fmt = el.dataset.fmt;
        if (fmt === 'datetime') {{
          el.textContent = d.toLocaleString(undefined, {{
            month: 'short', day: 'numeric', year: 'numeric',
            hour: 'numeric', minute: '2-digit'
          }});
        }} else {{
          el.textContent = d.toLocaleString(undefined, {{
            month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit'
          }});
        }}
      }} catch(e) {{}}
    }});
  }})();
</script>
</body>
</html>"""

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        f.write(html)

# ─────────────────────────────────────────────────────────────
#  GITHUB — dashboard deploy + Issues alerts
# ─────────────────────────────────────────────────────────────

def _gh_request(path: str, method: str = "GET", data: dict = None, token: str = None):
    headers = {
        "Authorization": f"Bearer {token or GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(data).encode() if data else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def deploy_to_github() -> str:
    """Push the status page HTML to the public dashboard repo (GitHub Pages)."""
    if not DEPLOY_TOKEN or not DASHBOARD_REPO:
        return ""
    try:
        with open(STATUS_FILE, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        sha = ""
        status, existing = _gh_request(
            f"/repos/{DASHBOARD_REPO}/contents/index.html", token=DEPLOY_TOKEN)
        if status == 200:
            sha = existing.get("sha", "")
        payload = {
            "message": f"Update dashboard {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": content,
        }
        if sha:
            payload["sha"] = sha
        status, result = _gh_request(
            f"/repos/{DASHBOARD_REPO}/contents/index.html", method="PUT",
            data=payload, token=DEPLOY_TOKEN)
        return result.get("commit", {}).get("sha", "")[:7] if status in (200, 201) else f"error {status}"
    except Exception as e:
        return f"error: {e}"


def create_github_issue(title: str, body: str) -> str:
    """Open a GitHub Issue when new listings are found."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return ""
    try:
        status, result = _gh_request(
            f"/repos/{GITHUB_REPO}/issues",
            method="POST",
            data={"title": title, "body": body, "labels": ["new-listing"]},
        )
        return result.get("html_url", "")
    except Exception as e:
        log(f"  GitHub Issue error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
#  PUSH NOTIFICATIONS — ntfy.sh (free)
# ─────────────────────────────────────────────────────────────

def send_ntfy(title: str, message: str):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers={
                "Title":    title,
                "Priority": "high",
                "Tags":     "tent,campsite",
                "Click":    DASHBOARD_URL,
                "Actions":  f"view, Open Dashboard, {DASHBOARD_URL}",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────────────────────

def send_alert(alerts: list[dict]):
    rows_html = ""
    for a in alerts:
        badge = ""
        if a["prev_count"] == 0 and a["count"] > 0:
            badge = '<span style="background:#16a34a;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px">NEW</span>'
        elif a["count"] > a["prev_count"]:
            delta = a["count"] - a["prev_count"]
            badge = f'<span style="background:#d97706;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px">+{delta} more</span>'

        listing_parts = []
        for r in a["results"][:10]:
            if r["href"] != a["url"]:
                link_open  = f'<a href="{r["href"]}" style="color:#16a34a">'
                link_close = "</a>"
            else:
                link_open, link_close = "", ""
            listing_parts.append(
                f'<li style="margin:4px 0;font-size:13px">'
                f'{link_open}{r["title"][:100]}{link_close}</li>'
            )
        listings_html = "".join(listing_parts)

        rows_html += f"""
        <div style="margin:20px 0;padding:18px 22px;border:1px solid #e5e7eb;
                    border-left:4px solid #16a34a;border-radius:10px;background:#fff">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
            <strong style="font-size:16px;color:#111827">{a['name']}</strong> {badge}
          </div>
          <p style="margin:0 0 10px;color:#6b7280;font-size:13px">
            {a['count']} listing(s) found
            {"(was " + str(a["prev_count"]) + " before)" if a["prev_count"] > 0 else ""}
          </p>
          <ul style="margin:0;padding-left:18px;color:#374151">{listings_html}</ul>
          <p style="margin:12px 0 0">
            <a href="{a['url']}" style="color:#16a34a;font-size:13px;font-weight:500">
              View search results
            </a>
          </p>
        </div>"""

    body_html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                       max-width:620px;margin:auto;padding:32px 24px;color:#111827;background:#f9fafb">
      <div style="background:linear-gradient(135deg,#052e16,#065f46);border-radius:14px;
                  padding:28px;margin-bottom:24px">
        <h2 style="color:#fff;margin:0 0 6px;font-size:22px">&#127957;&#65039; Campsite alert!</h2>
        <p style="color:rgba(255,255,255,.7);margin:0;font-size:13px">
          {datetime.now().strftime('%b %d, %Y at %I:%M %p')} &#183; Near Ionia, MI &#183; Aug 28-30, 2026
        </p>
      </div>
      {rows_html}
      <div style="background:#fff;border-radius:10px;padding:16px 20px;margin-top:8px;
                  border:1px solid #e5e7eb;text-align:center">
        <a href="{DASHBOARD_URL}" style="color:#16a34a;font-weight:600;font-size:14px;text-decoration:none">
          Open live dashboard
        </a>
        &nbsp; | &nbsp;
        <a href="https://github.com/{GITHUB_REPO}/issues"
           style="color:#16a34a;font-weight:600;font-size:14px;text-decoration:none">
          View on GitHub Issues
        </a>
      </div>
      <p style="color:#9ca3af;font-size:11px;text-align:center;margin-top:20px">
        Sent by your campsite monitor &#183; Expires July 31, 2026
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Campsite Monitor: {len(alerts)} search(es) now have availability!"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ", ".join(NOTIFY_EMAILS)
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP("smtp.mail.me.com", 587) as smtp:
        smtp.starttls()
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def _send_test_email():
    """
    Send a realistic-looking test alert so you can verify email delivery.
    All hrefs point to real deep-link URLs (the exact format the live monitor
    will send) so you can confirm the links open the right property page.
    """
    log("Sending TEST email alert…")
    fake_alerts = [
        {
            "name":       "Hipcamp — Near Ionia, MI (Aug 28-30)  [TEST]",
            "url":        "https://www.hipcamp.com/en-US/search?q=Ionia%2C+Michigan&start=2026-08-28&end=2026-08-30",
            "count":      3,
            "prev_count": 0,
            "results": [
                {
                    "title": "TEST — Shady Pines Camp | $65/night | 2 campsites avail.",
                    # Deep link to a real Hipcamp property page (example format)
                    "href":  "https://www.hipcamp.com/en-US/land/michigan-shady-pines-farm-camping-abc123",
                },
                {
                    "title": "TEST — Riverside Glamping Tent | $88/night",
                    "href":  "https://www.hipcamp.com/en-US/land/michigan-riverside-glamping-xyz789",
                },
                {
                    "title": "TEST — Lakefront Campsite #7 | $45/night",
                    "href":  "https://www.hipcamp.com/en-US/land/michigan-lake-shore-campground-def456",
                },
            ],
        },
        {
            "name":       "Airbnb — Ionia, MI (Aug 28-30)  [TEST]",
            "url":        "https://www.airbnb.com/s/Ionia--Michigan--United-States/homes?checkin=2026-08-28&checkout=2026-08-30",
            "count":      2,
            "prev_count": 0,
            "results": [
                {
                    "title": "TEST — Cozy Cabin near Lake Ionia | Entire cabin | $110/night",
                    # Deep link: real Airbnb room URL format with dates pre-filled
                    "href":  "https://www.airbnb.com/rooms/12345678?check_in=2026-08-28&check_out=2026-08-30&adults=2",
                },
                {
                    "title": "TEST — Modern Farmhouse Retreat | Entire home | $145/night",
                    "href":  "https://www.airbnb.com/rooms/98765432?check_in=2026-08-28&check_out=2026-08-30&adults=2",
                },
            ],
        },
        {
            "name":       "Recreation.gov — Near Ionia, MI (Aug 28-30)  [TEST]",
            "url":        "https://www.recreation.gov/search?lat=42.98&lng=-85.06&radius=10&entity_type=campground",
            "count":      1,
            "prev_count": 0,
            "results": [
                {
                    "title": "TEST — Ionia Recreation Area — Site 14 available Aug 28-29",
                    # Deep link: direct campground page format
                    "href":  "https://www.recreation.gov/camping/campgrounds/233396",
                },
            ],
        },
    ]
    try:
        send_alert(fake_alerts)
        log("✓ Test email sent to " + NOTIFY_EMAIL)
    except Exception as e:
        log(f"✗ Test email FAILED: {e}")


def main():
    # ── Test-email mode: python campsite_monitor.py --test-email ─────────────
    if "--test-email" in sys.argv:
        _send_test_email()
        return

    # ── Failure-notify mode: called by GitHub Actions on workflow failure ─────
    if "--notify-failure" in sys.argv:
        if not GMAIL_APP_PASSWORD:
            log("No GMAIL_APP_PASSWORD — skipping failure email.")
            return
        try:
            body = (
                "The campsite monitor workflow failed on GitHub Actions.\n\n"
                "Check the run logs here:\n"
                "https://github.com/mattckaz/campsite-monitor/actions\n\n"
                "This is an automated alert from your campsite monitor."
            )
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Campsite Monitor -- workflow failure"
            msg["From"]    = GMAIL_ADDRESS
            msg["To"]      = ", ".join(NOTIFY_EMAILS)
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP_SSL("smtp.mail.me.com", 465) as smtp:
                smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                smtp.sendmail(GMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
            log("Failure notification email sent.")
        except Exception as e:
            log(f"Failed to send failure email: {e}")
        return

    if date.today() > EXPIRY_DATE:
        log("Monitor expired (past July 31, 2026). Exiting.")
        return

    log("--- Monitor run started ---")
    state        = load_state()
    alerts       = []
    scraper_errors   = []
    scraper_bot_hits = []

    # Permanent record of every listing key we've ever alerted on.
    # Survives git push failures — prevents duplicate alerts even if state resets.
    alerted_keys: set = set(state.get("alerted_keys", []))

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--disable-infobars",
                  "--no-first-run", "--no-service-autorun", "--password-store=basic"],
        )
        chromium_ctx = chromium.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        _stealth.apply_stealth_sync(chromium_ctx)

        # Randomise viewport slightly so every run looks like a different screen
        _vw = random.choice([1280, 1366, 1440, 1536, 1920])
        _vh = random.choice([768, 800, 900, 960, 1024])

        firefox = pw.firefox.launch(headless=True)
        firefox_ctx = firefox.new_context(
            viewport={"width": _vw, "height": _vh},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) "
                "Gecko/20100101 Firefox/124.0"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )

        BROWSER_CTX = {
            "hipcamp":      chromium_ctx,
            "airbnb":       chromium_ctx,
            "vrbo":         firefox_ctx,
            "booking":      firefox_ctx,   # Firefox for better bot avoidance
            "vacasa":       chromium_ctx,
            "glamping_hub": chromium_ctx,
            "campspot":     chromium_ctx,
            "snow_lake":    chromium_ctx,
            "michigan_dnr": chromium_ctx,  # needs real session for API auth
        }

        for search in SEARCHES:
            name = search["name"]
            url  = search["url"]
            kind = search["type"]
            log(f"Checking: {name}")

            try:
                if kind in API_SCRAPERS:
                    results = SCRAPERS[kind](url)
                else:
                    page    = BROWSER_CTX[kind].new_page()
                    results = SCRAPERS[kind](page, url)
                    page.close()
            except Exception as e:
                log(f"  ERROR scraping {name}: {e}")
                scraper_errors.append(name)
                results = []

            if results is None:
                log(f"  WARNING: bot-detection for {name} -- skipping this run")
                scraper_bot_hits.append(name)
                continue

            prev       = state.get(name, {})
            prev_count = prev.get("count", 0)
            prev_keys  = set(prev.get("keys", []))
            curr_count = len(results)
            curr_keys  = {r["title"][:80] for r in results}

            # Only alert on keys we have NEVER alerted on before (across all runs)
            truly_new_keys = curr_keys - alerted_keys

            log(f"  {curr_count} listing(s) found (was {prev_count}), {len(truly_new_keys)} never alerted")

            if curr_count > 0 and truly_new_keys:
                alerts.append({
                    "name":       name,
                    "url":        url,
                    "count":      curr_count,
                    "prev_count": prev_count,
                    "results":    [r for r in results if r["title"][:80] in truly_new_keys],
                })

            state[name] = {
                "count":        curr_count,
                "keys":         list(curr_keys),
                "last_checked": datetime.now(timezone.utc).isoformat(),
            }

        chromium.close()
        firefox.close()

    # Record run health in state for dashboard display
    state["_last_run"] = {
        "time":     datetime.now(timezone.utc).isoformat(),
        "errors":   scraper_errors,
        "bot_hits": scraper_bot_hits,
        "ok":       len(scraper_errors) == 0 and len(scraper_bot_hits) == 0,
    }

    save_state(state)
    write_status_page(state)

    deploy_result = deploy_to_github()
    if deploy_result:
        log(f"GitHub Pages deploy: {deploy_result} -> {DASHBOARD_URL}")

    if alerts:
        log(f"Sending alerts for {len(alerts)} search(es)...")

        # Save alert history to state so dashboard can show past finds
        alert_history = state.get("alert_history", [])
        for a in alerts:
            for r in a["results"][:10]:
                key = r["title"][:80]
                alerted_keys.add(key)  # mark as permanently alerted
                alert_history.append({
                    "source":    a["name"],
                    "title":     r["title"][:100],
                    "href":      r["href"],
                    "found_at":  datetime.now(timezone.utc).isoformat(),
                })
        state["alert_history"] = alert_history[-50:]  # keep last 50
        state["alerted_keys"]  = list(alerted_keys)   # persist forever

        try:
            send_alert(alerts)
            log("Email sent.")
        except Exception as e:
            log(f"ERROR sending email: {e}")

        summary = ", ".join(
            f"{a['count']} on {a['name'].split('--')[0].strip()}" for a in alerts)
        send_ntfy("Campsite Alert!", f"New listings: {summary}. Open dashboard for details.")

        # Create a GitHub Issue — tracked record at github.com/repo/issues
        issue_body = (
            f"**Checked:** {datetime.now().strftime('%b %d, %Y at %I:%M %p')}  \n"
            f"**Dashboard:** {DASHBOARD_URL}\n\n"
        )
        for a in alerts:
            issue_body += f"### {a['name']}\n"
            issue_body += f"**{a['count']} listing(s)** found"
            if a["prev_count"] > 0:
                issue_body += f" (was {a['prev_count']})"
            issue_body += "\n\n"
            for r in a["results"][:10]:
                issue_body += f"- [{r['title'][:80]}]({r['href']})\n"
            issue_body += f"\n[View search]({a['url']})\n\n"

        issue_url = create_github_issue(
            f"Campsite Alert: new listings found ({datetime.now().strftime('%b %d, %Y')})",
            issue_body,
        )
        if issue_url:
            log(f"GitHub Issue created: {issue_url}")
    else:
        log("No new listings. No alerts sent.")

    log("--- Run complete ---\n")


if __name__ == "__main__":
    main()
