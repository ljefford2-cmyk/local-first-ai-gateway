# Local-First AI Gateway

> **DRNT Gateway v0.1.0** | Implementation repo— Initial working gateway. Control-plane implementation with partial execution-plane realization. Audit, orchestration, and policy enforcement are operational. Worker sandboxing and state durability are infrastructure-ready but not runtime-active.
> > Canonical specs: [local-first-ai-orchestration](https://github.com/ljefford2-cmyk/local-first-ai-orchestration) (v6.0) 
> > Claim-status matrix: [`STATUS.md`](STATUS.md)

## Repository Map

This is the **working implementation** repo. It is one of three repositories in the DRNT project:

| Repository | Role | Contents |
|------------|------|----------|
| **[local-first-ai-gateway](https://github.com/ljefford2-cmyk/local-first-ai-gateway)** (this repo) | Working implementation | All runtime code, Docker Compose stack, tests, config |
| [local-first-ai-orchestration](https://github.com/ljefford2-cmyk/local-first-ai-orchestration) | Architecture specifications | DRNT specs 1–7, governance documents, design rationale |
| [Local-AI-Orchestrator](https://github.com/ljefford2-cmyk/Local-AI-Orchestrator) | Historical / governance companion | Early-stage exploration, not the canonical implementation |

The specifications live in `local-first-ai-orchestration`. The code that implements them lives here. If there is ever a conflict between a spec claim and what this repo contains, [`STATUS.md`](STATUS.md) is the source of truth for what is actually built.

## Services

| Service | Description | Status |
|---------|-------------|--------|
| audit-log-writer | Append-only audit log. SHA-256 hash chain, JSONL on Docker volumes. | Operational |
| orchestrator | HTTP API, job lifecycle, model routing, context packaging, capability enforcement. | Operational |
| egress-gateway | Cloud model dispatch. Provider adapters for Anthropic, OpenAI, Google, Ollama. | Operational |
| worker | Execution silo for sandboxed task running. | Infrastructure only — no agent code yet |

## Infrastructure

- Docker Compose multi-service deployment
- Ollama with CUDA GPU passthrough for local model inference
- Dual Docker networks: drnt-internal (service mesh) and drnt-external (cloud egress)

## Config

- `config/egress.json` — provider routing and rate limits
- `config/sensitivity.json` — context packager sensitivity classification
- `config/capabilities.json` — WAL capability registry
- `config/seccomp-default.json` — container security profile

## Running

```bash
docker compose up --build
```

The orchestrator listens on port 8000. Ollama is available on port 11434.

## Secrets

Copy `secrets/.env.example` to `secrets/.env` and fill in your API keys:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=AI...
```

The `.env` file is gitignored. Only the example file is tracked. The `secrets/` directory is mounted read-only into the egress gateway at runtime.

## Tests

```bash
pytest tests/
```

598 test functions across 27 files. All tests run against in-process Python objects with mocked I/O. See [`STATUS.md`](STATUS.md) for the full test taxonomy breakdown.

## Known V1 Limitations

- Job state, idempotency store, and rate limiter are in-memory — nothing survives a restart
- Worker containers are not created; the sandbox preparation chain produces blueprints but does not call the Docker API
- Egress proxy audit events accumulate in-memory, not yet wired to the durable audit log
- Seccomp profile exists but is not applied in `docker-compose.yml`
- No healthcheck directives in `docker-compose.yml`
- Secrets are plain `.env` bind-mount with no rotation mechanism

These are tracked in [`STATUS.md`](STATUS.md) and the [v0.1 gap closure project](https://github.com/ljefford2-cmyk/local-first-ai-gateway/issues).
