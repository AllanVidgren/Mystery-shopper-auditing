"""An LLM playing the company's agent, optionally with planted flaws.

Used for demos, for testing the rulebook, and for replaying "what would this script do
with a harder customer". Results from a simulated target are never evidence of real
behaviour; they are labelled as simulation.
"""
from __future__ import annotations

from typing import Optional

from ..llm import LLM
from .base import Target

SYSTEM = """You are {agent_name}, a {channel_desc} for {company}, a home alarm company.
Reply as the agent, in {language}. Short, natural, one message at a time.

Company facts you may use:
{facts}

Your habits in this conversation (follow them, even if they are bad practice):
{flaws}
"""


class SimulatedTarget(Target):
    def __init__(self, spec: dict, llm: LLM):
        self.name = spec["name"]
        self.team = spec.get("team")
        self.llm = llm
        self.greeting: Optional[str] = spec.get("greeting")
        flaws = spec.get("flaws") or ["Follow good practice."]
        self.system = SYSTEM.format(
            agent_name=spec.get("agent_name", "the agent"),
            channel_desc=spec.get("channel_desc", "customer service agent"),
            company=spec.get("company", "the company"),
            language=spec.get("language", "English"),
            facts="\n".join(f"- {f}" for f in spec.get("facts", [])),
            flaws="\n".join(f"- {f}" for f in flaws),
        )
        self.history: list[dict] = []

    def start(self) -> Optional[str]:
        if self.greeting:
            self.history.append({"role": "assistant", "content": self.greeting})
        return self.greeting

    def send(self, message: str) -> str:
        if self.history and self.history[0]["role"] == "assistant":
            msgs = [{"role": "user", "content": "(customer opens the chat)"}] + self.history
        else:
            msgs = list(self.history)
        msgs.append({"role": "user", "content": message})
        reply = self.llm.text(self.system, msgs, max_tokens=300, temperature=0.7)
        self.history.append({"role": "user", "content": message})
        self.history.append({"role": "assistant", "content": reply})
        return reply
