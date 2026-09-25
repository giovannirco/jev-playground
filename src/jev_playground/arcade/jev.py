"""Jev over OpenRouter with a kept-alive TLS connection per thread."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

HOST = "openrouter.ai"
PATH = "/api/alpha/decisions"
MODEL_ID = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
REFERER = os.environ.get("PUBLIC_URL", "http://127.0.0.1:8780")

_local = threading.local()


def _key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            env_path = parent / ".env"
            if env_path.is_file():
                for line in env_path.read_text().splitlines():
                    if line.startswith("OPENROUTER_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("\"'")
            break
    raise RuntimeError("OPENROUTER_API_KEY is missing. Put it in .env. Nothing calls Jev until you use a page.")


class JevError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _conn() -> http.client.HTTPSConnection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(HOST, timeout=45)
        _local.conn = conn
    return conn


def _drop() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


def _post(body: bytes, site: str) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": REFERER,
        "X-OpenRouter-Title": f"jev-arcade/{site}",
    }
    for attempt in range(2):
        conn = _conn()
        try:
            conn.request("POST", PATH, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except (http.client.HTTPException, OSError):
            _drop()
            if attempt:
                raise
    raise RuntimeError("unreachable")


def decide(site: str, state, questions: dict) -> dict:
    body = json.dumps({"model": MODEL_ID, "state": state, "questions": questions}).encode()
    started = time.perf_counter()
    for attempt in range(4):
        status, raw = _post(body, site)
        if status in (429, 502, 503, 529) and attempt < 3:
            time.sleep(0.25 * (2**attempt))
            continue
        break
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise JevError(502, f"Jev returned {status} with a non-JSON body.") from None
    if status >= 400 or "error" in payload:
        err = payload.get("error", payload)
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise JevError(status if status >= 400 else 502, message or f"Jev returned {status}.")
    usage = payload.get("usage") or {}
    return {
        "answers": payload["answers"],
        "model": payload.get("model", MODEL_ID),
        "elapsed_ms": elapsed,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost": usage.get("cost") or 0,
        },
    }
