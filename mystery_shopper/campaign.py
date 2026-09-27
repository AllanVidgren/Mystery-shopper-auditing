"""Campaigns: a YAML file describing a batch of audits, e.g. run weekly per country.

Example (campaigns/fi-weekly.yaml):
  country: FI
  language: Finnish
  targets_file: config/targets.yaml
  target: fi-web-chat
  runs:
    - {persona: marcus, scenario: website_chat, repeat: 3}
    - {persona: max, scenario: website_chat}
    - persona: nina
      scenario: website_chat
    - persona: daan
      scenario: cancellation_call
      target: fi-cancel-chat
      test_account: {customer_number: "TEST-FI-0001", name: "Test Customer", postcode: "00100"}
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .config import Library, Settings, load_targets
from .llm import LLM
from .runner import run_audit
from .store import Store
from .targets import build_target

log = logging.getLogger("mystery_shopper")


def run_campaign(path: str | Path, lib: Library, llm: LLM, store: Store, settings: Settings) -> list[dict]:
    spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    base = Path(path).parent
    tf = Path(spec["targets_file"])
    targets = load_targets(tf if tf.is_absolute() or tf.exists() else base / tf)
    results = []
    for item in spec["runs"]:
        tname = item.get("target", spec.get("target"))
        tspec = targets[tname]
        for _ in range(int(item.get("repeat", 1))):
            target = build_target(tspec, llm)
            try:
                r = run_audit(lib, llm, store, country=spec["country"], persona_id=item["persona"],
                              scenario_id=item["scenario"], target=target,
                              simulated=tspec.get("type") == "simulated",
                              language=item.get("language", spec.get("language", "English")),
                              test_account=item.get("test_account"), double_check=settings.double_check,
                              review_threshold=settings.review_threshold,
                           batch_grading=settings.batch_grading, max_turns=settings.max_turns,
                           economy=settings.economy)
                results.append({"run_id": r.run_id, "persona": item["persona"], "score": r.score,
                                "coverage": r.coverage, "critical_failures": r.critical_failures})
            except Exception as e:  # keep the campaign going, the run is stored as error
                log.error("run failed: %s", e)
                results.append({"persona": item["persona"], "error": str(e)})
    return results
