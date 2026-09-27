"""Command line.

  python -m mystery_shopper run --country FI --persona marcus --scenario website_chat \
      --target demo-sim-flawed --targets config/targets.example.yaml
  python -m mystery_shopper campaign campaigns/demo.yaml
  python -m mystery_shopper group [--include-simulated]
  python -m mystery_shopper show RUN_ID
  python -m mystery_shopper decide FINDING_ID confirm --by "Local lawyer FI" [--note ...]
  python -m mystery_shopper purge --days 30
  python -m mystery_shopper serve --targets config/targets.yaml --port 8000
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import aggregate
from .campaign import run_campaign
from .config import Library, Settings, load_targets
from .llm import make_llm
from .runner import run_audit
from .store import Store
from .targets import build_target


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _summary(result) -> None:
    print(f"\nRun {result.run_id}  score={result.score}  coverage={result.coverage:.0%}  "
          f"critical failures={result.critical_failures}\n")
    for t in result.turns:
        who = "SHOPPER" if t.speaker == "shopper" else "AGENT  "
        print(f"[{t.n:>2}] {who} {t.text}")
    print()
    for f in result.findings:
        flag = "!" if f.critical else " "
        law = "legal " if f.breach_type == "legal" else "policy"
        lawyer = "  -> lawyer" if f.needs_lawyer else ""
        print(f"{flag} {f.rule_id:<3} {f.verdict:<14} {law} {f.confidence:.2f}  {f.question}{lawyer}")
        if f.verdict == "fail":
            for n, q in zip(f.evidence_turns, f.evidence_quotes):
                print(f"        turn {n}: \"{q}\"")
            print(f"        {f.reasoning}")
            if f.fix:
                print(f"        fix: {f.fix}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mystery_shopper")
    ap.add_argument("--config-dir", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--country", required=True)
    r.add_argument("--persona", required=True)
    r.add_argument("--scenario", required=True)
    r.add_argument("--target", required=True)
    r.add_argument("--targets", required=True, help="targets YAML file")
    r.add_argument("--language", default="English")
    r.add_argument("--test-account", default=None, help='JSON, e.g. {"customer_number": "TEST-1"}')

    c = sub.add_parser("campaign")
    c.add_argument("file")

    g = sub.add_parser("group")
    g.add_argument("--include-simulated", action="store_true")
    g.add_argument("--include-pending", action="store_true")

    s = sub.add_parser("show")
    s.add_argument("run_id")

    d = sub.add_parser("decide")
    d.add_argument("finding_id", type=int)
    d.add_argument("action", choices=sorted(["confirm", "overturn", "escalate", "note", "fixed", "verified"]))
    d.add_argument("--by", required=True)
    d.add_argument("--note", default="")

    p = sub.add_parser("purge")
    p.add_argument("--days", type=int, default=30)

    sv = sub.add_parser("serve")
    sv.add_argument("--targets", default=None)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    a = ap.parse_args(argv)
    chatty = a.verbose or a.cmd in ("run", "campaign")
    logging.basicConfig(level=logging.INFO if chatty else logging.WARNING, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if a.db:
        settings.db_path = a.db
    lib = Library.load(a.config_dir) if a.config_dir else Library.load()
    store = Store(settings.db_path)

    if a.cmd == "run":
        llm = make_llm(settings)
        tspec = load_targets(a.targets)[a.target]
        target = build_target(tspec, llm)
        result = run_audit(lib, llm, store, country=a.country, persona_id=a.persona, scenario_id=a.scenario,
                           target=target, simulated=tspec.get("type") == "simulated", language=a.language,
                           test_account=json.loads(a.test_account) if a.test_account else None,
                           double_check=settings.double_check, review_threshold=settings.review_threshold,
                           batch_grading=settings.batch_grading, max_turns=settings.max_turns,
                           economy=settings.economy)
        _summary(result)
    elif a.cmd == "campaign":
        llm = make_llm(settings)
        _print(run_campaign(a.file, lib, llm, store, settings))
    elif a.cmd == "group":
        _print(aggregate.group_overview(lib, store.all_findings(include_simulated=a.include_simulated),
                                        include_pending_in_patterns=a.include_pending))
    elif a.cmd == "show":
        _print(store.run(a.run_id))
    elif a.cmd == "decide":
        _print(store.decide(a.finding_id, a.action, a.by, a.note))
    elif a.cmd == "purge":
        cutoff = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat(timespec="seconds")
        print(f"Transcripts removed: {store.purge_transcripts(cutoff)}")
    elif a.cmd == "serve":
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(lib, store, settings, a.targets), host=a.host, port=a.port)
    return 0


def cli() -> int:
    from .llm import LLMError
    from .targets import TargetError
    try:
        return main()
    except (LLMError, TargetError, KeyError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
