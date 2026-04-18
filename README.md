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

## Why Governance Produces Better AI Outcomes

The performance ceiling for AI systems today is not model capability — it is orchestration. A capable model given an unbounded task and unlimited latitude produces unreliable output. The same model, constrained by graduated trust, structural gates, and fail-closed defaults, produces dramatically better results.

DRNT was designed around this principle, and the build process proved it empirically. This system was built using a two-agent workflow — one AI writing structured prompts, another executing on the filesystem — governed by the same patterns DRNT enforces at runtime:

- **Discovery before action.** Every execution step begins by reading the actual state, not assuming it. The model that skips this step confidently presents stale information as fact.
- **Gate-based progression.** No phase advances until the previous phase fully passes. Regressions caught early cost minutes; regressions caught late cost days.
- **Automatic demotion on failure.** When unsupervised execution scattered files across wrong directories or introduced silent regressions, trust was pulled back to explicit verification at every step. Quality improved immediately.
- **Audit trail as learning surface.** State snapshots, commit history, and test results create an empirical record. What was true at review time is what matters — not assertion after the fact.

When these constraints were loose, output quality was poor. When they tightened, the same models — with no capability upgrade — produced clean, verified, zero-regression output across seven specification implementations (see [`STATUS.md`](STATUS.md) for current test counts).

The implication extends beyond this project. The local model's value is not that it is smart. It is that it knows what it doesn't know, it enforces structural privacy before anything leaves the local perimeter (current implementation: regex-based PII redaction with sensitivity allowlists, single-field context packaging; multi-field context assembly and advanced NER are deferred (v0.3+)), and it never promotes its own trust level. Better outcomes come from better governance of capable models, not from making models more capable and hoping they self-govern.

## Specifications Implemented

| Spec | Name |
|------|------|
| 1 | Audit/Event Schema |
| 2 | Capability Model (WAL → Permissions) |
| 3 | Context Boundary Specification |
| 4 | Egress Policy Binding |
| 5 | Override Semantics |
| 6 | Silo Runtime Security |
| 7 | Signal Chain Resilience |
| 8 | Managed Build Workflow |

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

**⚠️ Not designed for internet exposure.** This system is designed for single-user, trusted-local-network operation. The API endpoints have no authentication. Do not expose ports to the public internet.

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

The implementation is covered by an automated test suite — see [`STATUS.md`](STATUS.md) for current counts and per-claim conformance. Unit tests run against in-process Python objects with mocked I/O. End-to-end tests against a running Docker Compose stack live in `tests/test_e2e_v02.py` and are skipped when the stack is not running.

## Development

**Dependency model:** This is a Docker-first monorepo. Each service manages its own `requirements.txt`. The root `pyproject.toml` contains shared pytest configuration only. For local development without Docker, install dependencies from the relevant service directory.

## Known V1 Limitations

- Seccomp profile is enforced on spawned worker containers via `worker_executor.py` but is not applied at the Docker Compose level to the four infrastructure services (orchestrator, audit-log-writer, egress-gateway, ollama)

These are tracked in [`STATUS.md`](STATUS.md).
