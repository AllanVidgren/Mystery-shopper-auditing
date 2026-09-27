"""Views on the stored findings.

Local lawyer: one country, every finding, transcripts.
Group lawyer: patterns and scores only. No transcripts, no individual findings except
escalated ones, no team labels. Only lawyer-confirmed failures count as confirmed.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from .config import Library


def _state(f: dict) -> str:
    """Cell state for a single finding, from the group's point of view."""
    if f["lawyer_status"] == "overturned":
        return "pass"
    if f["verdict"] == "fail" and f["lawyer_status"] == "confirmed":
        return "fail"
    if f["verdict"] in ("fail", "unclear"):
        return "review"
    if f["verdict"] == "pass":
        return "pass"
    return "untested"


_RANK = {"fail": 4, "review": 3, "pass": 2, "untested": 1}


def group_overview(lib: Library, findings: list[dict], min_countries: int = 2,
                   include_pending_in_patterns: bool = False) -> dict:
    countries = sorted({f["country"] for f in findings})
    rules = [r for r in lib.rules]
    cell: dict[tuple[str, str], str] = {}
    per_country: dict[str, dict] = defaultdict(lambda: {"runs": set(), "confirmed_critical_open": 0,
                                                         "in_review": 0, "pass_w": 0, "total_w": 0,
                                                         "graded": 0, "applicable": 0, "open_days": []})
    for f in findings:
        st = _state(f)
        key = (f["rule_id"], f["country"])
        if _RANK[st] > _RANK.get(cell.get(key, "untested"), 0):
            cell[key] = st
        c = per_country[f["country"]]
        c["runs"].add(f["run_id"])
        if f["verdict"] != "not_applicable":
            c["applicable"] += 1
        if st in ("pass", "fail"):
            c["graded"] += 1
            w = 2 if f["critical"] else 1
            c["total_w"] += w
            c["pass_w"] += w if st == "pass" else 0
        if st == "fail" and f["critical"] and f.get("fix_status") != "verified":
            c["confirmed_critical_open"] += 1
        if st == "review":
            c["in_review"] += 1

    heatmap = [{"rule_id": r.id, "question": r.question, "critical": r.critical, "channel": r.channel,
                "cells": {cc: cell.get((r.id, cc), "untested") for cc in countries}} for r in rules]

    patterns = []
    for r in rules:
        failing = [cc for cc in countries
                   if cell.get((r.id, cc)) == "fail" or
                   (include_pending_in_patterns and cell.get((r.id, cc)) == "review")]
        if len(failing) >= min_countries:
            patterns.append({
                "rule_id": r.id, "question": r.question, "critical": r.critical, "kind": r.kind,
                "breach_type": r.breach_type, "countries": failing, "consequence": r.consequence,
                "likely_cause": "Group script or group chatbot configuration (same failure in several countries)",
            })
    patterns.sort(key=lambda p: (-len(p["countries"]), not p["critical"]))

    cards = []
    for cc in countries:
        c = per_country[cc]
        cards.append({
            "country": cc, "audits": len(c["runs"]),
            "score": round(100 * c["pass_w"] / c["total_w"], 1) if c["total_w"] else None,
            "coverage": round(c["graded"] / c["applicable"], 2) if c["applicable"] else 0.0,
            "confirmed_critical_open": c["confirmed_critical_open"], "in_review": c["in_review"],
        })
    cards.sort(key=lambda x: (x["score"] is None, x["score"] if x["score"] is not None else 0))

    escalations = [{
        "finding_id": f["id"], "country": f["country"], "rule_id": f["rule_id"], "question": f["question"],
        "verdict": f["verdict"], "reasoning": f["reasoning"], "evidence": f["evidence"], "basis": f["basis"],
        "consequence": f["consequence"], "run_id": f["run_id"],
    } for f in findings if f["escalated"]]

    total_graded = sum(c["graded"] for c in per_country.values())
    total_applicable = sum(c["applicable"] for c in per_country.values())
    return {
        "kpis": {
            "confirmed_critical_open": sum(c["confirmed_critical_open"] for c in cards),
            "systemic_patterns": len(patterns),
            "coverage": round(total_graded / total_applicable, 2) if total_applicable else 0.0,
            "escalations_waiting": len(escalations),
            "in_review": sum(c["in_review"] for c in cards),
        },
        "patterns": patterns, "heatmap": heatmap, "countries": cards, "escalations": escalations,
        "note": "Findings and audit logs stay with each country's local lawyer.",
    }


def country_view(findings: list[dict], country: str) -> dict:
    fs = [f for f in findings if f["country"] == country.upper()]
    order = {"fail": 0, "unclear": 1, "pass": 2, "not_tested": 3, "not_applicable": 4}
    fs.sort(key=lambda f: (order.get(f["verdict"], 9), not f["critical"]))
    return {
        "country": country.upper(),
        "open_for_review": [f for f in fs if f["needs_lawyer"] and f["lawyer_status"] == "open"],
        "findings": fs,
    }


def exposure(findings: list[dict], monthly_volume: dict[str, dict[str, int]],
             value_per_case: Optional[dict[str, float]] = None) -> list[dict]:
    """Rough monthly exposure: failure rate x monthly conversations x value per affected case.

    monthly_volume: {"FI": {"sales": 12000, "cancellation": 3000, "chat": 20000}}
    value_per_case: {"S1": 199.0, "S2": 480.0}  e.g. uncollectable fee, refundable revenue
    """
    value_per_case = value_per_case or {}
    stats: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for f in findings:
        if f["verdict"] not in ("pass", "fail") or f["lawyer_status"] == "overturned":
            continue
        k = (f["country"], f["channel"], f["rule_id"])
        stats[k][1] += 1
        stats[k][0] += 1 if f["verdict"] == "fail" else 0
    out = []
    for (cc, ch, rid), (fails, n) in stats.items():
        vol = monthly_volume.get(cc, {}).get(ch)
        if not vol or not fails:
            continue
        rate = fails / n
        affected = round(rate * vol)
        out.append({"country": cc, "rule_id": rid, "failure_rate": round(rate, 2), "sample_size": n,
                    "affected_per_month": affected,
                    "eur_per_month": round(affected * value_per_case[rid]) if rid in value_per_case else None,
                    "caution": "Small sample" if n < 20 else ""})
    return sorted(out, key=lambda x: -(x["eur_per_month"] or 0))
