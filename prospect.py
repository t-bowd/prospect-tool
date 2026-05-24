#!/usr/bin/env python3
"""Business prospecting CLI — Google Places + ABN + website scrape + ASIC."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("prospect")

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ABN_LOOKUP_GUID = os.getenv("ABN_LOOKUP_GUID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
ABN_MATCHING_NAMES_URL = "https://abr.business.gov.au/json/MatchingNames.aspx"
ASIC_SEARCH_URL = "https://connectonline.asic.gov.au/RegistrySearch/faces/landing/SearchRegisters.jspx"

REQUEST_DELAY_SEC = 0.25
HTTP_TIMEOUT = 20
USER_AGENT = "ProspectTool/1.0 (+internal-research)"


@dataclass
class Business:
    business_name: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    google_rating: float | None = None
    review_count: int | None = None
    years_operating: int | None = None
    abn: str = ""
    entity_type: str = ""
    registration_date: str = ""
    owner_name: str = ""
    owner_email: str = ""
    owner_phone: str = ""
    director_names: str = ""
    priority_score: float = 0.0
    google_place_id: str = ""
    notes: str = ""
    _errors: list[str] = field(default_factory=list)

    def add_error(self, step: str, msg: str) -> None:
        self._errors.append(f"{step}: {msg}")

    def finalize_notes(self) -> None:
        if self._errors:
            self.notes = "; ".join(self._errors)


# ---------- Google Places ----------

def google_text_search(query: str, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    params: dict[str, Any] = {"query": query, "key": GOOGLE_PLACES_API_KEY}
    is_paginated = False
    while True:
        data = None
        status = "UNKNOWN"
        for attempt in range(8):
            r = requests.get(PLACES_TEXTSEARCH_URL, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            status = data.get("status", "UNKNOWN")
            if status == "INVALID_REQUEST" and is_paginated:
                time.sleep(3 + attempt)
                continue
            break
        if status not in ("OK", "ZERO_RESULTS"):
            msg = data.get("error_message", "") if data else ""
            if is_paginated:
                log.warning("Pagination gave up after retries (%s %s). Returning %d results.", status, msg, len(results))
                return results
            raise RuntimeError(f"Places text search failed: {status} {msg}")
        results.extend(data.get("results", []))
        if len(results) >= limit:
            return results[:limit]
        next_token = data.get("next_page_token")
        if not next_token:
            return results
        time.sleep(3)
        params = {"pagetoken": next_token, "key": GOOGLE_PLACES_API_KEY}
        is_paginated = True


def google_place_details(place_id: str) -> dict[str, Any]:
    fields = ",".join([
        "name", "formatted_address", "formatted_phone_number",
        "international_phone_number", "website", "rating",
        "user_ratings_total", "opening_hours", "url",
    ])
    params = {"place_id": place_id, "fields": fields, "key": GOOGLE_PLACES_API_KEY}
    r = requests.get(PLACES_DETAILS_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"Place details failed: {data.get('status')} {data.get('error_message','')}")
    return data.get("result", {})


# ---------- ABN ----------

def abn_lookup(business_name: str) -> dict[str, Any] | None:
    if not ABN_LOOKUP_GUID:
        raise RuntimeError("ABN_LOOKUP_GUID not configured")
    params = {"name": business_name, "guid": ABN_LOOKUP_GUID, "maxResults": 5}
    r = requests.get(ABN_MATCHING_NAMES_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    text = r.text.strip()
    # Response is wrapped in callback(...) sometimes; strip JSONP wrapper if present.
    m = re.match(r"^[A-Za-z_$][\w$]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ABN response not JSON: {e}")
    names = data.get("Names") or []
    if not names:
        return None
    # Prefer highest-score active match.
    names_sorted = sorted(
        names,
        key=lambda n: (n.get("IsCurrent", "N") == "Y", float(n.get("Score", 0) or 0)),
        reverse=True,
    )
    return names_sorted[0]


# ---------- Website scrape + Gemini ----------

def fetch_url(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


COMMON_OWNER_PATHS = [
    "about", "about-us", "contact", "contact-us",
    "team", "our-team", "staff", "people",
    "teachers", "tutors", "instructors", "faculty",
    "founder", "owner", "director", "principal", "leadership",
]

# Keywords used to score internal links on the homepage as "likely owner page".
OWNER_LINK_KEYWORDS = (
    "owner", "founder", "director", "principal", "ceo", "president",
    "about", "team", "staff", "people", "leadership", "bio",
    "teacher", "tutor", "instructor", "faculty", "meet",
)

MAX_SCRAPE_PAGES = 10


def discover_owner_links(homepage_html: str, base: str) -> list[str]:
    """Return internal URLs whose anchor text or path slug suggests an owner/about page."""
    soup = BeautifulSoup(homepage_html, "html.parser")
    base_host = urlparse(base).netloc.lower()
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base + "/", href)
        parsed = urlparse(url)
        if parsed.netloc.lower() != base_host:
            continue
        url_clean = url.split("#")[0]
        if url_clean in seen:
            continue
        seen.add(url_clean)
        text = (a.get_text() or "").strip().lower()
        path = parsed.path.lower()
        score = sum(1 for kw in OWNER_LINK_KEYWORDS if kw in text or kw in path)
        if score > 0:
            scored.append((score, url_clean))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored]


def scrape_site_text(homepage: str) -> str:
    parsed = urlparse(homepage)
    if not parsed.scheme:
        homepage = "https://" + homepage
        parsed = urlparse(homepage)
    base = f"{parsed.scheme}://{parsed.netloc}"

    candidates: list[str] = [homepage]
    candidates.extend(urljoin(base + "/", p) for p in COMMON_OWNER_PATHS)

    # Fetch homepage first to mine for relevant internal links.
    home_html = ""
    try:
        home_html = fetch_url(homepage)
    except Exception as e:
        log.debug("Homepage fetch failed %s: %s", homepage, e)

    if home_html:
        candidates.extend(discover_owner_links(home_html, base))

    # Dedup while preserving order, then cap.
    seen: set[str] = set()
    ordered: list[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    ordered = ordered[:MAX_SCRAPE_PAGES]

    chunks: list[str] = []
    if home_html:
        chunks.append(f"--- {homepage} ---\n{extract_text(home_html)[:8000]}")
        ordered = [u for u in ordered if u != homepage]
    for url in ordered:
        try:
            html = fetch_url(url)
            chunks.append(f"--- {url} ---\n{extract_text(html)[:8000]}")
        except Exception as e:
            log.debug("Skip %s: %s", url, e)
    return "\n\n".join(chunks)


def gemini_extract_owner(site_text: str, business_name: str) -> dict[str, str]:
    if not site_text.strip():
        return {}
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        f"You are extracting contact information for the business owner of '{business_name}' "
        f"from the website text below. Return a strict JSON object with keys: "
        f"owner_name, owner_email, owner_phone. Use empty strings if unknown.\n\n"
        f"IMPORTANT: Find the ACTUAL business owner / founder / director / principal — "
        f"a real person's name. Do NOT return generic info@... emails, contact form addresses, "
        f"or staff who are not the owner. If you cannot identify the owner with reasonable "
        f"confidence, return empty strings.\n\n"
        f"Website text:\n{site_text[:24000]}\n\n"
        f"Return only the JSON object, no prose."
    )
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    raw = (resp.text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("Gemini returned non-JSON: %s", raw[:200])
        return {}
    return {
        "owner_name": (data.get("owner_name") or "").strip(),
        "owner_email": (data.get("owner_email") or "").strip(),
        "owner_phone": (data.get("owner_phone") or "").strip(),
    }


# ---------- ASIC ----------

def asic_lookup(business_name: str) -> str:
    """Best-effort ASIC director lookup.

    ASIC Connect has no public API and uses a JSF-based UI that is hostile to
    scraping. Rather than ship a brittle scraper, flag for manual follow-up.
    """
    return ""  # Caller records a note that this needs manual follow-up.


# ---------- Scoring ----------

def years_between(start_iso: str, end: date) -> int | None:
    if not start_iso:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%Y"):
        try:
            d = datetime.strptime(start_iso[: len(fmt)] if "T" not in fmt else start_iso, fmt).date()
            return max(0, end.year - d.year - ((end.month, end.day) < (d.month, d.day)))
        except ValueError:
            continue
    return None


def compute_priority_score(b: Business) -> float:
    score = 0.0
    # Review count: up to 40 points, capped at 100 reviews.
    if b.review_count:
        score += 40 * min(b.review_count, 100) / 100
    # Rating: up to 20 points, only counts above 4.0.
    if b.google_rating and b.google_rating >= 4.0:
        score += 20 * (b.google_rating - 4.0) / 1.0
    # Years operating: up to 20 points, capped at 20 years.
    if b.years_operating is not None:
        score += 20 * min(b.years_operating, 20) / 20
    # Has website: 10 points.
    if b.website:
        score += 10
    # Owner contact found: 10 points if any of name/email/phone present.
    if b.owner_name or b.owner_email or b.owner_phone:
        score += 10
    return round(score, 2)


# ---------- Pipeline ----------

UNIVERSITY_NAME_KEYWORDS = ("university", "tafe", "conservatorium")


def is_university(business_name: str, website: str) -> bool:
    name = (business_name or "").lower()
    if any(k in name for k in UNIVERSITY_NAME_KEYWORDS):
        return True
    host = urlparse(website or "").netloc.lower()
    if host.endswith(".edu") or ".edu." in host:
        return True
    return False


def fetch_place_details(place_summary: dict[str, Any]) -> Business:
    b = Business()
    place_id = place_summary.get("place_id", "")
    b.google_place_id = place_id
    try:
        details = google_place_details(place_id)
        b.business_name = details.get("name", place_summary.get("name", ""))
        b.address = details.get("formatted_address", place_summary.get("formatted_address", ""))
        b.phone = details.get("formatted_phone_number", "") or details.get("international_phone_number", "")
        b.website = details.get("website", "")
        b.google_rating = details.get("rating")
        b.review_count = details.get("user_ratings_total")
    except Exception as e:
        b.business_name = place_summary.get("name", "")
        b.add_error("places_details", str(e))
        log.warning("Place details failed for %s: %s", b.business_name, e)
    return b


def enrich_business(b: Business) -> Business:
    time.sleep(REQUEST_DELAY_SEC)

    # ABN
    try:
        match = abn_lookup(b.business_name)
        if match:
            b.abn = match.get("Abn", "") or ""
            b.entity_type = match.get("NameType", "") or match.get("AbnStatus", "")
            b.registration_date = match.get("AbnStatusEffectiveFrom", "") or ""
            b.years_operating = years_between(b.registration_date, date.today())
    except Exception as e:
        b.add_error("abn_lookup", str(e))
        log.warning("ABN lookup failed for %s: %s", b.business_name, e)
    time.sleep(REQUEST_DELAY_SEC)

    # Website scrape + Gemini
    if b.website:
        try:
            site_text = scrape_site_text(b.website)
            if site_text:
                owner = gemini_extract_owner(site_text, b.business_name)
                b.owner_name = owner.get("owner_name", "")
                b.owner_email = owner.get("owner_email", "")
                b.owner_phone = owner.get("owner_phone", "")
        except Exception as e:
            b.add_error("website_scrape", str(e))
            log.warning("Website/Gemini failed for %s: %s", b.business_name, e)

    # ASIC (Pty Ltd only)
    if b.entity_type and "pty" in b.entity_type.lower():
        try:
            b.director_names = asic_lookup(b.business_name)
            if not b.director_names:
                b.add_error("asic", "no public API — manual follow-up required")
        except Exception as e:
            b.add_error("asic", str(e))

    b.priority_score = compute_priority_score(b)
    b.finalize_notes()
    return b


def run(business_type: str, location: str, limit: int) -> str:
    if not GOOGLE_PLACES_API_KEY:
        raise SystemExit("GOOGLE_PLACES_API_KEY not set")
    if not ABN_LOOKUP_GUID:
        log.warning("ABN_LOOKUP_GUID not set — ABN enrichment will fail per business")
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — owner extraction will fail per business")

    query = f"{business_type} in {location}"
    # Over-fetch so university exclusions don't shrink the final count below `limit`.
    # Places text search caps at 60 (3 pages of 20).
    fetch_budget = min(60, max(limit * 3, limit + 10))
    log.info("Searching Google Places: %s (fetching up to %d candidates)", query, fetch_budget)
    places = google_text_search(query, fetch_budget)
    log.info("Found %d candidate places", len(places))

    rows: list[dict[str, Any]] = []
    skipped = 0
    for place in places:
        if len(rows) >= limit:
            break
        name = place.get("name", "?")
        log.info("[%d/%d accepted] %s", len(rows) + 1, limit, name)
        b = fetch_place_details(place)
        if is_university(b.business_name, b.website):
            skipped += 1
            log.info("  skip (university/edu): %s", b.business_name)
            continue
        b = enrich_business(b)
        d = asdict(b)
        d.pop("_errors", None)
        rows.append(d)

    if skipped:
        log.info("Skipped %d university/edu results", skipped)

    df = pd.DataFrame(rows)
    columns = [
        "business_name", "address", "phone", "website", "google_rating",
        "review_count", "years_operating", "abn", "entity_type",
        "registration_date", "owner_name", "owner_email", "owner_phone",
        "director_names", "priority_score", "google_place_id", "notes",
    ]
    df = df[columns]
    df = df.sort_values("priority_score", ascending=False)

    type_slug = re.sub(r"[^a-z0-9]+", "_", business_type.lower()).strip("_")
    loc_slug = re.sub(r"[^a-z0-9]+", "_", location.lower()).strip("_")
    today = date.today().isoformat()
    out_path = f"results_{type_slug}_{loc_slug}_{today}.csv"
    df.to_csv(out_path, index=False)
    log.info("Wrote %d rows to %s", len(df), out_path)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Business prospecting CLI")
    parser.add_argument("--type", required=True, help="Business type, e.g. 'music school'")
    parser.add_argument("--location", required=True, help="Location, e.g. 'Sydney'")
    parser.add_argument("--limit", type=int, default=20, help="Max businesses to process")
    args = parser.parse_args()
    out = run(args.type, args.location, args.limit)
    print(out)


if __name__ == "__main__":
    main()
