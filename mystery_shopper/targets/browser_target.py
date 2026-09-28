"""Adapter for website chats without an API: drives a real browser (Playwright).

Works for chatbots and for live chats answered by real customer-service staff.

Config example (chatbot):
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
    bot_messages: ".message.agent"      # messages written by the company side
    reply_timeout_s: 30
    headless: true

Human mode (live chat with real staff) adds:
    human_mode: true
    reply_timeout_s: 300                # people can take minutes, queues longer
    first_reply_timeout_s: 900          # waiting in the queue for the first person
    settle_s: 8                         # a person often sends 2-3 messages in a row
    typing_indicator: ".typing"         # optional: wait while the employee is typing
    ignore_messages: "(in queue|number \\d+ in line|joined the chat)"  # system lines
    typing_speed_cps: 6                 # shopper types like a person (characters/second)
    think_s: [3, 8]                     # pause before typing, random in this range
    disclosure: "Hi, I am an AI test customer from the compliance team."  # optional,
                                        # sent first (see AI Act Art. 50(1))
"""
from __future__ import annotations

import random
import re
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
        self.human = bool(spec.get("human_mode"))
        self.disclosure: Optional[str] = spec.get("disclosure")
        self.reply_timeout = float(spec.get("reply_timeout_s", 300 if self.human else 30))
        self.first_timeout = float(spec.get("first_reply_timeout_s", self.reply_timeout))
        self.settle = float(spec.get("settle_s", 8 if self.human else 1.5))
        self.ignore = re.compile(spec["ignore_messages"], re.I) if spec.get("ignore_messages") else None
        self.cps = float(spec.get("typing_speed_cps", 6)) if self.human else 0.0
        think = spec.get("think_s", [3, 8] if self.human else [0, 0])
        self.think = (float(think[0]), float(think[1]))
        self._answered = False
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=spec.get("headless", True))
        self._page = self._browser.new_page(extra_http_headers={"X-Mystery-Shopper": "1"})
        self._root = None

    def _scope(self):
        if self._root is None:
            fr = self.spec.get("frame")
            self._root = self._page.frame_locator(fr) if fr else self._page
        return self._root

    def _agent_texts(self) -> list[str]:
        """All messages from the company side so far, without system lines (queue notices etc.)."""
        texts = [t.strip() for t in self._scope().locator(self.spec["bot_messages"]).all_inner_texts()]
        return [t for t in texts if t and not (self.ignore and self.ignore.search(t))]

    def _typing(self) -> bool:
        sel = self.spec.get("typing_indicator")
        if not sel:
            return False
        try:
            return self._scope().locator(sel).first.is_visible()
        except Exception:
            return False

    def _wait_reply(self, before: int, timeout: float) -> str:
        """Wait until the other side has answered and gone quiet for `settle` seconds.
        Collects several consecutive messages into one reply, as people often split them."""
        deadline = time.monotonic() + timeout
        last, quiet_since = None, None
        while time.monotonic() < deadline:
            texts = self._agent_texts()
            if len(texts) > before:
                joined = "\n".join(texts[before:])
                if joined != last or self._typing():
                    last, quiet_since = joined, time.monotonic()
                elif time.monotonic() - quiet_since >= self.settle:
                    return joined
            time.sleep(0.3)
        if last:
            return last
        raise TargetError(f"{self.name}: no reply within {timeout:.0f}s")

    def _type(self, box, message: str) -> None:
        if self.think[1] > 0:
            time.sleep(random.uniform(*self.think))
        if self.cps > 0:
            box.fill("")
            box.press_sequentially(message, delay=1000.0 / self.cps)
        else:
            box.fill(message)

    def start(self) -> Optional[str]:
        self._page.goto(self.spec["url"], wait_until="domcontentloaded")
        for step in self.spec.get("open_steps", []):
            if "click" in step:
                self._page.locator(step["click"]).first.click(timeout=15000)
            if "wait_s" in step:
                time.sleep(float(step["wait_s"]))
        self._scope().locator(self.spec["input"]).first.wait_for(timeout=20000)
        texts = self._agent_texts()
        return "\n".join(texts) if texts else None

    def send(self, message: str) -> str:
        before = len(self._agent_texts())
        box = self._scope().locator(self.spec["input"]).first
        self._type(box, message)
        if self.spec.get("send"):
            self._scope().locator(self.spec["send"]).first.click()
        else:
            box.press("Enter")
        timeout = self.reply_timeout if self._answered else self.first_timeout
        reply = self._wait_reply(before, timeout)
        self._answered = True
        return reply

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()
