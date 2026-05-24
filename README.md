# prospect-tool

CLI for top-of-funnel M&A prospecting. Searches Google Places, enriches each result with ABN data and a scraped owner contact, and writes a prioritised CSV.

## Setup

```bash
pip3 install -r requirements.txt
cp .env.example .env   # then fill in the 3 keys
```

Keys needed in `.env`: `GOOGLE_PLACES_API_KEY`, `ABN_LOOKUP_GUID`, `GEMINI_API_KEY`.

## Usage

```bash
python3 prospect.py --type "music school" --location "Sydney" --limit 50
```

| flag | required | description |
|------|----------|-------------|
| `--type` | yes | Business category, e.g. `"dental clinic"` |
| `--location` | yes | City, suburb, or region |
| `--limit` | no (default 20) | Number of non-university results to return (Google caps at 60) |

Output: `results_<type>_<location>_<date>.csv`, sorted by `priority_score` descending. Universities and TAFEs are auto-excluded.

## Scoring

`priority_score` is 0–100, combining:

- Review count — 40 pts (capped at 100 reviews)
- Rating above 4.0 — 20 pts
- Years operating from ABN date — 20 pts (capped at 20 years)
- Has website — 10 pts
- Owner contact extracted — 10 pts

## Limitations

- **ASIC director lookup is not automated** — flagged in `notes` for manual follow-up on Pty Ltd entities.
- **Pagination is best-effort** — if Google's `next_page_token` doesn't activate, you get the first 20 results with a warning.
- **Owner extraction depends on the website** — generic `info@` contact pages yield no owner.
- **ABN match is by name** — spot-check before trusting `years_operating` for common names.
- Per-business enrichment failures are logged in the `notes` column; the run never aborts.
