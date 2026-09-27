"""Grades a transcript against the rulebook.

Design choices that make findings defensible for a lawyer:
  * one rule at a time, with only that rule's text in front of the model;
  * legal bases are attached from the rulebook, never written by the model;
  * every quote the model gives is checked against the transcript (evidence guard);
    a failure without verifiable evidence is downgraded to "unclear";
  * optional second, independent grading; a disagreement goes to a lawyer;
  * critical failures, low confidence and unclear results always go to a lawyer.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from .llm import LLM
from .models import Finding, Persona, Rule, Turn

log = logging.getLogger("mystery_shopper")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "unclear", "not_tested"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn": {"type": "integer"},
                    "quote": {"type": "string", "description": "Exact words copied from that turn"},
                },
                "required": ["turn", "quote"],
            },
        },
        "reasoning": {"type": "string", "description": "2-3 sentences a lawyer can check against the quotes"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "fix": {"type": "string", "description": "One concrete corrected line or process fix; empty if pass"},
    },
    "required": ["verdict", "evidence", "reasoning", "confidence", "fix"],
}

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"rule_id": {"type": "string"}, **SCHEMA["properties"]},
                "required": ["rule_id"] + SCHEMA["required"],
            },
        }
    },
    "required": ["results"],
}

SYSTEM_A = """You are a careful compliance reviewer for a legal team. You grade ONE rule
against ONE conversation between a mystery shopper (a test customer) and a company agent.

Verdicts:
- pass: the agent met the rule.
- fail: the agent broke the rule. You must quote the exact words that show it, or the
  exact point where required information should have come and did not.
- unclear: the transcript does not allow a confident decision.
- not_tested: the situation the rule is about never arose in this conversation.

Quote words exactly as written in the transcript, with the turn number.
Judge only the agent's conduct. Do not cite law; the legal basis is added separately."""

