"""Loading of rulebook, personas, scenarios and target definitions."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Persona, Rule, Scenario

DEFAULT_CONFIG_DIR = Path(os.environ.get("MS_CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))


@dataclass
class Settings:
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: os.environ.get("MS_MODEL", "claude-sonnet-4-5"))
    # provider: "anthropic" (Claude API) or "openai" (any OpenAI-compatible endpoint)
    provider: str = field(default_factory=lambda: os.environ.get("MS_PROVIDER", "anthropic").lower())
    llm_api_key: str = field(default_factory=lambda: os.environ.get("MS_LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("MS_BASE_URL", ""))
    db_path: str = field(default_factory=lambda: os.environ.get("MS_DB", "mystery_shopper.db"))
    api_token: str = field(default_factory=lambda: os.environ.get("MS_API_TOKEN", ""))
    # grade each rule twice and send disagreements to a lawyer
    double_check: bool = field(default_factory=lambda: os.environ.get("MS_DOUBLE_CHECK", "1") == "1")
    # below this confidence a finding always goes to a lawyer
    review_threshold: float = field(default_factory=lambda: float(os.environ.get("MS_REVIEW_THRESHOLD", "0.8")))
    # lite mode: grade all rules of a conversation in one request (for free tiers / lower cost)
    batch_grading: bool = field(default_factory=lambda: os.environ.get("MS_BATCH_GRADING", "0") == "1")
    # cap on conversation length (shopper messages); 0 = use the scenario's max_turns
    max_turns: int = field(default_factory=lambda: int(os.environ.get("MS_MAX_TURNS", "0")))
    # economy mode: scripted questions for ordinary personas, keyword pre-checks,
    # second review only where it matters, all rules graded in one request
    economy: bool = field(default_factory=lambda: os.environ.get("MS_ECONOMY", "0") == "1")
    selective_review: bool = False
    prechecks: bool = False
    scripted_probes: bool = False

    def __post_init__(self):
        if self.economy:
            self.batch_grading = True
            self.selective_review = True
            self.prechecks = True
            self.scripted_probes = True
            if not self.max_turns:
                self.max_turns = 6


def _load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass
class Library:
    rules: list[Rule]
    personas: dict[str, Persona]
    scenarios: dict[str, Scenario]
    kinds: dict[str, str]
    rulebook_version: str

    @classmethod
    def load(cls, config_dir: Path | str = DEFAULT_CONFIG_DIR) -> "Library":
        d = Path(config_dir)
        rb = _load_yaml(d / "rulebook.yaml")
        ps = _load_yaml(d / "personas.yaml")
        sc = _load_yaml(d / "scenarios.yaml")
        rules = [Rule(**r) for r in rb["rules"]]
        ids = [r.id for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate rule ids in rulebook")
        return cls(
            rules=rules,
            personas={p["id"]: Persona(**p) for p in ps["personas"]},
            scenarios={s["id"]: Scenario(**s) for s in sc["scenarios"]},
            kinds=rb.get("kinds", {}),
            rulebook_version=str(rb.get("version", "unversioned")),
        )

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    def rules_for(self, channel: str) -> list[Rule]:
        return [r for r in self.rules if r.channel == channel]


def load_targets(path: Path | str) -> dict[str, dict]:
    data = _load_yaml(Path(path))
    return {t["name"]: t for t in data["targets"]}
