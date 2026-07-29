# GridBrief AI — Product Requirements Document (PRD)

**Version:** 2.1 — replication-ready website specification (semantic retrieval)
**Owner:** Group 3
**Scope of this milestone:** ERCOT-first, full build (final deliverable)
**Timeline:** 5 weeks · 6 contributors · agent-assisted build (Codex)
**Document purpose:** A build-ready spec precise enough to hand to a coding agent and split across a 6-person team without ambiguity.

---

## 1. Overview & Vision

GridBrief AI is an **Agentic RAG energy-intelligence website** that ingests energy-market data and text for a US RTO/ISO, grounds statements in retrieved public sources, and combines an operating-data workspace, a source-cited newsletter, and a conversational AI analyst in one production-oriented web experience.

We ship **ERCOT** end-to-end this milestone, but every interface is designed so a second ISO can be added by writing one new ingestion adapter — not by rewriting the pipeline.

**One-line pitch:** *"The scattered state of ERCOT — prices, load, fuel mix, outages, weather, and policy — turned into a trustworthy, cited briefing in minutes instead of hours."*

---

## 2. Problem, Goals, Non-Goals

### 2.1 Problem
Energy analysts track prices, load, generation mix, outages, grid advisories, weather, and regulatory activity across many portals, PDFs, and dashboards. It is slow to gather, easy to miss what matters, and hard to trust auto-summaries that don't cite sources.

### 2.2 Goals
1. Collect ERCOT structured data + relevant text (notices, news, weather, policy) via **APIs and feeds** (no scraping).
2. Store structured data in **PostgreSQL** and text in a **vector store** with shared metadata for joins.
3. Retrieve with **hybrid RAG** (semantic + structured lookups).
4. Generate a briefing via a **multi-agent LangGraph pipeline** with a self-verification loop.
5. **Cite every factual claim**; measurably minimize hallucination.
6. Support **role-personalized** editions (general + 2 personas).
7. Ship a responsive **FastAPI website** combining operating intelligence, interactive charts, a data lab, a newsletter studio/archive, evidence drawers, and Ask GridBrief AI.
8. Automate production refresh while showing data freshness and provenance to readers.
9. Make failures safe and understandable: never expose tracebacks, raw validation JSON, credentials, or JavaScript strings such as `[object Object]`.

### 2.3 Non-Goals (explicitly out of scope this milestone)
- Price/load **forecasting** or trading signals (this is a briefing, not a model; keep separate from any predictive work).
- Financial or trading advice.
- Real-time (<5 min) streaming; near-real-time batch is sufficient.
- Multi-ISO coverage shipped now (designed for, not delivered).
- User auth/accounts, payments, mobile apps.

### 2.4 Success Metrics (targets, measured in §11)
| Dimension | Target |
|---|---|
| Citation coverage (factual claims with a valid supporting source) | ≥ 90% |
| Groundedness (LLM-judge, 0–1) | ≥ 0.90 |
| Hallucination rate | ≤ 5% |
| Retrieval Hit Rate@5 on gold set | ≥ 0.85 |
| Topic classification F1 | ≥ 0.80 |
| End-to-end newsletter generation | < 5 min per edition |
| Data freshness at generation (grid data & notices) | ≤ 1 hour old |
| Breaking-update latency (item ingested → bulletin published) | ≤ 30 min |
| Demo | 3 role editions + live website + Ask AI |
| Warm page load | ≤ 3 s at p75 (free-tier cold start excluded) |
| Cached chart-window change | ≤ 300 ms perceived latency |
| Ask AI response contract | 100% string answers or readable error messages |
| Responsive layout | No horizontal overflow at 360, 768, 1024, and 1440 px |

---

## 3. Users & Personas

| Persona | Cares most about | Edition emphasis |
|---|---|---|
| **General** (default) | Balanced overview | All sections, balanced weight |
| **Market / Trading Analyst** | DAM/RTM prices, spreads, scarcity, ancillary clearing | Prices + scarcity events first; renewables as price driver |
| **Grid Operations & Reliability** | Load, forecasts, reserves, outages, constraints, frequency, advisories/EEA | Operating status and risks first; prices only as supporting context |

**Personalization mechanism (not separate pipelines):** a `role` parameter drives (a) topic weighting in ranking, (b) section ordering/selection, (c) writer-prompt framing. Same graph, different config.

These are presentation and retrieval personas, not authorization roles. Each must materially change the information selected—not merely reorder identical cards:

- **General / Executive:** current operating posture, the few largest changes, active risks, price context, renewable share, and a concise “what matters today.”
- **Market Analyst:** hub RT/DA prices, DART (`day-ahead − real-time`) spreads, volatility, ancillary services, scarcity context, load/fuel fundamentals, and market-risk watch.
- **Grid Operations & Reliability:** actual versus forecast load, reserve headroom, outages by type, capacity, storage behavior, frequency, weather threats, and operational-status chronology.

Persona acceptance test: for the same data window, at least 40% of section headings or highlighted metrics differ between any two personas. Shared facts may appear when genuinely relevant.

---

## 4. System Architecture

```mermaid
flowchart TB
    subgraph Ingestion["Ingestion Layer (deterministic ETL, scheduled)"]
        A1[ERCOT Public API\nvia gridstatus] --> N[Normalizer]
        A2[EIA API v2 / EIA-930] --> N
        A3[NWS api.weather.gov] --> N
        A4[RSS: ERCOT notices, EIA Today in Energy] --> N
        A5[PUCT filings / GDELT news optional] --> N
    end

    N --> PG[(Supabase Postgres\nstructured + time-series + editions + text)]
    N --> EMB[Embedding + Chunking]
    EMB --> VDB[(pgvector semantic index\nin Supabase Postgres)]

    subgraph Gen["Generation Graph (agentic, LangGraph)"]
        T[Triage / Classifier] --> P[Planner]
        P --> R[Retriever\nhybrid: vector + SQL]
        R --> W[Section Writers\nparallel]
        W --> V[Verifier / Critic]
        V -- unsupported claims --> W
        V -- pass --> ED[Editor / Assembler]
    end

    PG --> R
    VDB --> R
    ED --> STORE[(Editions + eval traces\nin Supabase Postgres)]
    STORE --> API[FastAPI application + JSON/CSV APIs]
    API --> WEB[Responsive intelligence website]
    API --> CHAT[Grounded Ask AI]
    STORE --> EVAL[Evaluation Harness]
```