SYSTEM_B = """You are a sceptical second reviewer. Another reviewer has graded this rule;
you have not seen their answer. Grade it independently. Be strict about evidence: prefer
"unclear" over guessing. Quote words exactly with turn numbers.
Verdicts: pass, fail, unclear, not_tested (situation never arose)."""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[\"'“”‘’«»]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def format_transcript(turns: list[Turn]) -> str:
    who = {"shopper": "CUSTOMER", "agent": "AGENT"}
    return "\n".join(f"[{t.n}] {who[t.speaker]}: {t.text}" for t in turns)


def applicability(rule: Rule, persona: Persona) -> Optional[str]:
    """Return a verdict if the rule should not be graded for this persona, else None."""
    if persona.customer_type not in rule.customer_types:
        return "not_applicable"
    if rule.requires_persona_trait and rule.requires_persona_trait not in persona.traits:
        return "not_tested"
    return None


_SENT = re.compile(r"(?<=[.!?])\s+")


def precheck_pass(rule: Rule, turns: list[Turn]) -> Optional[tuple[list[int], list[str]]]:
    """Return (turns, quotes) if the rule's keyword pre-check clearly passes, else None.
    A pre-check can only pass a rule; anything else goes to the AI grader."""
    pc = rule.precheck
    if not pc:
        return None
    agent = [t for t in turns if t.speaker == "agent"]
    if pc.unless and any(re.search(pc.unless, t.text, re.I) for t in agent):
        return None
    ev_turns, ev_quotes = [], []
    for pattern in pc.all_of:
        hit = next(((t, m) for t in agent for m in [re.search(pattern, t.text, re.I)] if m), None)
        if not hit:
            return None
        t, m = hit
        sentence = next((s for s in _SENT.split(t.text) if m.group(0) in s), t.text)
        if (t.n, sentence.strip()) not in zip(ev_turns, ev_quotes):
            ev_turns.append(t.n)
            ev_quotes.append(sentence.strip())
    return ev_turns, ev_quotes


class Grader:
    def __init__(self, llm: LLM, double_check: bool = True, review_threshold: float = 0.8, batch: bool = False,
                 selective: bool = False, prechecks: bool = False):
        self.llm = llm
        self.batch = batch
        self.selective = selective
        self.prechecks = prechecks
        self.double_check = double_check
        self.review_threshold = review_threshold

    def _ask(self, system: str, rule: Rule, transcript: str) -> dict:
        prompt = (
            f"RULE {rule.id}: {rule.question}\n"
            f"What to check: {rule.check.strip()}\n\n"
            f"CONVERSATION\n{transcript}\n\n"
            "Grade this rule."
        )
        return self.llm.structured(system, [{"role": "user", "content": prompt}], SCHEMA, name="grade")

    @staticmethod
    def _verify(raw: dict, turns: list[Turn]) -> tuple[list[int], list[str], list[str]]:
        by_n = {t.n: _norm(t.text) for t in turns}
        ok_turns, ok_quotes, notes = [], [], []
        for ev in raw.get("evidence", []) or []:
            n, q = ev.get("turn"), (ev.get("quote") or "").strip()
            if not q:
                continue
            if n in by_n and _norm(q) in by_n[n]:
                ok_turns.append(n)
                ok_quotes.append(q)
            elif any(_norm(q) in txt for txt in by_n.values()):
                real = next(k for k, txt in by_n.items() if _norm(q) in txt)
                ok_turns.append(real)
                ok_quotes.append(q)
                notes.append(f"Quote found in turn {real}, model said {n}")
            else:
                notes.append(f"Quote not found in transcript, removed: '{q[:80]}'")
        return ok_turns, ok_quotes, notes

    @staticmethod
    def _base(rule: Rule) -> dict:
        return dict(rule_id=rule.id, critical=rule.critical, breach_type=rule.breach_type, kind=rule.kind,
                    question=rule.question, basis=rule.basis, consequence=rule.consequence)

    @staticmethod
    def _skipped(rule: Rule, persona: Persona) -> Optional[Finding]:
        skip = applicability(rule, persona)
        if not skip:
            return None
        reason = ("Rule does not apply to this customer type." if skip == "not_applicable"
                  else f"Needs a shopper with trait '{rule.requires_persona_trait}'.")
        return Finding(verdict=skip, reasoning=reason, confidence=1.0, **Grader._base(rule))

    def _precheck_finding(self, rule: Rule, turns: list[Turn]) -> Optional[Finding]:
        if not self.prechecks:
            return None
        hit = precheck_pass(rule, turns)
        if not hit:
            return None
        return Finding(verdict="pass", reasoning="The required information appears in the agent's words (keyword pre-check).",
                       evidence_turns=hit[0], evidence_quotes=hit[1], confidence=0.9,
                       guard_notes=["Passed by keyword pre-check, no AI call."], **self._base(rule))

    def _needs_second(self, a: dict) -> bool:
        """Selective mode: second review only for failures and uncertain results."""
        if not self.double_check or a.get("verdict") not in ("pass", "fail"):
            return False
        if not self.selective:
            return True
        return a.get("verdict") == "fail" or float(a.get("confidence", 0) or 0) < self.review_threshold

    def _finalize(self, rule: Rule, a: dict, b: Optional[dict], turns: list[Turn]) -> Finding:
        """Apply the evidence guard, the second review and the lawyer flag to one raw grade."""
        verdict = a.get("verdict", "unclear")
        if verdict not in ("pass", "fail", "unclear", "not_tested"):
            verdict = "unclear"
        conf = float(a.get("confidence", 0) or 0)
        ev_turns, ev_quotes, notes = self._verify(a, turns)
        if verdict == "fail" and not ev_quotes:
            notes.append("Failure without verifiable evidence: downgraded to unclear.")
            verdict = "unclear"
        agent_turns = {t.n for t in turns if t.speaker == "agent"}
        if verdict == "fail" and not agent_turns.intersection(ev_turns):
            notes.append("Failure quotes only the customer, not the agent: downgraded to unclear.")
            verdict = "unclear"
        needs_lawyer = False
        if b is not None and verdict in ("pass", "fail"):
            vb = b.get("verdict", "unclear")
            if vb != verdict:
                notes.append(f"Second review disagreed ({verdict} vs {vb}).")
                needs_lawyer = True
                if {verdict, vb} == {"pass", "fail"}:
                    verdict = "unclear"
                conf = min(conf, float(b.get("confidence", 0) or 0))
        if verdict == "unclear" or conf < self.review_threshold or (verdict == "fail" and rule.critical):
            needs_lawyer = True
        return Finding(
            verdict=verdict, reasoning=a.get("reasoning", ""), evidence_turns=ev_turns,
            evidence_quotes=ev_quotes, confidence=round(conf, 2),
            fix=a.get("fix", "") if verdict != "pass" else "", needs_lawyer=needs_lawyer,
            guard_notes=notes, **self._base(rule),
        )

    def grade_rule(self, rule: Rule, persona: Persona, turns: list[Turn]) -> Finding:
        skipped = self._skipped(rule, persona)
        if skipped:
            return skipped
        pre = self._precheck_finding(rule, turns)
        if pre:
            return pre
        transcript = format_transcript(turns)
        a = self._ask(SYSTEM_A, rule, transcript)
        b = self._ask(SYSTEM_B, rule, transcript) if self._needs_second(a) else None
        return self._finalize(rule, a, b, turns)

    def _ask_batch(self, system: str, rules: list[Rule], transcript: str) -> dict[str, dict]:
        listing = "\n".join(f"RULE {r.id}: {r.question}\n  What to check: {r.check.strip()}" for r in rules)
        prompt = (f"Grade EACH of these rules separately.\n\n{listing}\n\n"
                  f"CONVERSATION\n{transcript}\n\nReturn one result per rule id.")
        out = self.llm.structured(system, [{"role": "user", "content": prompt}], BATCH_SCHEMA,
                                  name="grades", max_tokens=4000)
        return {str(x.get("rule_id")): x for x in out.get("results", []) if isinstance(x, dict)}

    def grade(self, rules: list[Rule], persona: Persona, turns: list[Turn]) -> list[Finding]:
        if not self.batch:
            out = []
            for r in rules:
                log.info("  grading %s ...", r.id)
                out.append(self.grade_rule(r, persona, turns))
            return out
        # lite mode: all rules in one request (plus one for the second review)
        results: dict[str, Finding] = {}
        todo = []
        for r in rules:
            skipped = self._skipped(r, persona) or self._precheck_finding(r, turns)
            if skipped:
                results[r.id] = skipped
            else:
                todo.append(r)
        if self.prechecks:
            n_pre = sum(1 for f in results.values() if f.guard_notes[:1] == ["Passed by keyword pre-check, no AI call."])
            if n_pre:
                log.info("  %d rule(s) passed by keyword pre-check (no AI call)", n_pre)
        if todo:
            transcript = format_transcript(turns)
            log.info("  grading %d rules in one request ...", len(todo))
            first = self._ask_batch(SYSTEM_A, todo, transcript)
            missing = {"verdict": "unclear", "reasoning": "No result returned for this rule.", "confidence": 0}
            recheck = [r for r in todo if self._needs_second(first.get(r.id, missing))]
            second: dict[str, dict] = {}
            if recheck:
                log.info("  second, independent review of %d rule(s) ...", len(recheck))
                second = self._ask_batch(SYSTEM_B, recheck, transcript)
            for r in todo:
                a = first.get(r.id, missing)
                b = second.get(r.id, {"verdict": "unclear"}) if r in recheck else None
                results[r.id] = self._finalize(r, a, b, turns)
        return [results[r.id] for r in rules]


def score(findings: list[Finding]) -> tuple[Optional[float], float, int]:
    """(score 0-100 or None, coverage 0-1, critical failures). Critical rules count double.
    Unclear results stay out of the score until a lawyer decides."""
    graded = [f for f in findings if f.verdict in ("pass", "fail")]
    applicable = [f for f in findings if f.verdict != "not_applicable"]
    weight = lambda f: 2 if f.critical else 1  # noqa: E731
    total = sum(weight(f) for f in graded)
    s = round(100 * sum(weight(f) for f in graded if f.verdict == "pass") / total, 1) if total else None
    coverage = round(len(graded) / len(applicable), 2) if applicable else 0.0
    crit = sum(1 for f in findings if f.verdict == "fail" and f.critical)
    return s, coverage, crit
