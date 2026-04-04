# Local-First AI Gateway

Working implementation of the DRNT personal AI gateway.

Architecture specifications and governance documents are maintained in
[local-first-ai-orchestration](https://github.com/ljefford2-cmyk/local-first-ai-orchestration).

## Services

| Service | Description |
| --- | --- |
| audit-log-writer | Append-only audit log. SHA-256 hash chain, JSONL on Docker volumes. |
| orchestrator | HTTP API, job lifecycle, model routing, context packaging, capability enforcement. |
| egress-gateway | Cloud model dispatch. Provider adapters for Anthropic, OpenAI, Google, Ollama. |
| worker | Execution silo for sandboxed task running. |

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
