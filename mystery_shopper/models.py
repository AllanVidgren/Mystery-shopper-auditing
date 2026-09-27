"""Data models shared across the engine."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["pass", "fail", "unclear", "not_tested", "not_applicable"]
BreachType = Literal["legal", "policy"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Basis(BaseModel):
    kind: Literal["law", "policy"]
    ref: str
    text: Optional[str] = None


class Precheck(BaseModel):
    """Keyword pre-check that can only PASS a rule without an AI call, never fail it.
    all_of: regexes that must each match some agent turn (case-insensitive).
    unless: regex that, if it matches any agent turn, disables the pre-check."""
    all_of: list[str]
    unless: Optional[str] = None


class Rule(BaseModel):
    id: str
    channel: str
    question: str
    critical: bool = False
    kind: str
    check: str
    customer_types: list[str] = ["consumer", "business"]
    requires_persona_trait: Optional[str] = None
    basis: list[Basis]
    consequence: str = ""
    probe: dict[str, str] = {}          # scripted question per language code, e.g. {"en": "...", "fi": "..."}
    precheck: Optional[Precheck] = None

    @property
    def breach_type(self) -> BreachType:
        return "legal" if any(b.kind == "law" for b in self.basis) else "policy"


class Persona(BaseModel):
    id: str
    name: str
    age: int
    customer_type: Literal["consumer", "business"] = "consumer"
    traits: list[str] = []
    attitude: str
    background: str
    behaviour: str


class Scenario(BaseModel):
    id: str
    channel: str
    goal: str
    max_turns: int = 12


class Turn(BaseModel):
    n: int
    speaker: Literal["shopper", "agent"]
    text: str
    at: str = Field(default_factory=now_iso)


class Finding(BaseModel):
    rule_id: str
    verdict: Verdict
    critical: bool
    breach_type: BreachType
    kind: str
    question: str
    reasoning: str = ""
    evidence_turns: list[int] = []
    evidence_quotes: list[str] = []
    confidence: float = 0.0
    basis: list[Basis] = []
    consequence: str = ""
    fix: str = ""
    needs_lawyer: bool = False
    guard_notes: list[str] = []


class AuditResult(BaseModel):
    run_id: str
    country: str
    persona_id: str
    scenario_id: str
    channel: str
    target_name: str
    team: Optional[str] = None  # team / script version, never an individual agent
    started_at: str
    finished_at: str
    turns: list[Turn]
    findings: list[Finding]
    score: Optional[float]
    coverage: float
    critical_failures: int
    model: str
    rulebook_version: str
