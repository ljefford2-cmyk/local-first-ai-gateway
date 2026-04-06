# Local-First AI Gateway

> **DRNT Gateway v0.2.1** | Implementation repo
> Canonical specs: [local-first-ai-orchestration](https://github.com/ljefford2-cmyk/local-first-ai-orchestration) (v7.0)
> Claim-status matrix: [`STATUS.md`](STATUS.md)

## Repository Map

This is the **working implementation** repo. It is one of three repositories in the DRNT project:

| Repository | Role | Contents |
|------------|------|----------|
| **[local-first-ai-gateway](https://github.com/ljefford2-cmyk/local-first-ai-gateway)** (this repo) | Working implementation | All runtime code, Docker Compose stack, tests, config |
| [local-first-ai-orchestration](https://github.com/ljefford2-cmyk/local-first-ai-orchestration) | Architecture specifications | DRNT specs 1–7, governance documents, design rationale |
| [Local-AI-Orchestrator](https://github.com/ljefford2-cmyk/Local-AI-Orchestrator) | Historical / governance companion | Early-stage exploration, not the canonical implementation |

The specifications live in `local-first-ai-orchestration`. The code that implements them lives here. If there is ever a conflict between a spec claim and what this repo contains, [`STATUS.md`](STATUS.md) is the source of truth for what is actually built.

## Specifications Implemented

| Spec | Name |
|------|------|
| 1 | Audit Log Writer |
| 2 | Orchestrator Core |
| 3 | Context Packager |
| 4 | Egress Policy |
| 5 | Override Semantics |
| 6 | Worker Silo / Runtime Enforcement |
| 7 | Signal Chain Resilience |

## Services

| Service | Description | Status |
|---------|-------------|--------|
| audit-log-writer | Append-only audit log. SHA-256 hash chain, JSONL on Docker volumes. | Operational |
| orchestrator | HTTP API, job lifecycle, model routing, context packaging, capability enforcement. | Operational |
| egress-gateway | Cloud model dispatch. Provider adapters for Anthropic, OpenAI, Google, Ollama. | Operational |
| worker-proxy | Docker sidecar — sole holder of the Docker socket. Exposes container lifecycle over HTTP. | Operational |
| worker | Execution silo for sandboxed task running. One-shot containers created via worker-proxy. | Operational |

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

735 tests across 34 files (717 passing, 18 skipped e2e integration). All unit tests run against in-process Python objects with mocked I/O. See [`STATUS.md`](STATUS.md) for the full test taxonomy breakdown.

## Known V1 Limitations

- Rate limiter is in-memory — counters reset on restart (v0.2 item)
- Seccomp profile is enforced on spawned worker containers via `worker_executor.py` but is not applied at the Docker Compose level to the four infrastructure services (orchestrator, audit-log-writer, egress-gateway, ollama)
- Secrets are plain `.env` bind-mount with no rotation mechanism

These are tracked in [`STATUS.md`](STATUS.md).
