"""Target adapters: how the shopper reaches the company's channel."""
from __future__ import annotations

from typing import Optional

from ..llm import LLM
from .base import Target, TargetError
from .http_target import HttpTarget
from .simulated import SimulatedTarget


def build_target(spec: dict, llm: Optional[LLM] = None) -> Target:
    """Create a target from its config entry. Refuses targets that are not authorised."""
    kind = spec.get("type")
    if kind != "simulated":
        if not spec.get("authorized") or not spec.get("authorized_by"):
            raise TargetError(
                f"Target '{spec.get('name')}' is not authorised. Set authorized: true and "
                "authorized_by: <person/role who approved testing this channel>."
            )
    if kind == "simulated":
        if llm is None:
            raise TargetError("Simulated target needs an LLM")
        return SimulatedTarget(spec, llm)
    if kind == "http":
        return HttpTarget(spec)
    if kind == "browser":
        from .browser_target import BrowserTarget  # imported lazily: needs Playwright
        return BrowserTarget(spec)
    raise TargetError(f"Unknown target type: {kind}")


__all__ = ["Target", "TargetError", "build_target"]
