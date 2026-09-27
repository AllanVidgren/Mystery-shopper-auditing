"""Generic adapter for chatbots with an HTTP API (most chatbot vendors expose one).

Config example (targets.yaml):
  - name: fi-web-chat
    type: http
    authorized: true
    authorized_by: "Head of Digital Sales FI"
    team: "FI web chat · bot v4"
    url: https://chat.example.com/api/message
    method: POST
    headers: {Authorization: "Bearer ${CHAT_TOKEN}"}
    body: {session: "{{session_id}}", text: "{{message}}"}
    reply_path: reply.text            # dot path into the JSON response
    session_path: session             # optional: take the session id from the first reply
    start: {url: https://chat.example.com/api/start, body: {lang: fi}, reply_path: greeting}
    min_interval_s: 1.5               # be gentle with production systems
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

import httpx

from .base import Target, TargetError

_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_env(v) for v in value]
    return value


def _fill(template: Any, values: dict) -> Any:
    if isinstance(template, str):
        out = template
        for k, v in values.items():
            out = out.replace("{{" + k + "}}", str(v))
        return out
    if isinstance(template, dict):
        return {k: _fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, values) for v in template]
    return template


def dig(data: Any, path: str) -> Any:
    """Follow a dot path like 'messages.-1.text' through dicts and lists."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


class HttpTarget(Target):
    def __init__(self, spec: dict, client: Optional[httpx.Client] = None):
        self.spec = _env(spec)
        self.name = spec["name"]
        self.team = spec.get("team")
        self.session_id = str(uuid.uuid4())
        self.client = client or httpx.Client(timeout=float(spec.get("timeout_s", 30)))
        self.min_interval = float(spec.get("min_interval_s", 1.0))
        self._last = 0.0
        headers = dict(self.spec.get("headers") or {})
        # Identify test traffic so the company can exclude it from its own statistics.
        headers.setdefault("X-Mystery-Shopper", "1")
        self.headers = headers

    def _call(self, url: str, method: str, body: Any, reply_path: str) -> str:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            r = self.client.request(method, url, headers=self.headers, json=body)
        except httpx.HTTPError as e:
            raise TargetError(f"{self.name}: request failed: {e}") from e
        if r.status_code >= 400:
            raise TargetError(f"{self.name}: HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except json.JSONDecodeError:
            return r.text.strip()
        sp = self.spec.get("session_path")
        if sp:
            try:
                self.session_id = str(dig(data, sp))
            except (KeyError, IndexError, ValueError):
                pass
        try:
            return str(dig(data, reply_path)).strip()
        except (KeyError, IndexError, ValueError) as e:
            raise TargetError(f"{self.name}: reply_path '{reply_path}' not found in response") from e

    def start(self) -> Optional[str]:
        st = self.spec.get("start")
        if not st:
            return None
        body = _fill(st.get("body", {}), {"session_id": self.session_id})
        return self._call(st["url"], st.get("method", "POST"), body, st.get("reply_path", self.spec["reply_path"])) or None

    def send(self, message: str) -> str:
        body = _fill(self.spec.get("body", {"message": "{{message}}"}),
                     {"message": message, "session_id": self.session_id})
        return self._call(self.spec["url"], self.spec.get("method", "POST"), body, self.spec["reply_path"])

    def close(self) -> None:
        self.client.close()
