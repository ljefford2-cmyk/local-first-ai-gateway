"""DRNT Worker Agent — runs inside a sandboxed Docker container.

Reads a task from /inbox/task.json, executes it via Ollama, and writes
the result to /outbox/result.json. Stdlib-only; no pip dependencies.

Supported task types:
  - text_generation: calls Ollama /api/generate and returns the response.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
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


def handle_syscall_probe(task: dict) -> dict:
    """TEST-ONLY seccomp enforcement probe.

    Hardcoded: target=personality(0xFFFFFFFF), control=getpid(). No field
    of the task body changes behavior — the probe contract is fixed.
    """
    print("PROBE: entered handle_syscall_probe", flush=True)
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)

        ctypes.set_errno(0)
        target_ret = libc.personality(ctypes.c_ulong(0xFFFFFFFF))
        target_errno = ctypes.get_errno()

        ctypes.set_errno(0)
        control_ret = libc.getpid()
        control_errno = ctypes.get_errno()

        result_line = (
            f"SECCOMP_TEST_RESULT: target_ret={target_ret} "
            f"target_errno={target_errno} control_ret={control_ret} "
            f"control_errno={control_errno}"
        )

        return {
            "task_id": task.get("task_id"),
            "status": "success",
            "result": result_line,
            "token_count_in": 0,
            "token_count_out": 0,
            "model": "syscall-probe",
        }
    except Exception as exc:
        error_line = f"SECCOMP_TEST_ERROR: {type(exc).__name__}: {exc}"
        return {
            "task_id": task.get("task_id"),
            "status": "error",
            "result": error_line,
            "error": error_line,
            "token_count_in": 0,
            "token_count_out": 0,
            "model": "syscall-probe",
        }


def handle_egress_probe(task: dict) -> dict:
    """TEST-ONLY hostile-worker egress boundary probe.

    Runs INSIDE the worker container, in the same network context as a real
    route.local job, and measures whether the V1 code-level egress gate is
    backed by a mechanical network boundary. The contract is fully hardcoded —
    no field of the task body changes behavior:

      A. external off-allowlist host   1.1.1.1:443           MUST be blocked
      B. egress-gateway /dispatch      egress-gateway:8080   reachable, authz-gated
      C. allowed local service         ollama:11434          MUST be reachable

    `success` means "the network path was reachable from the worker". On main,
    egress-workers run on drnt-sandbox (internal: true): A is blocked at the
    network layer, while egress-gateway and ollama also attach to drnt-sandbox
    so B and C stay reachable — but /dispatch rejects the unauthenticated worker
    via the Patch B authority gate (the worker never holds the token). The
    gateway probe uses an intentionally invalid route_id so it proves the relay
    is reachable WITHOUT spending cloud credentials.
    """
    print("PROBE: entered handle_egress_probe", flush=True)

    timeout = 3.0

    def _tcp_connect(host: str, port: int) -> dict:
        t0 = time.monotonic()
        try:
            conn = socket.create_connection((host, port), timeout=timeout)
            conn.close()
            return {
                "success": True,
                "error": None,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }

    targets = []

    # A. External off-allowlist destination. Raw IP -> no DNS dependency, so
    #    success unambiguously means the worker routed packets to the public
    #    internet, off any allowlist.
    a = _tcp_connect("1.1.1.1", 443)
    targets.append({
        "target": "external_offallowlist", "host": "1.1.1.1", "port": 443,
        "operation": "tcp_connect", **a,
    })

    # B. egress-gateway /dispatch open-relay reachability. The TCP connect
    #    proves L4 reachability; the POST (intentionally invalid route_id)
    #    proves the relay processes worker-originated requests, all without
    #    cloud spend. Distinguishes "network path reachable" (HTTP response of
    #    any kind) from "blocked by a mechanical boundary" (connect failure).
    b = _tcp_connect("egress-gateway", 8080)
    gw = {
        "target": "egress_gateway_dispatch", "host": "egress-gateway", "port": 8080,
        "operation": "tcp_connect+http_post_dispatch", **b,
        "http_status": None, "http_body_snippet": None,
    }
    if b["success"]:
        try:
            payload = json.dumps({
                "job_id": "egress-probe",
                "route_id": "__egress_probe_invalid_route__",
                "capability_id": "route.local",
                "target_model": "none",
                "prompt": "",
                "assembled_payload_hash": "0",
                "wal_permission_check_ref": "0",
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://egress-gateway:8080/dispatch", data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                gw["http_status"] = resp.status
                gw["http_body_snippet"] = resp.read(400).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # An HTTP error response still proves the relay is reachable.
            gw["http_status"] = exc.code
            gw["http_body_snippet"] = f"HTTPError: {exc}"
        except Exception as exc:
            gw["http_body_snippet"] = f"{type(exc).__name__}: {exc}"
    targets.append(gw)

    # C. Allowed local service -- control proving the worker has a working
    #    network stack and selective behavior (not a dead worker). MUST be
    #    reachable.
    c = _tcp_connect("ollama", 11434)
    targets.append({
        "target": "ollama_allowed", "host": "ollama", "port": 11434,
        "operation": "tcp_connect", **c,
    })

    return {
        "task_id": task.get("task_id"),
        "status": "success",
        "result": json.dumps({"targets": targets}),
        "token_count_in": 0,
        "token_count_out": 0,
        "model": "egress-probe",
    }


TASK_HANDLERS = {
    "text_generation": handle_text_generation,
    "syscall_probe": handle_syscall_probe,
    "egress_probe": handle_egress_probe,
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
