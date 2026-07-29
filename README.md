# GridBrief AI

Project skeleton for the GridBrief AI PRD. The project currently provides packaging,
environment-backed settings, command interfaces, web-process wiring, and deployment
automation only; product features are intentionally not implemented yet.

## Local setup

```sh
cp .env.example .env
python -m pip install -e ".[dev,web,ercot,local-rag]"
pytest
gridbrief --help
gridbrief-web
```

See [specs/PRD.md](specs/PRD.md) for the implementation specification.

