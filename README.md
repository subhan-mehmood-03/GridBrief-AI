# GridBrief AI

GridBrief AI is a source-grounded ERCOT intelligence product. It ingests structured market and
grid data plus official documents, generates verified persona-specific editions, answers grounded
questions, and serves a responsive production website with evidence, weather, charts, and a Data Lab.

## Local setup

```powershell
python -m pip install -e ".[dev,web,ercot,local-rag]"
gridbrief migrate
gridbrief ingest all --hours 168
gridbrief index
gridbrief generate --role general
gridbrief generate --role market_analyst
gridbrief generate --role grid_operations
gridbrief-web
```

Open `http://localhost:8000`. API documentation is at `http://localhost:8000/api/docs`.

## Verification

```powershell
ruff check src tests
python -m pytest -p no:cacheprovider
node --check src/gridbrief/web_static/site.js
gridbrief evaluate
```

Local repository and migration tests use the isolated `gridbrief_test` PostgreSQL database. Never
point the test URL at the shared Supabase database.

## Operations

- `gridbrief scheduler --once` runs one local refresh cycle.
- `gridbrief scheduler` runs continuously only when `GRIDBRIEF_AUTOMATIC_REFRESH=true`.
- Public production should set automatic refresh to false and use the checked-in GitHub Actions.
- Ask AI uses deterministic calculations for stored metrics. Set
  `GRIDBRIEF_ALLOW_REMOTE_LLM=true` with a valid `GRIDBRIEF_GROQ_API_KEY` and
  `GRIDBRIEF_GROQ_MODEL` to enable grounded model explanations for retrieved evidence.
- `gridbrief archive --limit 20` lists recent saved editions.

Configuration and deployment requirements are documented in [specs/PRD.md](specs/PRD.md).
