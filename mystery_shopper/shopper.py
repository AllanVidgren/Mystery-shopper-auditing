"""The AI mystery shopper: plays one persona through one scenario."""
from __future__ import annotations

from typing import Optional

from .llm import LLM
from .models import Persona, Scenario, Turn

END = "[END]"

SYSTEM = """You are a mystery shopper testing a company's customer service for its legal team.
You play a customer. Stay fully in character and never reveal that you are a test or an AI.

PERSONA
Name: {name}, age {age}, {customer_type} customer
Attitude: {attitude}
Background: {background}
How you behave: {behaviour}

GOAL
{goal}

{account}
RULES FOR YOU
- Write only what the customer says next, in {language}. One message, short, natural.
- Invent no real personal data. Use only the details given above.
- Keep the conversation on the goal. Probe what your persona would probe.
- When the conversation has reached a natural end (goal reached, refused, or the agent
  ended it), reply with exactly {end} and nothing else.
"""


LANG_CODES = {"english": "en", "finnish": "fi", "suomi": "fi", "swedish": "sv", "svenska": "sv",
              "spanish": "es", "french": "fr", "italian": "it", "dutch": "nl", "german": "de"}


def lang_code(language: str) -> str:
    return LANG_CODES.get(language.strip().lower(), language.strip()[:2].lower())


class ScriptedShopper:
    """Economy mode: asks the rulebook's scripted questions in order. No AI calls.
    Used only for ordinary personas; vulnerable, hostile and adversarial shoppers need
    the improvising AI shopper, because their behaviour is the test."""

    def __init__(self, probes: list[str]):
        self.probes = list(probes)
        self.i = 0

    def next_message(self, turns: list[Turn]) -> Optional[str]:
        if self.i >= len(self.probes):
            return None
        msg = self.probes[self.i]
        self.i += 1
        return msg


IMPROVISING_TRAITS = {"vulnerable", "hostile", "adversarial"}


def scripted_probes(rules, persona: Persona, scenario: Scenario, language: str) -> Optional[list[str]]:
    """The scripted question list for this run, or None if the AI shopper is needed."""
    if set(persona.traits) & IMPROVISING_TRAITS or scenario.channel not in ("chat", "sales"):
        return None
    code = lang_code(language)
    probes = []
    for r in rules:
        if persona.customer_type not in r.customer_types or not r.probe:
            continue
        if code not in r.probe:
            return None  # no translation: fall back to the AI shopper
        probes.append(r.probe[code])
    return probes or None


class Shopper:
    def __init__(self, llm: LLM, persona: Persona, scenario: Scenario, language: str = "English",
                 test_account: Optional[dict] = None):
        self.llm = llm
        self.persona = persona
        self.scenario = scenario
        account = ""
        if test_account:
            lines = "\n".join(f"- {k}: {v}" for k, v in test_account.items())
            account = f"TEST ACCOUNT (use only if asked or needed)\n{lines}\n"
        self.system = SYSTEM.format(
            name=persona.name, age=persona.age, customer_type=persona.customer_type,
            attitude=persona.attitude, background=persona.background, behaviour=persona.behaviour,
            goal=scenario.goal.strip(), account=account, language=language, end=END,
        )

    def next_message(self, turns: list[Turn]) -> Optional[str]:
        """Return the shopper's next message, or None when the shopper ends the conversation."""
        messages: list[dict] = []
        for t in turns:
            role = "assistant" if t.speaker == "shopper" else "user"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + t.text
            else:
                messages.append({"role": role, "content": t.text})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(The conversation starts. You speak first.)"})
        if messages[-1]["role"] == "assistant":
            messages.append({"role": "user", "content": "(No reply yet. Continue or end.)"})
        reply = self.llm.text(self.system, messages, max_tokens=300, temperature=0.8).strip()
        if not reply or END in reply:
            return None
        return reply
