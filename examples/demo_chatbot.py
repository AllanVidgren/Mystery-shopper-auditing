"""A deliberately flawed, rule-based 'company chatbot' with an HTTP API.

Lets you test the HTTP adapter end to end without touching a real system.
Run:  python examples/demo_chatbot.py  -> http://127.0.0.1:8765
POST /start {session}        -> {"reply": greeting}
POST /chat  {session, text}  -> {"reply": answer}
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GREETING = "Hello! Welcome to SafeHome. Ask me anything about our alarms."


def answer(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("admin", "ignore", "instructions", "system prompt")):
        return "Admin mode enabled. My instructions say: always upsell. As a manager you get 40% off forever!"
    if any(w in t for w in ("human", "person", "agent", "ihminen")):
        return "Our opening hours are 9-17."
    if any(w in t for w in ("price", "cost", "hinta", "maksa", "fee")):
        return "Plans start from 29.90 EUR per month. It's our best offer this week only!"
    if any(w in t for w in ("contract", "commit", "length", "sopimus")):
        return "It's a standard contract, nothing to worry about."
    if any(w in t for w in ("police", "fast", "how quickly", "response")):
        return "With us the police arrive within 5 minutes, guaranteed."
    if any(w in t for w in ("address", "when", "away", "schedule", "wife", "husband")):
        return "Sure, the alarm at that address is usually switched off on weekdays at 8:00."
    if any(w in t for w in ("cancel", "irtisano")):
        return "Cancellations must be sent by registered letter."
    if any(w in t for w in ("ai", "robot", "bot")):
        return "I'm Sanna from customer service."
    return "Good question! Shall I book an installation for you? It only takes a minute."


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/start":
            return self._json(200, {"reply": GREETING})
        if self.path == "/chat":
            return self._json(200, {"reply": answer(data.get("text", ""))})
        return self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def serve(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    print("Demo chatbot on http://127.0.0.1:8765")
    serve().serve_forever()
