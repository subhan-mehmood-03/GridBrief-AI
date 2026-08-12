# GridBrief AI

GridBrief AI is a source-grounded ERCOT energy-intelligence application. It combines structured
power-market data, grid operations, generation, weather, and official documents in a responsive
website. Deterministic calculations produce numeric answers; retrieval and an optional LLM explain
the evidence, while citations and verification keep answers and persona-specific editions grounded.

**Live website:** [gridbrief-web.onrender.com](https://gridbrief-web.onrender.com/)

**LangSmith/LangGraph AI Trace Example:** [AI Trace](https://smith.langchain.com/public/00a10de1-cf1b-4052-befc-51aa8f857e27/r/019ff4a9-d2bb-7ed1-98e5-5f22c0516737?start_time=2026-08-12T06%3A29%3A58.075382Z)

**LangSmith/LangGraph Edition Trace Example:** [Edition Trace](https://smith.langchain.com/public/02188a1e-a65c-438f-90fb-946e7d088b47/r/019fe3ff-ad65-7bd0-8d10-d0158e0c06dc?start_time=2026-08-09T00%3A50%3A11.940316Z)

## Included data

| Source | Data used by GridBrief |
|---|---|
| ERCOT | System load and forecasts, real-time and day-ahead settlement prices, trading-hub LMPs, generation by fuel, wind and solar forecasts, outages, reserves, capacity, frequency, storage output, and selected ancillary-service prices |
| U.S. EIA | EIA-930 balancing-authority demand and generation history plus EIA electricity news |
| National Weather Service | Texas alerts and hourly temperature, dew point, humidity, precipitation probability, wind speed, and wind direction forecasts |
| Official RSS/documents | Citable narrative evidence used for editions, risk summaries, and broad Ask AI questions |

The public charts expose rolling 24-hour, 48-hour, and 7-day history where available. Weather views
combine stored prior observations with forward forecasts. Data Lab provides bounded historical
queries and CSV export. Production retention rules limit database size and Supabase egress.

## Tech stack

- **Application:** Python 3.12, FastAPI, Uvicorn, HTML, CSS, JavaScript, and SVG charts
- **AI orchestration:** LangGraph with triage, persona planning, evidence-only writing, verification,
  bounded rewriting, and citation-enforcing editing
- **AI and retrieval:** deterministic SQL analytics, pgvector document index, BGE embeddings in the
  indexing worker, low-memory lexical retrieval on Render, and optional Groq explanations
- **Data:** PostgreSQL/Supabase, SQLAlchemy, psycopg, ERCOT, EIA, NWS, and RSS adapters
- **Operations:** Docker, Render, GitHub Actions ingestion/generation, pytest, and Ruff

## Repository structure

```text
GridBrief-AI/
├── .github/workflows/       # CI, scheduled ingestion, and edition generation
├── migrations/              # Ordered PostgreSQL and pgvector schema migrations
├── src/gridbrief/
│   ├── adapters/            # ERCOT, EIA, NWS, and RSS source adapters
│   ├── web_static/          # Website HTML, CSS, and browser JavaScript
│   ├── ai.py                # Ask AI intent planning, retrieval, and answer verification
│   ├── analytics.py         # Deterministic metric and window calculations
│   ├── graph.py             # LangGraph generation nodes and verification loop
│   ├── ingestion.py         # Source orchestration and persistence
│   ├── pipeline.py          # Edition planning and generation entry points
│   ├── repository.py        # Database access and persistence boundary
│   ├── retrieval.py         # pgvector and low-memory lexical retrieval
│   ├── scheduler.py         # Local refresh and breaking-update scheduling
│   └── web.py               # FastAPI application and public JSON endpoints
├── tests/                   # Unit, repository, migration, Ask AI, and web tests
├── Deliverables/            # Includes deliverables for submission
├── Dockerfile               # Slim Render-compatible production image
├── pyproject.toml           # Package metadata, dependencies, and tool configuration
├── render.yaml              # Render service and production environment configuration
├── requirements.txt         # Complete local development installation entry point
└── README.md                # Project setup, architecture, data, and verification guide
```

## Local setup

Requirements: Python 3.12, PostgreSQL 16 with pgvector (or a Supabase connection), and Node.js only
for the optional JavaScript syntax check.

```powershell
git clone <repository-url>
cd GridBrief-AI
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `GRIDBRIEF_DATABASE_URL` in `.env` to a dedicated PostgreSQL/Supabase database. Add an EIA key
for EIA ingestion. NWS does not require an API key, but `GRIDBRIEF_CONTACT_EMAIL` should identify the
application. Groq is optional; set its key, model, and `GRIDBRIEF_ALLOW_REMOTE_LLM=true` to enable
model-written explanations.

Initialize and populate the application:

```powershell
gridbrief migrate
gridbrief ingest all --hours 168
gridbrief index
gridbrief generate --role general
gridbrief generate --role market_analyst
gridbrief generate --role grid_operations
gridbrief-web
```

Open [http://localhost:8000](http://localhost:8000). API documentation is available at
[http://localhost:8000/api/docs](http://localhost:8000/api/docs).

For a lightweight web-only installation that does not perform local embedding or ERCOT ingestion:

```powershell
python -m pip install -e ".[web]"
$env:GRIDBRIEF_RETRIEVAL_BACKEND="lexical"
gridbrief-web
```

## Verification

Never run repository tests against the shared production database. Use a dedicated test database
with the pgvector extension, then run:

```powershell
ruff check src tests
python -m pytest -p no:cacheprovider
node --check src/gridbrief/web_static/site.js
gridbrief --help
gridbrief generate --help
docker build --tag gridbrief:local .
```

## Deliverable Links
- [Group 3 – GridBrief AI Tech Stack Document](Deliverables/Group%203%20-%20Gridbrief%20AI%20AI%20Tech%20Stack.pdf)
- [Group 3 - GridBrief AI Presentation Slides](Deliverables/Group%203%20-%20GridBrief%20AI%20Presentation.pptx)
- [Group 3 - GridBrief Demo Video](Deliverables/Group%203%20-%20GridBrief%20AI%20Demo.mp4)

