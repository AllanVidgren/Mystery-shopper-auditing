"""HTTP API (Starlette). Feeds the dashboard.

Access model (privacy by design):
  X-MS-Role: local:FI  -> runs, transcripts and findings for Finland only; can decide
  X-MS-Role: group     -> patterns, heatmap, country scores, escalations only
Replace the header with your SSO claims in production; the checks stay the same.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import aggregate
from .config import Library, Settings, load_targets
from .llm import LLM, make_llm
from .runner import run_audit
from .store import Store
from .targets import TargetError, build_target

log = logging.getLogger("mystery_shopper")


class Forbidden(Exception):
    pass


def _role(request: Request) -> tuple[str, Optional[str]]:
    raw = request.headers.get("x-ms-role", "")
    if raw == "group":
        return "group", None
    if raw.startswith("local:") and len(raw) > 6:
        return "local", raw[6:].upper()
    raise Forbidden("Send X-MS-Role: group or local:<COUNTRY>")


def create_app(lib: Library, store: Store, settings: Settings, targets_file: Optional[str] = None,
               llm: Optional[LLM] = None) -> Starlette:
    targets = load_targets(targets_file) if targets_file else {}

    def get_llm() -> LLM:
        return llm or make_llm(settings)

    def guard(request: Request):
        if settings.api_token:
            if request.headers.get("authorization", "") != f"Bearer {settings.api_token}":
                raise Forbidden("Bad or missing API token")
        return _role(request)

    async def health(_):
        return JSONResponse({"ok": True, "rulebook": lib.rulebook_version})

    async def library(request: Request):
        guard(request)
        return JSONResponse({
            "rules": [r.model_dump() | {"breach_type": r.breach_type} for r in lib.rules],
            "personas": [p.model_dump() for p in lib.personas.values()],
            "scenarios": [s.model_dump() for s in lib.scenarios.values()],
            "targets": [{"name": n, "type": t.get("type"), "team": t.get("team")} for n, t in targets.items()],
        })

    async def start_run(request: Request):
        role, country = guard(request)
        body = await request.json()
        cc = (body.get("country") or "").upper()
        if role == "local" and cc != country:
            raise Forbidden("Local lawyers can only start runs for their own country")
        for k in ("country", "persona", "scenario", "target"):
            if not body.get(k):
                return JSONResponse({"error": f"missing {k}"}, status_code=400)
        if body["target"] not in targets:
            return JSONResponse({"error": "unknown target"}, status_code=400)
        if body["persona"] not in lib.personas or body["scenario"] not in lib.scenarios:
            return JSONResponse({"error": "unknown persona or scenario"}, status_code=400)
        tspec = targets[body["target"]]
        the_llm = get_llm()
        target = build_target(tspec, the_llm)  # raises if not authorised
        run_id = f"{cc}-{lib.scenarios[body['scenario']].channel.upper()}-{uuid.uuid4().hex[:8]}"

        def work():
            try:
                run_audit(lib, the_llm, store, country=cc, persona_id=body["persona"],
                          scenario_id=body["scenario"], target=target,
                          simulated=tspec.get("type") == "simulated", language=body.get("language", "English"),
                          test_account=body.get("test_account"), double_check=settings.double_check,
                          review_threshold=settings.review_threshold, run_id=run_id,
                          batch_grading=settings.batch_grading, max_turns=settings.max_turns,
                           economy=settings.economy)
            except Exception as e:  # stored as error by run_audit
                log.error("background run %s failed: %s", run_id, e)

        threading.Thread(target=work, daemon=True).start()
        return JSONResponse({"run_id": run_id, "status": "running"}, status_code=202)

    async def list_runs(request: Request):
        role, country = guard(request)
        if role == "group":
            raise Forbidden("Group view has no access to individual runs")
        return JSONResponse(store.runs(country=country))

    async def get_run(request: Request):
        role, country = guard(request)
        run = store.run(request.path_params["run_id"])
        if role == "group" or run["country"] != country:
            raise Forbidden("Not your country")
        return JSONResponse(run)

    async def country_findings(request: Request):
        role, country = guard(request)
        cc = request.path_params["cc"].upper()
        if role == "group" or cc != country:
            raise Forbidden("Not your country")
        sim = request.query_params.get("include_simulated") == "1"
        return JSONResponse(aggregate.country_view(store.all_findings(include_simulated=sim), cc))

    async def decide(request: Request):
        role, country = guard(request)
        fid = int(request.path_params["fid"])
        f = store.finding(fid)
        run = store.run(f["run_id"])
        if role == "group" or run["country"] != country:
            raise Forbidden("Only the local lawyer of that country decides on findings")
        body = await request.json()
        try:
            return JSONResponse(store.decide(fid, body.get("action", ""), body.get("by", ""), body.get("note", "")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def group_overview(request: Request):
        role, _ = guard(request)
        if role != "group":
            raise Forbidden("Group view only")
        sim = request.query_params.get("include_simulated") == "1"
        pend = request.query_params.get("include_pending") == "1"
        return JSONResponse(aggregate.group_overview(lib, store.all_findings(include_simulated=sim),
                                                     include_pending_in_patterns=pend))

    async def exposure(request: Request):
        guard(request)
        body = await request.json()
        sim = request.query_params.get("include_simulated") == "1"
        return JSONResponse(aggregate.exposure(store.all_findings(include_simulated=sim),
                                               body.get("monthly_volume", {}), body.get("value_per_case")))

    async def forbidden(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=403)

    async def not_found(_request, exc):
        return JSONResponse({"error": f"not found: {exc}"}, status_code=404)

    async def target_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/api/library", library),
            Route("/api/runs", start_run, methods=["POST"]),
            Route("/api/runs", list_runs, methods=["GET"]),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/countries/{cc}/findings", country_findings),
            Route("/api/findings/{fid:int}/decision", decide, methods=["POST"]),
            Route("/api/group/overview", group_overview),
            Route("/api/exposure", exposure, methods=["POST"]),
        ],
        exception_handlers={Forbidden: forbidden, KeyError: not_found, TargetError: target_error},
    )