**Design principles:** ingestion is deterministic ETL; derived calculations are deterministic Python/SQL; the LLM explains retrieved facts but never becomes the source of record. The browser consumes stable JSON contracts and never reads the database directly. In production, scheduled ingestion is separated from the web process so deploys and horizontal scaling cannot duplicate jobs.

---

## 5. Data Sources & Ingestion Spec

> **Rule:** APIs and feeds only. Register for all keys on **Day 1** — ERCOT auth is the critical-path risk.

| Source | Access | Auth | Data captured | Type | Cadence | Priority |
|---|---|---|---|---|---|---|
| **ERCOT Public API** (via `gridstatus`) | REST, base `api.ercot.com/api/public-reports` | Subscription key **+** 1-hour Bearer ID token (re-fetch on expiry) | DAM/RTM LMP & SPP by settlement point/hub/zone, system load by weather zone, wind/solar actual+forecast, ancillary clearing prices, resource outages, grid conditions/advisories | Structured | Hourly / daily batch | **P0** |
| **EIA API v2** (EIA-930) | REST, `api.eia.gov/v2` | Free API key | Hourly demand, net generation by fuel, interchange for **ERCO** balancing authority | Structured | Daily | **P0** (also ERCOT fallback) |
| **NWS** | REST, `api.weather.gov` | None | Active alerts + forecasts for Texas zones (heat/cold/storm) | Structured + short text | Every 15–60 min | **P0** |
| **ERCOT Market Notices** | RSS | None | Advisories, EEA notices, market messages | Text | Every 15–30 min | **P0** |
| **EIA Today in Energy** | RSS | None | Analyst-grade energy explainers | Text | Daily | **P1** |
| **PUCT filings / open meetings** | Feed / listing | None | Texas regulatory activity relevant to ERCOT | Text | Daily | **P1** |
| **GDELT / news** | REST | None (GDELT free) | Broad energy news mentions | Text | Daily | **P2 (optional)** |
| **FERC eLibrary** | Document listing | None | Federal filings | Text | — | **P3 (deprioritized — ERCOT is intrastate)** |

**Ingestion contract (every adapter implements this so new ISOs plug in):**
```python
def fetch(since: datetime, until: datetime) -> list[RawItem]: ...
# RawItem = {source, source_id, published_at, kind: "timeseries"|"document",
#            payload, url, raw_hash}
```

**Historical note (ERCOT):** Public API data starts **Dec 11, 2023**; older data needs the manual Data Access Portal. For a *current* briefing this is a non-issue — pull a rolling window (e.g., last 7–30 days).

**Accelerator:** use **`gridstatus`** for ERCOT ingestion. It wraps the endpoints above, handles token refresh + pagination, and supports other ISOs — directly serving the generalization goal. *Verify its license before including in the deliverable.*

### 5.1 Scheduling, Freshness & Orchestration

**Core principle: ingestion cadence ≠ newsletter cadence.** Data is polled *continuously on a schedule* so the store is always current; editions are generated on top of an already-fresh store. A daily briefing does **not** mean a once-a-day pull.

**Polling schedule (per source group):**

| Source group | Poll frequency | Why |
|---|---|---|
| ERCOT prices / load / fuel mix / wind / solar | Hourly | Matches posting cadence; sufficient for a daily brief |
| ERCOT market notices & advisories (EEA, etc.) | Every 15–30 min | Same-day grid events must surface the day they happen |
| NWS active alerts | Every 15–60 min | Alerts appear/expire quickly |
| EIA-930 (demand, generation by fuel) | Daily | Source publishes with ~1-day lag |
| EIA Today in Energy / PUCT / news (optional) | Daily | Slow-moving narrative/regulatory text |

**Idempotent & incremental (never re-ingest the same item):**
- Per-source **watermark** drives `fetch(since, until)`; only new records are pulled.
- **De-dup** on `raw_hash` / `source_ref`; **upsert** time-series on `(iso, metric, settlement_point, ts)` and documents on `source_ref`.
- **Revisions:** ERCOT/EIA restate values, so re-fetch a rolling 3–7 day window each cycle and upsert to catch corrections.
- **Incremental indexing:** only new/changed documents are chunked and added to the active retrieval backend.

**On-demand freshness:** a user-triggered edition first runs an incremental pull (or verifies the freshness watermark is within SLA), so it is never stale. Every edition stamps a `data_as_of` timestamp shown to the reader.

**Orchestration reality:** "continuous" here means *frequent scheduled polling*, not event streaming — and it runs on **GitHub Actions scheduled workflows** (free, no server to keep alive): an ingest workflow every 15–30 min and a daily generation workflow. Set these up in Week 1 (Step 0) so scheduling isn't a Week-5 surprise. No always-on worker exists — the breaking check runs at the end of each ingest workflow, so breaking latency is bounded by the poll interval.

---

## 6. Data Model

### 6.1 PostgreSQL (structured + metadata)
```
sources(id, name, kind, base_url)
raw_items(id, source_id, source_ref, kind, published_at, url, raw_hash, ingested_at)
timeseries(id, iso, metric, settlement_point, ts, value, unit, source_id)
   -- metric ∈ {lmp, spp, system_load, wind_gen, solar_gen, fuel_mix_*, as_price_*, ...}
documents(id, source_id, title, url, published_at, text, topic, importance, chunk_ids[])
editions(id, iso, role, cycle_date, generated_at, status, markdown, html, json)
edition_claims(id, edition_id, claim_text, cited_chunk_ids[], verified bool, groundedness float)
eval_runs(id, edition_id, metric, value, detail_json, created_at)
ingestion_watermarks(id, source_id, last_success_at, window_end, status, detail_json)
ingestion_runs(id, source_id, started_at, completed_at, status, inserted, updated, skipped, error)
breaking_triggers(id, source_ref, topic, severity, fingerprint, fired_at, cooldown_until)
```

Required constraints/indexes: unique source name; unique `(source_id, source_ref)` raw item; unique raw hash where applicable; unique `(iso, metric, settlement_point, ts)` time-series observation; indexes on time-series metric/location/time, documents published time/topic, editions role/generated time, and watermark source. Foreign keys use deliberate cascade behavior. Store numeric energy values with enough precision to reproduce calculations; convert to JSON-safe floats only at the API boundary.

### 6.2 Retrieval index and chunks

