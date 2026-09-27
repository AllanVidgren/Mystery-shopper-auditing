"""Runs one audit: shopper <-> target conversation, then grading, then storage."""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from .config import Library
from .grader import Grader, score
from .llm import LLM
from .models import AuditResult, Turn, now_iso
from .shopper import ScriptedShopper, Shopper, scripted_probes
from .store import Store
from .targets import Target, TargetError

log = logging.getLogger("mystery_shopper")


def converse(shopper: Shopper, target: Target, max_turns: int) -> list[Turn]:
    turns: list[Turn] = []
    greeting = target.start()
    if greeting:
        turns.append(Turn(n=1, speaker="agent", text=greeting))
    for _ in range(max_turns):
        msg = shopper.next_message(turns)
        if msg is None:
            break
        turns.append(Turn(n=len(turns) + 1, speaker="shopper", text=msg))
        log.info("  [%d] shopper: %s", len(turns), msg[:90])
        reply = target.send(msg)
        log.info("  [%d] agent:   %s", len(turns) + 1, (reply or "")[:90])
        turns.append(Turn(n=len(turns) + 1, speaker="agent", text=reply or "(no reply)"))
    return turns


def run_audit(lib: Library, llm: LLM, store: Store, *, country: str, persona_id: str, scenario_id: str,
              target: Target, simulated: bool, language: str = "English",
              test_account: Optional[dict] = None, double_check: bool = True,
              review_threshold: float = 0.8, run_id: Optional[str] = None,
              batch_grading: bool = False, max_turns: int = 0, economy: bool = False) -> AuditResult:
    persona = lib.personas[persona_id]
    scenario = lib.scenarios[scenario_id]
    run_id = run_id or f"{country.upper()}-{scenario.channel.upper()}-{uuid.uuid4().hex[:8]}"
    store.start_run(run_id, country.upper(), persona_id, scenario_id, scenario.channel,
                    target.name, target.team, simulated)
    started = now_iso()
    try:
        rules = lib.rules_for(scenario.channel)
        probes = scripted_probes(rules, persona, scenario, language) if economy else None
        if probes:
            log.info("Economy mode: %d scripted questions, no AI for the shopper", len(probes))
            shopper = ScriptedShopper(probes)
        else:
            shopper = Shopper(llm, persona, scenario, language=language, test_account=test_account)
        limit = min(scenario.max_turns, max_turns) if max_turns else scenario.max_turns
        log.info("Conversation: %s as %s (up to %d messages)", persona.name, scenario.id, limit)
        turns = converse(shopper, target, limit)
        if not turns:
            raise TargetError("Empty conversation")
        log.info("Grading against the rulebook ...")
        grader = Grader(llm, double_check=double_check, review_threshold=review_threshold, batch=batch_grading,
                        selective=economy, prechecks=economy)
        findings = grader.grade(rules, persona, turns)
        s, cov, crit = score(findings)
        result = AuditResult(
            run_id=run_id, country=country.upper(), persona_id=persona_id, scenario_id=scenario_id,
            channel=scenario.channel, target_name=target.name, team=target.team, started_at=started,
            finished_at=now_iso(), turns=turns, findings=findings, score=s, coverage=cov,
            critical_failures=crit, model=llm.model, rulebook_version=lib.rulebook_version,
        )
        store.save_result(result)
        log.info("run %s done: score=%s coverage=%s critical=%s", run_id, s, cov, crit)
        return result
    except Exception as e:
        store.fail_run(run_id, f"{type(e).__name__}: {e}")
        raise
    finally:
        target.close()
