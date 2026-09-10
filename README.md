# Lead Finder

**Pull hundreds of local businesses — with phone, website and e-mail — out of an area that most tools cap at 20.**

A self-hosted lead generation engine: FastAPI backend, Streamlit workspace,
SQLite storage. Point it at a city and a business type, watch the leads arrive,
export them to CSV or Excel.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009485)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B)
![License](https://img.shields.io/badge/licence-PolyForm%20Noncommercial-orange)

---

## The problem this solves

Every places API has a ceiling. **Google Places returns at most 60 results per
text query** no matter how you paginate it — so "dentists in Chicago" gives you
the same 60 businesses today, tomorrow and next week. Scraping tools built on
top of that inherit the ceiling, hand you duplicates on every re-run, and freeze
a browser tab for ten minutes while they work.

Lead Finder gets past all three:

| The usual problem | What this does |
|---|---|
| Hard cap of ~20–60 results per search | Splits the area into an **N×N grid** and searches each cell separately — 9 searches instead of 1, uncapped in aggregate |
| Same businesses returned every run | **Deduplicates against your whole database** — every place you've already saved or excluded is filtered out before you see it |
| Provider dies, run dies | Any cell that errors or gets rate-limited is **retried on OpenStreetMap** — free, keyless, no per-query cap |
| Fixed "sleep 60s" between batches | **Adaptive pacing**: full speed while the provider is happy, automatic back-off on a 429, recovery when it clears |
| A frozen progress bar for ten minutes | **Live job progress** — cells completed, leads found, duplicates skipped, sites scraped, all updating as it runs, with a working Stop button |
| Crash halfway = start over | **Every cell is saved and checkpointed as it finishes**, so an interrupted run resumes from the first area it never reached |
| No e-mail addresses | Scrapes every discovered website for **e-mail and social profiles** |

## Measured on a real run

Google Places primary, OSM fallback, 3×3 grid, dedup on:

```
searching   33%  found=29   4s   29 leads from 3/9 cells
enriching  100%  found=58   9s   Scraping websites for contact details — 2/56
enriching  100%  found=58  62s   Scraping websites for contact details — 34/56
completed  100%  found=58  94s   58 leads · 54 enriched
```

58 businesses from an area a single query caps at 60 — while a parallel
"dentists in Austin" run surfaced 57 new leads and silently discarded 23
already in the database. Search finishes in seconds; the website scraping stage
is the slow part, at roughly one site per second.

## Quick start

```bash
git clone https://github.com/intikhab49/Upgraded-Lead-Finder-.git
cd Upgraded-Lead-Finder-
pip install -r requirements.txt

cp .env.example .env        # optional: add GOOGLE_PLACES_API_KEY
```

**No API key? It still works.** Set `PROVIDER_NAME=osm` and the whole thing runs
on OpenStreetMap — free, no signup, no billing, no per-query cap. Add a Google
key later for richer data (ratings, opening hours, better phone coverage).

```bash
# backend
python -m uvicorn app.main:app --port 8000

# workspace (second terminal)
streamlit run frontend/app.py --server.port 8501
```

Open **http://localhost:8501** to work, or **http://localhost:8000/docs** for the API.

## The workspace

Three screens, no clutter:

- **Find leads** — one form: where, what, how wide, how deep. Start it and watch
  the run: areas searched, leads found, duplicates skipped, scraping progress,
  current pacing. Stop whenever you like and keep everything found so far.
- **Saved leads** — filter by text, status or search run; page through them;
  export to CSV or Excel; delete a batch you don't want.
- **History** — every run with status, lead count, duration and timing;
  filterable and exportable; resume the ones that stopped early.

**You choose what happens to results, up front.** Every run asks: *save to the
database as they arrive*, or *hold for review* — where nothing is written until
you tick the rows you want. No silent writes.

## How a run works

```
location + category
        ↓
  geocode → split the area into N×N cells
        ↓
  ┌─────────────────────────────────────────────┐
  │  4 cells searched at once                   │
  │    · paced by an adaptive rate limiter      │
  │    · 429/5xx retried with jittered backoff  │
  │    · failed cell → free OSM fallback        │
  │    · results deduped against the database   │
  │    · each finished cell saved + checkpointed│
  └─────────────────────────────────────────────┘
        ↓
  scrape every website found for e-mail + socials (20 at a time)
        ↓
  review, export, or hand to your dialer
```

**Start a run** — `POST /api/v1/discovery/start` returns a job id immediately:

```bash
curl -X POST localhost:8000/api/v1/discovery/start -H 'Content-Type: application/json' -d '{
  "location": "Chicago, IL", "business_category": "dentist",
  "radius": 8000, "grid_size": 3, "max_results": 400,
  "auto_save": true, "exclude_saved": true
}'
```

Then poll `GET /discovery/{job_id}` for live progress, read partial results from
`/{job_id}/results`, stop it with `/{job_id}/cancel`, or save a chosen subset
with `/{job_id}/save`. `GET /discovery/runs/resumable` lists runs that stopped
early — pass `resume_run_id` to `/start` to continue one.

## What's in the box

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11+ |
| API | FastAPI, Pydantic v2, Uvicorn |
| Providers | Google Places (New v1) · OpenStreetMap/Overpass (free fallback) · Foursquare · Geoapify |
| Data | SQLAlchemy 2.0, SQLite, openpyxl |
| Frontend | Streamlit |
| Enrichment | httpx + BeautifulSoup website scraping; optional Gemini decision engine |
| Quality | pytest (asyncio), ruff, mypy, black, isort |

```
app/
├── api/routes/       # business, discovery, review, leads, export, history
├── core/             # config, exceptions, logging, retry, rate_limiter
├── db/               # models (Lead, RunHistory, DiscoveryExclusion), repository
├── providers/        # google_places, osm, foursquare, geoapify + base contract
├── services/         # discovery_engine, discovery_jobs, enrichment, scraping,
│                     # deduplicator, persistence, run history
└── agents/           # optional multi-agent enrichment pipeline
frontend/             # Streamlit workspace + API client
tests/                # unit + integration suites
```

Swapping providers is a config change, not a code change — every provider
implements the same `BaseProvider` contract (`app/providers/base.py`).

## Configuration

Everything lives in `.env` (see `.env.example`, and `app/core/config.py` for the
full list). The ones that matter:

| Variable | What it does | Default |
|----------|--------------|---------|
| `PROVIDER_NAME` | Primary provider: `google_places` · `osm` · `foursquare` · `geoapify` | `google_places` |
| `GOOGLE_PLACES_API_KEY` | Google Places (New) key — omit it and run on `osm` | — |
| `FALLBACK_PROVIDER_NAME` | Free provider used when the primary fails; `none` to disable | `osm` |
| `DISCOVERY_CONCURRENCY` | Grid cells searched at once | `4` |
| `DISCOVERY_MIN_INTERVAL` / `DISCOVERY_MAX_INTERVAL` | Adaptive pacing floor / ceiling, seconds | `0.35` / `30` |
| `ENRICHMENT_CONCURRENCY` | Websites scraped at once | `20` |
| `OSM_MIN_INTERVAL` | Seconds between Overpass calls (be a good citizen) | `1.5` |
| `RETRY_*` | Retry policy for transient 429/5xx | 3 retries, exp. backoff |
| `DATABASE_URL` | SQLAlchemy URL | `sqlite:///./business_leads.db` |
| `GEMINI_API_KEY` | Optional — enables the LLM decision engine | — |

> **Place IDs.** The schema uses the historical field name `fsq_place_id` for
> backward compatibility. It holds whatever the active provider issued: a Google
> place ID (`ChIJ…`), an OSM reference (`osm:n42`), or a Foursquare ID.

## API reference

All routes are mounted under `/api/v1`.

**Discovery (recommended)**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/discovery/providers` | Which providers are configured and usable |
| POST | `/discovery/start` | Start a crawl — returns a job id immediately |
| GET | `/discovery/{job_id}` | Live progress, counts, pacing, per-cell log |
| GET | `/discovery/{job_id}/results` | Leads collected so far (paginated) |
| POST | `/discovery/{job_id}/cancel` | Stop a run, keep everything found |
| POST | `/discovery/{job_id}/save` | Persist all, or a selected subset |
| GET | `/discovery/jobs` | Jobs in this server process |
| GET | `/discovery/runs/resumable` | Runs that stopped with areas left |

**Search, leads, review, export, history**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` · `/metrics` | Health check · runtime metrics |
| POST | `/search` · `/search/query` | Blocking search by category or free text |
| GET | `/businesses/{place_id}` | Business detail |
| POST | `/enrich` · `/enrich/discover` | Scrape a site · discover e-mail and socials |
| GET/POST/PUT/DELETE | `/leads` · `/leads/{id}` · `/leads/batch` | Full lead CRUD, upsert by place ID |
| GET | `/leads/stats` · `/leads/batches` | Dashboard counts · leads grouped by run |
| POST | `/exclusions/{place_id}` · DELETE · GET | Never show this place again |
| GET | `/review/pending` · `/review/{id}/approve` · `/reject` · `/edit` | Human review queue |
| GET | `/export/csv` · `/export/excel` | Download saved leads with every field |
| GET | `/history` · `/history/stats` · `/history/export/csv` | Run history and exports |

Exported rows carry: name, address, city, state, country, phone, website,
e-mail, LinkedIn, Facebook, Instagram, X, YouTube, categories, coordinates,
confidence score, source, review status, created/updated timestamps.

## Testing

```bash
pytest tests/ -v                              # full suite
pytest tests/test_discovery_engine.py -v      # grid crawl, fallback, cancellation
pytest tests/test_rate_limiter.py -v          # adaptive pacing
pytest tests/test_osm_provider.py -v          # free provider + mirror rotation
pytest tests/ --cov=app --cov-report=html     # coverage

ruff check app/ frontend/ tests/
mypy app/ --ignore-missing-imports
```

## Honest limits

- **E-mail coverage depends on the website.** Addresses come from scraping, so a
  business with no site — or one that hides contact behind a JS form — won't
  have one. Expect roughly 4 in 5 among leads that have a scrapable site.
- **OpenStreetMap coverage varies by area and category.** It is excellent for
  cafés, clinics and shops in mapped cities; thin for niche B2B categories.
- **Overpass is volunteer-run.** The client paces itself, rotates mirrors and
  backs off, but a busy mirror can still slow a run down.
- **Google billing still applies.** A 5×5 grid is 25 searches, not one.
- **Website scraping is the slow stage** — about a second per site. A 400-lead
  run spends most of its wall clock here, not in the search. Turn it off for
  pure phone-number lists.
- **No authentication.** Built to run on your own machine or a private network.
  Put it behind a reverse proxy with auth before exposing it.

## Licence and ownership

Copyright © 2026 **Intikhab Azam**. All rights reserved.

Published under the [PolyForm Noncommercial License 1.0.0](LICENSE) — the source
is public to read, run, study and modify for **noncommercial** purposes.

**Commercial rights are not granted.** Selling this software, selling a service
built on it, or using it inside a business requires a separate written licence
from the copyright holder. Only the owner may offer it commercially — open an
issue or contact [@intikhab49](https://github.com/intikhab49) to arrange one.

The leads you generate with it are yours. The software is not.