- **Semantic retrieval via pgvector** in the same Supabase Postgres: a `chunks` table `(chunk_id, document_id FK, iso, text, embedding vector(768), source, topic, published_at, url)` with an HNSW index on `embedding`. Retrieval is true vector similarity, not keyword matching — including in the free cloud deployment.
- Embeddings are produced by `sentence-transformers` (`BAAI/bge-base-en-v1.5`, 768-dim) in the ingest/index step (runs in GitHub Actions for cloud, locally for dev) — no separate embedding service to host. Chunk text + metadata are also persisted in Postgres so results are fully citable.
- Both the cloud pgvector backend and an optional offline Chroma backend expose the **same** `search(query, filters, k)` interface and return the same evidence contract, so retrieval is a config swap, not a rewrite.
- Chunking: approximately 500–800 tokens with 10–15% overlap. Every chunk carries `chunk_id`, `document_id`, ISO, source, topic, published time, and official URL.
- **Shared key = `document.id`**, so every retrieved chunk resolves to a citable source row.

---

## 7. Agentic Generation Pipeline (LangGraph)

### 7.1 Shared graph state
```python
class GraphState(TypedDict):
    iso: str
    role: str
    edition_mode: str      # scheduled_daily | on_demand | breaking
    window_start: datetime # rolling trailing-24h start
    cycle_date: date
    raw_items: list        # candidate items for this cycle
    classified: list       # + topic, importance score
    plan: dict             # sections to include, ordering (role-aware)
    retrieved: dict         # section -> grounding context (chunks + timeseries)
    drafts: dict            # section -> draft with inline [cite:doc_id]
    verification: dict      # section -> {passed, unsupported_claims[]}
    revision_count: int
    edition: dict           # final markdown/html/json
```

