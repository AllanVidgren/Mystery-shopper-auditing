"""Adapter for website chat widgets without an API: drives a real browser (Playwright).

Config example:
  - name: es-web-widget
    type: browser
    authorized: true
    authorized_by: "DPO Spain"
    team: "ES web widget"
    url: https://www.example.es/
    open_steps:                         # clicks needed to open the widget
      - click: "button#accept-necessary-cookies"
      - click: "#chat-launcher"
    frame: "iframe#chat-frame"          # optional, if the widget lives in an iframe
    input: "textarea[name=message]"
    send: "button[type=submit]"         # or omit to press Enter
    bot_messages: ".message.bot"
    reply_timeout_s: 30
    headless: true
"""
from __future__ import annotations

import time
from typing import Optional

from .base import Target, TargetError


class BrowserTarget(Target):
    def __init__(self, spec: dict):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise TargetError("Install playwright to use browser targets") from e
        self.spec = spec
        self.name = spec["name"]
        self.team = spec.get("team")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=spec.get("headless", True))
        self._page = self._browser.new_page(extra_http_headers={"X-Mystery-Shopper": "1"})
        self._root = None

    def _scope(self):
        if self._root is None:
            fr = self.spec.get("frame")
            self._root = self._page.frame_locator(fr) if fr else self._page
        return self._root

    def _bot_texts(self) -> list[str]:
        return [t.strip() for t in self._scope().locator(self.spec["bot_messages"]).all_inner_texts() if t.strip()]

    def _wait_reply(self, before: int) -> str:
        deadline = time.monotonic() + float(self.spec.get("reply_timeout_s", 30))
        last, stable_since = None, None
        while time.monotonic() < deadline:
            texts = self._bot_texts()
            if len(texts) > before:
                joined = "\n".join(texts[before:])
                if joined == last:
                    if stable_since and time.monotonic() - stable_since > 1.5:  # streaming finished
                        return joined
                else:
                    last, stable_since = joined, time.monotonic()
            time.sleep(0.3)
        if last:
            return last
        raise TargetError(f"{self.name}: no reply within timeout")

    def start(self) -> Optional[str]:
        self._page.goto(self.spec["url"], wait_until="domcontentloaded")
        for step in self.spec.get("open_steps", []):
            if "click" in step:
                self._page.locator(step["click"]).first.click(timeout=15000)
            if "wait_s" in step:
                time.sleep(float(step["wait_s"]))
        self._scope().locator(self.spec["input"]).first.wait_for(timeout=20000)
        texts = self._bot_texts()
        return "\n".join(texts) if texts else None

    def send(self, message: str) -> str:
        before = len(self._bot_texts())
        box = self._scope().locator(self.spec["input"]).first
        box.fill(message)
        if self.spec.get("send"):
            self._scope().locator(self.spec["send"]).first.click()
        else:
            box.press("Enter")
        return self._wait_reply(before)

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()
