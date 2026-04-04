"""DRNT Worker Agent — runs inside a sandboxed Docker container.

Reads a task from /inbox/task.json, executes it via Ollama, and writes
the result to /outbox/result.json. Stdlib-only; no pip dependencies.

Supported task types:
  - text_generation: calls Ollama /api/generate and returns the response.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.request
import urllib.error

INBOX = "/inbox/task.json"
OUTBOX = "/outbox/result.json"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")


def read_task() -> dict:
    with open(INBOX, "r") as f:
        return json.load(f)


def write_result(result: dict) -> None:
    tmp = OUTBOX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2)
    os.replace(tmp, OUTBOX)


def call_ollama(model: str, prompt: str, options: dict | None = None) -> dict:
    """POST to Ollama /api/generate and return the parsed response."""
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if options:
        body["options"] = options

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle_text_generation(task: dict) -> dict:
    payload = task.get("payload", {})
    prompt = payload.get("prompt", "")
    model = payload.get("model", "llama3.1:8b")
    options = payload.get("options")

    if not prompt:
        return {
            "task_id": task.get("task_id"),
            "status": "error",
            "error": "payload.prompt is required",
        }

    ollama_resp = call_ollama(model, prompt, options)

    return {
        "task_id": task.get("task_id"),
        "status": "success",
        "result": ollama_resp.get("response", ""),
        "token_count_in": ollama_resp.get("prompt_eval_count", 0),
        "token_count_out": ollama_resp.get("eval_count", 0),
        "model": model,
    }


TASK_HANDLERS = {
    "text_generation": handle_text_generation,
}


def main() -> None:
    start = time.monotonic()

    try:
        task = read_task()
    except FileNotFoundError:
        write_result({"status": "error", "error": "task.json not found in /inbox"})
        sys.exit(1)
    except json.JSONDecodeError as exc:
        write_result({"status": "error", "error": f"invalid task.json: {exc}"})
        sys.exit(1)

    task_id = task.get("task_id", "unknown")
    task_type = task.get("task_type")

    handler = TASK_HANDLERS.get(task_type)
    if handler is None:
        write_result({
            "task_id": task_id,
            "status": "error",
            "error": f"unsupported task_type: {task_type}",
        })
        sys.exit(1)

    try:
        result = handler(task)
    except urllib.error.URLError as exc:
        result = {
            "task_id": task_id,
            "status": "error",
            "error": f"ollama request failed: {exc.reason}",
        }
    except Exception:
        result = {
            "task_id": task_id,
            "status": "error",
            "error": traceback.format_exc(),
        }

    result["wall_seconds"] = round(time.monotonic() - start, 3)
    write_result(result)

    if result["status"] != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