### 7.2 Nodes
1. **Triage / Classifier** — LLM tags each item into `{grid, prices, renewables, weather, policy, market}` and scores newsworthiness 0–1. (Eval'd for F1.)
2. **Planner** — role-aware: selects top items per topic, decides which sections this cycle warrants (e.g., skip "Notable Events" if nothing crossed threshold), sets ordering. *This is genuine agentic decision-making, not a fixed template.* In `breaking` mode it narrows scope to just the triggering item/cluster and emits a single focused bulletin.
3. **Retriever (tool node)** — for each planned section, **hybrid retrieval**: vector search over `ercot_docs` **+** `sql_query` / `get_timeseries` tools for the numbers. Returns grounding bundle.
4. **Section Writers (parallel)** — draft each section from retrieved context only, inserting inline `[cite:doc_id]` markers. Refuse to state numbers not present in the bundle.
5. **Verifier / Critic** — claim-level groundedness check against cited chunks; emits `unsupported_claims`. **Conditional edge:** if any section fails → route back to its Writer with the specific failing claims; cap at `MAX_REVISIONS = 2`, then drop unsupported claims and flag.
6. **Editor / Assembler** — merges sections, enforces citation coverage, applies role tone, renders `markdown` / `html` / `json`, and writes `editions` + `edition_claims`.

### 7.3 Agent tools (function-calling)
- `vector_search(query, filters, k)`
- `sql_query(sql)` / `get_timeseries(metric, settlement_point, start, end)`
- `chart_spec(metric, range)` → returns a spec/series contract the website renders (agent never draws pixels).

**Why this is resume-credible:** tool use, role specialization, dynamic planning, conditional routing, and a self-correction loop — the defining traits of an agentic system, all gradeable and demoable.

---

## 8. Newsletter Generation Spec

**Default sections (Planner may reorder/drop by role):**
1. **Executive Summary** — 3–5 bullets, each cited.
2. **Grid Conditions** — load vs. forecast, reserves, advisories/EEA.
3. **Prices** — DAM/RTM SPP highlights, spikes, ancillary clearing.
4. **Renewables & Fuel Mix** — wind/solar output vs. forecast, mix shifts.
5. **Weather Impact** — active alerts and grid-relevant outlook.
6. **Policy / Regulatory** — PUCT / ERCOT notices worth knowing.
7. **Notable Events** — outages, records, anomalies (conditional).
8. **Outlook** — clearly labeled as qualitative, source-grounded, not a forecast.

**Citation format:** inline superscripts in the reader view resolving to a **Sources** list (title + publisher + date + link). Every number and named event must carry a citation or it is cut by the Verifier.

**Cadence:** **daily** scheduled edition + on-demand generation (user triggers a cycle for a chosen role/date range). Ingestion runs continuously underneath (see §5.1), so a daily edition always reflects same-day data; each edition stamps a `data_as_of` timestamp.

### 8.1 Publish Timing & Intraday Updates

**Edition window:** every edition covers a **rolling trailing 24 hours** `[now − 24h, now]` — not a fixed calendar or operating day. `data_as_of = now` on every render (scheduled, on-demand, or breaking).

**Three generation modes — one graph, three entry points** (`edition_mode` in the graph state):

| Mode | Trigger | Scope | Output |
|---|---|---|---|
| `scheduled_daily` | Cron (canonical morning run) | Full section set over trailing 24h | Complete consolidated brief |
| `on_demand` | User clicks **Generate** | Full section set over chosen window (default trailing 24h) | Complete brief |
| `breaking` | High-severity item detected between scheduled runs | Scoped to the triggering item/cluster | Short cited **Breaking Update** bulletin, published immediately and folded into the current rolling edition |

**Breaking trigger predicate** — an item fires an intraday update if **any** hold:
- ERCOT grid condition at **Watch or EEA 1/2/3** (advisory escalation).
- Real-time scarcity / major price event (RTM SPP crosses a configured threshold, or ORDC scarcity adder active).
- Large unplanned generation/transmission outage above a capacity threshold.
- NWS extreme heat/cold/storm warning over the ERCOT footprint.
- Classifier importance ≥ 0.9 in `{grid, prices}`.

**De-bounce & cost control (critical — the poller runs every 15–30 min):**
- **Cooldown:** at most one breaking bulletin per topic per rolling hour.
- **Clustering:** related items merge into one bulletin, not one per record.
- **Idempotency:** every triggering item is logged; re-polling and minor revisions never re-fire. A *material escalation* (e.g., Advisory → EEA 2) may re-fire once.
- Bulletins pass the **same Verifier citation gate** as full editions — breaking speed never bypasses grounding.

**Reader experience:** the current rolling edition shows `Last updated: HH:MM CT`; breaking bulletins appear as timestamped, flagged entries pinned to the top and into "Notable Events." The next `scheduled_daily` run reconciles them into the consolidated brief.

---

## 9. Production Website Requirements

The deliverable is a website, not a collection of notebook charts or a generic admin dashboard. It must present dense grid information progressively: a useful overview in under 30 seconds, with deeper investigation one click away.

### 9.1 Information architecture

The single-page MVP has anchored sections; these may later become routes without changing API contracts:

1. **Header and freshness bar** — product identity, persona switcher, data-as-of time, source health, preferences, and wallboard mode.
2. **Market ribbon** — continuously moves right-to-left like a market ticker; duplicates its item set for a seamless loop; values include system load, RT price, DA price, outages, wind, and solar. Pause animation for `prefers-reduced-motion`.
3. **Daily Desk** — decision-oriented priorities, anomalies/percentiles, upcoming operating calendar, and user-configured threshold alerts.
4. **Operating pulse** — current load, window range, supporting sparkline, and clear units/source time.
5. **Role-aware intelligence** — KPIs and sections specific to General, Market Analyst, or Grid Operations & Reliability.
6. **Fuel mix** — one compact labeled chart per fuel; current value, window average, coverage note, source, and expand control. The 24h/48h/7d switch must update both charts and summary numbers.
7. **Reliability and risk** — reserves, capacity, frequency, storage, outages, active risk clusters, affected areas, announcement/update/effective/expiry times, and impact narrative.
8. **Market and regional views** — hub prices/DART, ancillary services, weather-zone load/temperature, and click-through investigation.
9. **Newsletter** — latest role-specific edition, unobtrusive evidence controls, quality summary, archive, print, and standalone HTML export.
10. **Data Lab** — metric, location, and range selectors; exact observations; min/max/average; detailed chart; table; CSV export; saved browser-local views.
11. **Ask GridBrief AI** — persistent conversation thread with suggestions, user message immediately moved into the thread, pending state, answer context/confidence, optional chart action, and evidence drawer.
12. **Evidence drawer** — source publisher/full official name, title, timestamp, metric/location/value, and evidence type. Do not expose inaccessible API URLs as reader links.

### 9.2 Visual and interaction design

- Responsive at 360/768/1024/1440 px; keyboard reachable; visible focus states; semantic headings; contrast meets WCAG AA.
- Use a deliberate energy-market visual system: dark neutral canvas, restrained bright status accents, tabular numerals, dense but breathable cards, and subtle motion.
- Never rely on color alone for severity or direction; pair color with text/icon/arrow.
- Datetimes display in the configured market timezone (default Central Time) with timezone abbreviation. Raw ISO timestamps remain in APIs and exports.
- Units always accompany values (`MW`, `Hz`, `$/MWh`, `%`). Define acronyms on first meaningful use. Clarify that EIA balancing-authority code `ERCO` means ERCOT; reader-facing labels should say ERCOT.
- Citations must not interrupt prose. Strip inline citation tokens from the reading flow and replace them with a compact **Evidence N** control at paragraph/answer level.
- Empty, stale, loading, partial-coverage, rate-limited, and error states are designed states—not blank cards.
- A chart window change uses cached/prefetched data when possible, disables duplicate clicks while loading, and retains the old chart until replacement data is ready.
- Expandable charts use the same selected time window as compact charts and provide exact pointer/keyboard inspection.

### 9.3 Required browser state

The browser keeps only non-sensitive state: selected persona, selected window, preferences/thresholds, saved Data Lab views, short Ask AI history, request cache, and in-flight request registry. Secrets and database credentials never enter client JavaScript.

Every value crossing into HTML uses a single safe text/escaping function. Objects and validation payloads are converted to readable text; the literal `[object Object]` is a release-blocking defect. Ask history is compacted before transport and again on the server.

### 9.4 API surface (minimum contract)

| Method/path | Purpose | Required response behavior |
|---|---|---|
| `GET /api/health` | Process liveness | Small stable JSON; no DB dependency |
| `GET /api/ready` | Deployment readiness | Fails until schema/core data/latest edition are usable |
| `GET /api/status` | Source freshness and warnings | Per-source timestamps/status without credentials |
| `GET /api/config` | Public feature flags | Ask availability, generation availability, public mode |
| `GET /api/metrics?hours=24` | Core chart series | `from`, `to`, metric-keyed arrays, source/unit/location |
| `GET /api/intelligence?hours=24` | Derived website intelligence | KPIs, operations, changes, alerts, market and regional data |
| `GET /api/daily-use` | Daily Desk | priorities, anomalies, calendar |
| `GET /api/edition/latest?role=general` | Latest newsletter | sections, sources, quality, `data_as_of` |
| `GET /api/editions?limit=N` | Archive | newest-first edition summaries |
| `GET /api/data/series` | Data Lab | validated metric/range/location and bounded points |
| `GET /api/data/export.csv` | Export | matching CSV attachment |
| `POST /api/ask` | Grounded conversational answer | Always a string `answer`, sources, plan, confidence, verification, optional chart |
| `POST /api/generate` | Privileged generation | Admin-only in public mode; readable failure contract |
| `GET /api/automation` | Scheduler ownership | Explicitly reports web-managed vs externally managed |

All endpoints validate ranges and cap response size. Public errors contain a human-readable `detail`; logs retain the technical exception with secrets redacted. Add security headers, request timeouts, and rate limits to costly public endpoints.

### 9.5 Free public deployment

- **Database:** Supabase managed PostgreSQL. Use its pooled URL with SSL for web/Actions and direct URL for migrations if required.
- **Web:** Render free web service running the FastAPI application from a Dockerfile. Render supplies HTTPS and deploys checked commits from `main`.
- **Automation:** GitHub Actions owns scheduled ingestion/generation in public production. Set web-process scheduling off to avoid duplicate jobs.
- **Retrieval:** **pgvector semantic retrieval** on Supabase in the free cloud deployment (embeddings generated in the scheduled ingest/index job). Local dev may instead use an offline Chroma backend behind the same `search()` interface.
- **Secrets:** store actual values separately in GitHub Actions and Render secret settings. Never commit `.env`.

Required production flags:

```text
GRIDBRIEF_PUBLIC_MODE=true
GRIDBRIEF_AUTOMATIC_REFRESH=false
GRIDBRIEF_RETRIEVAL_BACKEND=pgvector
GRIDBRIEF_REQUIRE_FRESH_DATA=true
```

Free services can cold-start and scheduled jobs can run late. Show freshness honestly and describe the release as near-real-time public beta, not guaranteed real-time infrastructure.

---

## 10. Tech Stack (locked)

*Core architectural choices are locked for team compatibility. Model slugs and free-host availability must be re-verified at implementation time because providers rotate offerings.*

| Layer | Locked choice | Notes |
|---|---|---|
| Language | **Python 3.12** | 3.11+ acceptable |
| Deps / lint / test | **pip + `pyproject.toml`**, **ruff**, **pytest** | Editable install with named optional dependency groups |
| Config / secrets | **Pydantic Settings + env vars** (local `.env`) + GitHub Actions/Render secrets | Nothing secret in the repo |
| Structured store | **PostgreSQL** (managed on **Supabase**), **SQLAlchemy 2 + psycopg 3** | Versioned SQL migrations; transactional repository/service layer |
| Retrieval store | **pgvector on Supabase** (semantic, cloud + local) behind one `search()` interface | Optional offline Chroma backend for local dev only |
| Shared types | **dataclasses/domain types + Pydantic v2 request/config models** | External payloads normalize before persistence |
| Ingestion | **`gridstatus`** where practical, **requests/httpx**, **feedparser** | Adapter interface isolates authentication/pagination |
| Scheduling | **GitHub Actions** scheduled workflows (cron) | Ingest every 15–30 min + breaking check + daily gen; no always-on worker |
| Embeddings | **`BAAI/bge-base-en-v1.5` (768-dim) via `sentence-transformers`**, generated in the ingest/index job | Runs in cloud (Actions) and local; `nomic-embed-text` via Ollama is an optional offline alternative |
| Agent framework | **LangGraph** (+ `langchain-core`) | State machine, conditional edges, verify→revise loop |
| Inference provider | **Groq** | Open-weight models, fast/cheap |
| Generator model | **Configurable Groq chat model** through a small OpenAI-compatible client | Default chosen from the provider's current supported list; deterministic routes answer common lookups |
| Web/API | **FastAPI + Uvicorn**, server-rendered HTML shell, native HTML/CSS/JavaScript/SVG | No frontend build system required for MVP |
| Charts | Native SVG generated from API series | Compact and expanded views share scale/window logic |
| Hosting | **Supabase** (Postgres) · **GitHub Actions** (ingest/gen) · **Render** (FastAPI web) | Free public-beta topology; push checked `main` commits to deploy |
| VCS / CI | **GitHub** + **GitHub Actions** (pytest on PRs) | — |

---

## 11. Evaluation Plan

**Reuse the harness from the prior RAG build and extend it.**

**Retrieval:** Hit Rate@5, MRR, Recall — on a hand-built **gold set of ~30–50 query→expected-source pairs**.
**Generation:** Groundedness, Factual Accuracy, Hallucination Rate (LLM-as-judge at scale + spot human check).
**New for GridBrief:**
- **Topic Classification F1** — labeled set of ~100 items.
- **Citation Coverage** — % of factual claims with ≥1 valid supporting citation.
- **Source Attribution Precision** — cited source actually supports the claim.

**Business/UX:** time-to-brief vs. manual baseline; short usefulness + trust survey (does citation presence raise trust?).

**Gating:** an edition fails CI if citation coverage < 90% or hallucination > 5% on its claims.

---

## 12. Work Division — Exactly 6 People

Six people work concurrently behind frozen contracts; this is not a six-step handoff where Person 6 waits four weeks. Each person owns production code, tests, documentation, and a demo checkpoint for one bounded workstream.

| # | Primary ownership | Main paths/modules | Replication deliverable |
|---|---|---|---|
| 1 | **Platform, security, CI/CD, evaluation** | config, Docker, migrations runner, `.github/workflows/`, deployment docs, evaluation/quality tests | Supabase/Render/Actions environments, secret matrix, CI gates, health/readiness, monitoring checklist, gold-set and release report |
| 2 | **Database and domain model** | models, DB/session layer, repository, migrations, persistence, seed fixtures | Complete schema, idempotent migrations, upsert/query APIs, seed database, retention/index plan |
| 3 | **Data ingestion and automation** | adapters, ingestion, normalization, polling, operations, freshness, scheduler | ERCOT/EIA/NWS/RSS adapters, normalized canonical metrics, watermarks/revisions, source-status reporting, scheduled refresh |
| 4 | **Retrieval, analytics, and citations** | chunking, indexing, retrieval/vector store, charting, deterministic analytics, export | Hybrid retrieval, calculation evidence, citation IDs, chart-ready series, Data Lab query/export services |
| 5 | **AI generation and Ask AI** | domain/graph/pipeline/workflow, LLM client/cache, AI router, quality, breaking services | Personas, planner/writer/verifier loop, query understanding, deterministic market answers, conversational prompt, claim removal/confidence, breaking updates |
| 6 | **Web product and UX** | FastAPI routes/schemas plus `web_static/` HTML/CSS/JS | Responsive production website, all sections in §9, API integration, chart interactions, safe renderer, accessibility and browser acceptance tests |

### 12.1 Contract freeze (end of Day 2)

The team agrees and records these before feature work:

- canonical metric names, locations, units, timezone, and `RawItem`/time-series/document types;
- database tables and which person may change each migration;
- repository and retrieval method signatures;
- edition JSON, source/evidence JSON, Ask AI request/response, and chart-series response;
- persona enum and section configuration;
- error envelope, freshness definition, and environment-variable names.

Contract changes require a short architecture note and approval from every affected owner. Person 1 maintains the contract test suite; Person 2 owns migration ordering; Person 6 may mock every API from checked-in JSON fixtures.

### 12.2 Parallel implementation plan

| Week | Person 1 | Person 2 | Person 3 | Person 4 | Person 5 | Person 6 | Integrated exit criterion |
|---|---|---|---|---|---|---|---|
| **1 — foundation** | Repo, CI, environments, secret templates | Schema, migrations, seed DB | Adapter fixtures/contracts; register keys Day 1 | Retrieval interfaces and fixture index | Graph/query-plan interfaces and prompt threat model | Wireframe, design tokens, static shell, API mocks | CI green; app shell and seed APIs run locally |
| **2 — vertical data slice** | Supabase dev project, eval skeleton | Production repository/upserts | ERCOT/EIA/NWS core ingestion + watermarks | pgvector semantic retrieval, analytics and citation IDs | First grounded General edition | Operating pulse, range switch, fuel charts against fixture API | One real source flows ingest → DB → API → website |
| **3 — intelligence** | Security/rate-limit tests, Actions schedules | Performance indexes and edition persistence | Extended ERCOT, RSS, freshness/status | Data Lab, export, evidence bundles | Three personas, verifier loop, Ask AI, DART/market terminology | Role views, risks, evidence drawer, Ask UI | Three materially distinct cited editions; grounded Ask answer |
| **4 — production product** | Render staging, readiness and observability | Migration rehearsal/backup plan | Failure recovery, revisions, stale-source behavior | Retrieval eval and chart coverage | Breaking updates, follow-ups, malformed-output defenses | Newsletter studio/archive, responsive/accessibility pass | Full staging acceptance suite passes |
| **5 — launch** | Full eval, go-live/runbook, demo | Query tuning and migration sign-off | Freshness burn-in | Gold-set tuning | Prompt/eval tuning | Cross-browser polish/performance | Public beta deployed; §20 acceptance checklist signed |

### 12.3 Branching and integration rules

- Protect `main`; require CI and one owner review. Use small feature branches and PRs, not six long-lived branches.
- No secrets, `.env`, production dumps, model caches, or generated build output in Git.
- Every PR includes tests and updates its contract/docs when behavior changes.
- Database changes are forward migrations; never rewrite a migration already applied to shared Supabase.
- Person 6 uses fixture APIs until backend endpoints exist; backend owners preserve those fixture contracts.
- Integrate a thin end-to-end slice every week. A component is not “done” if it works only in isolation.
- Assign the strongest agent/prompt engineer to Person 5; Person 1 pairs on evaluation and Person 4 pairs on grounding.

---

## 13. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **ERCOT API onboarding delay** (key + token flow) | Blocks P0 data | Register Day 1; `gridstatus` handles token refresh; **EIA-930 covers ERCOT structured data as fallback** |
| Groq free-tier rate limits/model retirement | Slow or blocked generation | Cache exact prompts; bound output; retry with backoff; keep model slug configurable and validate it before release |
| Hallucinated numbers/events | Trust failure | Writers restricted to retrieved context; Verifier loop; CI citation gate |
| News has no clean API | Missing narrative layer | RSS feeds are the intended mechanism (not scraping); GDELT optional |
| 6 people → merge chaos | Lost time | Strict workstream ownership + the interfaces in §5/§6/§7; PR review |
| Scope creep toward forecasting | Derails milestone | §2.3 non-goals are firm; outlook stays qualitative |
| Free-tier cold starts (Render web spins down when idle; Actions cron can run late) | Slow first load; freshness lag | Warm the app before a demo; GitHub Actions keeps ingesting on schedule; show freshness honestly (`data_as_of` + per-source health) and describe the release as near-real-time public beta, not guaranteed real-time |
| Intraday updates spam readers / spike LLM cost | Noise + budget overrun | Per-topic hourly cooldown, item clustering, idempotent trigger log, severity thresholds |

---

## 14. Future Work (post-milestone)
- Add a second ISO by writing one ingestion adapter (interface already defined).
- Add reranking on top of pgvector; email delivery; user accounts and saved preferences; feedback-driven ranking.

---

## 15. Reference Repository Layout

The names may vary, but a replica must preserve these boundaries. Avoid one giant application module.

```text
repo/
├─ .github/workflows/       # CI and scheduled refresh
├─ migrations/              # ordered forward SQL migrations
├─ specs/                   # PRD, API contracts, architecture notes
├─ src/gridbrief/
│  ├─ adapters/             # one module per external source
│  ├─ config.py             # environment-backed settings
│  ├─ domain.py             # canonical types/enums
│  ├─ models.py             # SQLAlchemy persistence models
│  ├─ db.py                 # engine/session/schema bootstrap
│  ├─ repository.py         # transactional persistence API
│  ├─ ingestion.py          # adapter orchestration
│  ├─ normalization.py      # source payload → canonical records
│  ├─ polling.py            # windows/watermarks/revision pulls
│  ├─ freshness.py          # SLA and source health
│  ├─ chunking.py           # deterministic document chunks
│  ├─ indexing.py           # incremental retrieval index
│  ├─ retrieval.py          # common search interface
│  ├─ ai.py                 # question understanding + deterministic answers
│  ├─ llm.py                # model client, retries, cache, output bound
│  ├─ graph.py/pipeline.py  # classify/plan/write/verify/edit
│  ├─ quality.py            # claim/citation checks
│  ├─ evaluation.py         # offline metrics and release gates
│  ├─ scheduler.py          # local scheduler only
│  ├─ operations.py         # refresh/generate application services
│  ├─ web.py                # FastAPI schemas, routes, security
│  └─ web_static/           # index.html, site.css, site.js
├─ tests/                   # mirrors modules + contract/integration tests
├─ Dockerfile
├─ render.yaml
├─ .env.example
├─ pyproject.toml
└─ README.md / deployment runbook
```

## 16. Replication Runbook

### 16.1 Accounts and credentials

Create these before coding against live services:

1. GitHub repository and Actions access.
2. Supabase PostgreSQL project; record pooled and direct database URLs.
3. Render account connected to GitHub.
4. EIA Open Data key.
5. Groq API key and a currently supported chat model.
6. Monitored contact email for NWS request etiquette.
7. ERCOT API Explorer subscription key/username/password if authenticated operational datasets are required. The application must still run in a documented degraded mode without them.
8. Optional NOAA CDO and EPA CAMPD keys for extended context.

Create `.env.example` with names and safe defaults only:

```dotenv
GRIDBRIEF_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/gridbrief
GRIDBRIEF_ISO=ERCOT
GRIDBRIEF_TIMEZONE=America/Chicago
GRIDBRIEF_FRESHNESS_MINUTES=60
GRIDBRIEF_REQUIRE_FRESH_DATA=true
# --- Local-dev defaults below. PRODUCTION overrides these (see 9.5) with ---
# PUBLIC_MODE=true, AUTOMATIC_REFRESH=false (GitHub Actions owns scheduling),
# RETRIEVAL_BACKEND=pgvector. The local/prod difference is intentional — do not "fix" it.
GRIDBRIEF_AUTOMATIC_REFRESH=true
GRIDBRIEF_PUBLIC_MODE=false
GRIDBRIEF_CONTACT_EMAIL=you@example.com
GRIDBRIEF_RETRIEVAL_BACKEND=pgvector
GRIDBRIEF_EMBEDDING_MODEL=BAAI/bge-base-en-v1.5
# Optional offline retrieval only (set RETRIEVAL_BACKEND=chroma to use these):
GRIDBRIEF_CHROMA_PATH=./chroma_data
GRIDBRIEF_OLLAMA_BASE_URL=http://localhost:11434
GRIDBRIEF_GROQ_MODEL=<currently-supported-model>
GRIDBRIEF_LLM_CACHE_PATH=build/llm_cache.json
# GRIDBRIEF_ADMIN_API_KEY=
# GRIDBRIEF_EIA_API_KEY=
# GRIDBRIEF_GROQ_API_KEY=
# GRIDBRIEF_ERCOT_SUBSCRIPTION_KEY=
# GRIDBRIEF_ERCOT_API_USERNAME=
# GRIDBRIEF_ERCOT_API_PASSWORD=
# GRIDBRIEF_NOAA_CDO_TOKEN=
# GRIDBRIEF_EPA_CAM_API_KEY=
```

Actual values go only in local `.env`, encrypted GitHub Actions secrets, and Render secrets. Rotate any credential ever pasted into a screenshot, issue, chat, log, or commit.

### 16.2 Local bootstrap

PowerShell reference sequence:

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
python -m pip install -e ".[dev,web,ercot,local-rag]"
gridbrief init-db
gridbrief migrate
```

If using hosted Supabase locally, set `GRIDBRIEF_DATABASE_URL` to its pooled SQLAlchemy/psycopg URL and skip the local PostgreSQL container. pgAdmin is a client and does not need to stay open; the actual database service must be reachable.

Semantic retrieval (default) needs no extra service — `gridbrief index` embeds documents with `sentence-transformers` (`BAAI/bge-base-en-v1.5`) into pgvector:

```powershell
gridbrief index
```

Optional offline alternative (Chroma + Ollama), only if you set `GRIDBRIEF_RETRIEVAL_BACKEND=chroma`:

```powershell
ollama pull nomic-embed-text
ollama serve
gridbrief index
```

If `ollama` is not recognized, restart the terminal, locate the installed executable, add its directory to user `PATH`, and verify with `ollama --version`. The cloud public beta uses `GRIDBRIEF_RETRIEVAL_BACKEND=pgvector` and does not depend on Ollama.

### 16.3 First data load

Run sources separately so failures are attributable:

```powershell
gridbrief ingest ercot --hours 168
gridbrief ingest nws --hours 24
gridbrief ingest eia --hours 168
gridbrief ingest ercot_extended --hours 30
gridbrief index
gridbrief generate --role general
gridbrief generate --role market_analyst
gridbrief generate --role grid_operations
gridbrief evaluate --edition-id 1
gridbrief archive
```

Adapters must log source, requested window, page count, inserted/updated/skipped counts, final watermark, duration, and a redacted error. A non-critical optional source may fail without losing successful core-source transactions. Required-source freshness can still block publication.

### 16.4 Run the application

```powershell
gridbrief-web
```

Open `http://127.0.0.1:8000`. In local mode, the web process may own background scheduling. Verify `/api/health`, `/api/ready`, `/api/status`, and `/api/automation` before judging frontend defects.

Local definition of working:

- core page and static assets return HTTP 200;
- selected time windows return distinct `from`/`to` values and recomputed summaries;
- all fuel charts have labels and an expanded view;
- latest editions exist for all three personas;
- evidence controls open official publisher metadata without obstructing prose;
- Ask AI answers RT, DA, DART, load, reserves, outages, frequency, wind, solar, storage, and follow-up-location questions;
- refresh/freshness status identifies stale sources rather than silently showing old values.

### 16.5 Automated refresh ownership

There must be exactly one scheduler owner per environment:

- **Developer laptop:** web process or `gridbrief scheduler`, not both.
- **Public beta:** GitHub Actions; Render sets `GRIDBRIEF_AUTOMATIC_REFRESH=false`.
- **Paid production:** a dedicated worker/managed scheduler; web instances never poll.

The scheduled workflow performs core ingest, optional ingest, indexing when applicable, breaking evaluation, daily edition generation, and freshness reporting. Use concurrency control so a delayed run cannot overlap the next run. Workflow steps should preserve partial source success and fail the job only when a required release condition is unmet.

### 16.6 Public deployment sequence

1. Run lint/tests locally and push a reviewed commit to GitHub.
2. Create Supabase, initialize schema with the direct/migration-safe URL, and confirm indexes.
3. Add database/source/model values to GitHub Actions **Secrets**.
4. Manually run the refresh workflow; confirm core rows, watermarks, and at least one published edition.
5. Create a Render Blueprint from `render.yaml`; add the same database/model/contact secrets plus a generated admin key.
6. Set the four production flags from §9.5.
7. Wait for `/api/ready`, not merely `/api/health`.
8. Run the smoke matrix in §20 in a private browser window.
9. Verify GitHub Actions reports external scheduler ownership and Render does not run duplicate jobs.
10. Enable provider usage alerts, database backups appropriate to the tier, and a monthly credential-rotation review.

## 17. Ask GridBrief AI Requirements

Ask AI is a grounded interface to stored evidence and deterministic analytics—not a general chatbot and not a trading advisor.

### 17.1 Question understanding

The router identifies intent, subject, location, time window, persona, and direct-lookup versus analytical mode. Required vocabulary includes:

- RT/real-time/spot/wholesale price; DA/day-ahead; SPP/LMP; DART as **day-ahead minus real-time**;
- ERCOT hubs North, South, West, and Houston; system and weather zones;
- load/demand/consumption/electricity use; forecasts; reserve/headroom;
- planned/unplanned/forced/total outages; capacity; grid frequency;
- wind, solar, fuel mix, battery/storage/BESS; weather and reliability;
- common misspellings and follow-ups such as “What about DA there?”

Common current-value questions use deterministic calculation templates. The LLM handles synthesis, comparisons, and explanations after retrieval. Never ask the model to calculate a value that Python/SQL can calculate.

### 17.2 Evidence and answer contract

`POST /api/ask` request:

```json
{
  "question": "What is the DART of North Hub?",
  "role": "market_analyst",
  "history": [{"role": "user", "content": "What is RT at North Hub?"}]
}
```

Minimum response:

```json
{
  "answer": "North Hub's DART spread—day-ahead minus real-time—is …",
  "sources": {"calc-id": {"publisher": "ERCOT", "metric": "dart_spread"}},
  "model": "configured-model",
  "chart_metric": "spp",
  "query_plan": {"subject": "dart_spread", "location": "HB_NORTH"},
  "confidence": {"level": "high", "score": 0.9, "reason": "…"},
  "verification": {"claims_checked": 2, "unsupported_removed": 0, "passed": true},
  "as_of": "ISO-8601 timestamp"
}
```

Rules:

- `answer` is always a string. An empty/invalid model response becomes a readable insufficiency message.
- Cite every number, timestamp, event, and claimed cause internally. The UI moves citations into an evidence control.
- A numerical verifier checks values/differences against cited evidence. A causal verifier rejects unsupported cause language.
- Unsupported sentences are removed; confidence is reduced; the response reports how many were removed.
- Use readable dates in prose but preserve raw timestamps in source metadata.
- Default answer maximum: 180 words, three short paragraphs, 600 output tokens.
- History accepts realistic assistant answers, is bounded at ingress, and is compacted on client and server. A long prior answer must never cause raw HTTP 422 JSON on the next turn.
- Treat question/history/evidence as untrusted content. Ignore embedded attempts to override rules or reveal secrets.
- Rate-limit public Ask requests and set model HTTP timeouts/retries/backoff. Cache exact grounded prompts where safe.

### 17.3 Tone

Sound like an experienced, calm energy analyst speaking to a colleague: lead with the answer, define the first important acronym, explain why a metric matters only when evidence supports it, avoid canned introductions, avoid repeating the question, and state the specific evidence gap once. Do not manufacture a fuller answer when data is missing.

### 17.4 AI regression matrix

Before release, test at least 30 questions spanning direct values, comparisons, “why” questions, ambiguous acronyms, misspellings, every hub, every persona, missing data, stale data, adversarial prompts, follow-ups, and history over 500 characters. Assertions cover route/subject/location, evidence presence, answer string type, numerical sign, no unknown citations, no `[object Object]`, no raw validation structure, and response length.

## 18. Data Semantics and Calculation Rules

- Canonical timestamps are timezone-aware UTC; UI converts to market time.
- Hub location keys use stable codes such as `HB_NORTH`; labels use “North Hub.”
- Reader-facing “ERCOT” may correspond to EIA balancing-authority code `ERCO`; retain the raw code in source metadata.
- RT–DA spread = real-time minus day-ahead. DART = day-ahead minus real-time. Never use the terms interchangeably; include explicit operands in calculation evidence.
- Window “latest” is the newest observation within the selected window. Window average/min/max and fuel-mix summaries are recomputed for 24h, 48h, and 168h.
- Fuel-mix averages should be time-weighted when sampling intervals vary. Show coverage if observed duration is materially shorter than requested.
- Positive storage net output means discharging and negative means charging; state this near the metric.
- Forecast values remain labeled forecasts throughout retrieval, prose, evidence, and UI.
- No interpolated, imputed, or forecast value may be presented as an observation.
- Every metric has a registry entry: canonical name, label, unit, allowed locations, source priority, expected cadence, freshness SLA, and chart eligibility.

## 19. Testing and Quality Strategy

### 19.1 Test pyramid

- **Unit:** adapters with fixtures, normalization, chunking, routing, calculations, citation parsing, quality checks, formatting, risk clustering.
- **Contract:** Pydantic API schemas, fixture JSON compatibility, database repository methods, retrieval result shape.
- **Integration:** temporary database from ingest through persistence/retrieval/generation; migrations from empty schema; idempotent repeated ingestion.
- **Web:** FastAPI TestClient for security, readiness, public restrictions, bounds, roles, long history, and response types.
- **Browser:** headless smoke at target widths; role/window switches, modal charts, evidence drawer, Ask thread, archive, Data Lab export.
- **Evaluation:** gold retrieval set, labeled classification set, claim/citation metrics, persona-distinctness metric, human usefulness review.

### 19.2 Required CI commands

```powershell
ruff check src tests
python -m pytest -p no:cacheprovider
node --check src/gridbrief/web_static/site.js
```

CI also scans for committed secrets, verifies migration order, builds the Docker image, and checks that the HTML references the current cache-busted static asset version.

### 19.3 Release blockers

- failed tests/lint/migration;
- citation coverage or hallucination gate missed;
- wrong DART sign or mixed RT/DA semantics;
- `[object Object]`, traceback, credential, raw validation JSON, or inaccessible internal-source link displayed;
- 7-day selector reusing 24/48-hour summaries;
- ticker animation broken or incompatible with reduced motion;
- duplicate active alerts not intentionally clustered;
- stale source presented as current without warning;
- public generation possible without admin authorization;
- web and external scheduler both enabled;
- one persona only reorders identical content.

## 20. Final Acceptance and Handoff

### 20.1 Functional smoke matrix

1. Open the site at desktop and mobile widths; no horizontal overflow.
2. Confirm freshness/source health and `data_as_of` are understandable.
3. Select General, Market Analyst, and Grid Operations; verify materially distinct content.
4. Switch 24h → 48h → 7d; confirm API window, chart paths, averages, extrema, labels, and coverage change.
5. Expand every fuel chart and inspect exact points.
6. Verify the ticker moves right-to-left and loops without cutoffs.
7. Inspect active risks: no accidental duplicates; each retains affected area and full timeline.
8. Open newsletter evidence; prose remains readable and official publisher names are clear.
9. Run Data Lab metric/location/range queries and compare CSV with table values.
10. Ask the AI at least: current RT North Hub, DA there follow-up, DART North Hub, current load, reserve change, unplanned outages, battery behavior, weather reliability risk, and a question with missing evidence.
11. Continue after a long AI answer; the next response is normal, not validation JSON.
12. Trigger rate limiting and server errors in staging; UI shows readable safe text.
13. Run the scheduled refresh twice; duplicates do not grow and watermarks advance.
14. Confirm Render readiness, GitHub scheduler ownership, and latest edition after deployment.

### 20.2 Documentation package

Handoff includes:

- this PRD and architecture diagram;
- environment-variable/secret matrix with owners and rotation procedure;
- API contract examples and canonical metric registry;
- local setup, source onboarding, migration, deployment, rollback, backup, and incident runbooks;
- source fixture provenance and licenses/terms review;
- evaluation datasets/results and known limitations;
- six-person ownership map and pending-work list;
- screenshots of target responsive states and a five-minute demo script.

### 20.3 Definition of done

The project is complete when a new developer can clone the repository, follow §16 without undocumented intervention, load fixture or live data, generate three grounded persona editions, run the full website, pass CI, and deploy the same artifact publicly. “Looks correct on one laptop” is not completion; repeatable setup, explicit contracts, automated tests, source freshness, safe failure behavior, and operational ownership are part of the product.

---

*End of PRD v2.1*
