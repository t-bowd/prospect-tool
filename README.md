# prospect-tool

CLI tool for top-of-funnel M&A prospecting. Given a business type and location, it searches Google Places, enriches each result with ABN/ASIC data and scraped owner details, and writes a prioritised CSV ready for outreach.

## What it does

For each business found in a given area, the tool gathers:

- **Google Places** — name, address, phone, website, rating, review count
- **ABN Lookup** — ABN, entity type, registration date, derived years operating
- **Website scrape + Gemini** — owner/founder name, email, phone (extracted from `/`, `/about`, `/contact`)
- **ASIC** — flagged as manual follow-up for Pty Ltd entities (no public API)

It then computes a `priority_score` (review count, rating, years operating, has website, owner contact found) and sorts the CSV by it.

Universities and TAFEs are filtered out automatically (name contains *university* / *tafe* / *conservatorium*, or website ends with `.edu` / `.edu.*`). The tool over-fetches candidates so excluded results don't shrink your final count.

## Setup

Requires Python 3.12+.

```bash
git clone <repo-url>
cd prospect-tool
pip3 install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and fill in the three keys (see [Getting API keys](#getting-api-keys) below).

## Usage

```bash
python3 prospect.py --type "music school" --location "Sydney" --limit 50
```

Arguments:

| flag | required | description |
|------|----------|-------------|
| `--type` | yes | Business category, e.g. `"music school"`, `"dental clinic"`, `"accounting firm"` |
| `--location` | yes | City, suburb, or region — anything Google understands |
| `--limit` | no (default 20) | Number of non-university results to return. Capped at 60 by Google. |

Output is written to the current directory as:

```
results_<type>_<location>_<date>.csv
```

e.g. `results_music_school_sydney_2026-05-24.csv`

### CSV columns

`business_name`, `address`, `phone`, `website`, `google_rating`, `review_count`, `years_operating`, `abn`, `entity_type`, `registration_date`, `owner_name`, `owner_email`, `owner_phone`, `director_names`, `priority_score`, `google_place_id`, `notes`

The `notes` column records which enrichment steps failed per business (e.g. `abn_lookup: ...`, `website_scrape: ...`). The run never aborts on a single failure — it logs and continues.

## Getting API keys

All three keys go in `.env`.

### 1. `GOOGLE_PLACES_API_KEY`

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or reuse one). Enable billing — Google gives a $200/month free credit which covers thousands of calls.
3. **APIs & Services → Library** → enable **Places API** (classic, not "Places API (New)").
4. **APIs & Services → Credentials → Create Credentials → API key**.
5. Restrict the key: API restriction = Places API; application restriction = IP address (your laptop) or none for testing.

Note: a referrer-restricted browser key (e.g. one used in a website's HTML) won't work here — the tool calls Places from a server-side context.

### 2. `ABN_LOOKUP_GUID`

Free, instant registration with the Australian Business Register.

1. Go to [abr.business.gov.au/Tools/WebServices](https://abr.business.gov.au/Tools/WebServices).
2. Register for the ABN Lookup web services.
3. The GUID arrives by email within a few minutes.

Free tier: ~1,000 requests/day per GUID.

### 3. `GEMINI_API_KEY`

Free tier available with no billing required.

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. Sign in, click **Create API key**, attach to any Google Cloud project.

Free tier on `gemini-2.5-flash`: 10 RPM / 1,500 RPD — plenty for a 50-business run.

## How scoring works

`priority_score` is a 0–100 number combining:

- Review count (40 pts max, capped at 100 reviews)
- Rating above 4.0 (20 pts max)
- Years operating from ABN registration date (20 pts max, capped at 20 years)
- Has a website (10 pts)
- Any owner contact extracted (10 pts)

CSV is sorted by this descending.

## Known limitations

- **ASIC**: ASIC Connect is a JSF-based UI with no public API. Director names are not populated automatically — the tool flags Pty Ltd entities in `notes` so you know to look them up manually (or use a paid reseller like InfoTrack).
- **Pagination**: Google's `next_page_token` is eventually-consistent. The tool retries up to 8 times; if it still fails it returns the first 20 results and logs a warning rather than aborting.
- **Owner extraction**: Gemini will only return an owner when the website actually names them. Generic `info@` contact pages produce empty fields by design.
- **ABN matching is name-based**, so businesses with very common or very generic names may match the wrong entity. Spot-check the `abn` column before trusting `years_operating`.

## Troubleshooting

| symptom | fix |
|---------|-----|
| `GOOGLE_PLACES_API_KEY not set` | Your keys are in `.env.example` instead of `.env`. Copy them over. |
| `REQUEST_DENIED` from Places | Key is referrer-restricted or Places API isn't enabled on the project. Create a fresh unrestricted key. |
| `404 NOT_FOUND` from Gemini | Model name is stale. Edit `gemini_extract_owner` in `prospect.py` to use a current model. |
| Pagination warnings | Google sometimes throttles `next_page_token`. Re-run, or accept the 20-result first page. |
| Many `abn_lookup` failures in `notes` | Check your `ABN_LOOKUP_GUID` is valid and you haven't blown the daily quota. |

## Dependencies

`requests`, `google-genai`, `python-dotenv`, `pandas`, `beautifulsoup4`. Listed in `requirements.txt`.
