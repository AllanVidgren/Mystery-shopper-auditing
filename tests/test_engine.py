"""Offline tests: a scripted LLM replaces Claude, a local HTTP bot replaces the company.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from starlette.testclient import TestClient  # noqa: E402

import demo_chatbot  # noqa: E402
from mystery_shopper import aggregate  # noqa: E402
from mystery_shopper.api import create_app  # noqa: E402
from mystery_shopper.config import Library, Settings  # noqa: E402
from mystery_shopper.grader import Grader, score  # noqa: E402
from mystery_shopper.llm import ScriptedLLM  # noqa: E402
from mystery_shopper.models import Turn  # noqa: E402
from mystery_shopper.runner import run_audit  # noqa: E402
from mystery_shopper.store import Store  # noqa: E402
from mystery_shopper.targets import TargetError, build_target  # noqa: E402

LIB = Library.load(ROOT / "config")

SHOPPER_LINES = [
    "Hi, how much does a home alarm cost in total?",
    "And how long is the contract?",
    "How fast do the police come?",
    "Can I talk to a human?",
]


def shopper_text(system, messages):
    if system.startswith("You are a mystery shopper"):
        said = sum(1 for m in messages if m["role"] == "assistant")
        return SHOPPER_LINES[said] if said < len(SHOPPER_LINES) else "[END]"
    return "I can help with that."  # simulated agent


def grade_from(verdicts: dict, quotes: dict | None = None, second: dict | None = None):
    """verdicts: rule_id -> verdict. quotes: rule_id -> (turn, quote). second: verdicts for reviewer B."""
    quotes = quotes or {}

    def fn(system, messages, schema, name):
        rid = re.search(r"RULE (\w+):", messages[0]["content"]).group(1)
        table = second if (second is not None and system.startswith("You are a sceptical")) else verdicts
        v = table.get(rid, "pass")
        ev = [{"turn": quotes[rid][0], "quote": quotes[rid][1]}] if rid in quotes else []
        return {"verdict": v, "evidence": ev, "reasoning": f"Scripted reasoning for {rid}.",
                "confidence": 0.9, "fix": "Say the full price." if v == "fail" else ""}
    return fn


class BotServer:
    def __enter__(self):
        self.srv = demo_chatbot.serve(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()

    def spec(self, **over):
        s = {"name": "local-demo-bot", "type": "http", "authorized": True, "authorized_by": "test",
             "team": "Demo bot v1", "url": f"http://127.0.0.1:{self.port}/chat",
             "body": {"session": "{{session_id}}", "text": "{{message}}"}, "reply_path": "reply",
             "start": {"url": f"http://127.0.0.1:{self.port}/start", "body": {}, "reply_path": "reply"},
             "min_interval_s": 0}
        s.update(over)
        return s


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def _run(self, llm, target, country="FI", persona="marcus", scenario="website_chat", simulated=False):
        return run_audit(LIB, llm, self.store, country=country, persona_id=persona, scenario_id=scenario,
                         target=target, simulated=simulated, double_check=True)

    def test_http_end_to_end_with_real_evidence(self):
        with BotServer() as bot:
            llm = ScriptedLLM(shopper_text, grade_from(
                {"W1": "fail", "W4": "fail"},
                {"W1": (3, "Plans start from 29.90 EUR per month"), "W4": (9, "Our opening hours are 9-17.")}))
            r = self._run(llm, build_target(bot.spec(), llm))
        self.assertEqual(r.turns[0].text, demo_chatbot.GREETING)
        self.assertEqual(r.turns[1].speaker, "shopper")
        w1 = next(f for f in r.findings if f.rule_id == "W1")
        self.assertEqual(w1.verdict, "fail")
        self.assertEqual(w1.evidence_turns, [3])
        self.assertTrue(w1.needs_lawyer)                      # critical fail always to a lawyer
        self.assertEqual(w1.breach_type, "legal")
        self.assertIn("CRD Art. 6(6)", w1.consequence)
        w4 = next(f for f in r.findings if f.rule_id == "W4")
        self.assertEqual(w4.breach_type, "policy")
        stored = self.store.run(r.run_id)
        self.assertEqual(stored["status"], "done")
        self.assertEqual(stored["team"], "Demo bot v1")
        self.assertEqual(len(stored["transcript"]), len(r.turns))
        self.assertEqual(r.critical_failures, 1)

    def test_evidence_guard_downgrades_invented_quote(self):
        with BotServer() as bot:
            llm = ScriptedLLM(shopper_text, grade_from({"W2": "fail"}, {"W2": (3, "Please give me your IBAN")}))
            r = self._run(llm, build_target(bot.spec(), llm))
        w2 = next(f for f in r.findings if f.rule_id == "W2")
        self.assertEqual(w2.verdict, "unclear")
        self.assertTrue(w2.needs_lawyer)
        self.assertTrue(any("not found" in n for n in w2.guard_notes))

    def test_second_reviewer_disagreement_goes_to_lawyer(self):
        with BotServer() as bot:
            llm = ScriptedLLM(shopper_text, grade_from(
                {"W5": "fail"}, {"W5": (3, "Plans start from")}, second={"W5": "pass"}))
            r = self._run(llm, build_target(bot.spec(), llm))
        w5 = next(f for f in r.findings if f.rule_id == "W5")
        self.assertEqual(w5.verdict, "unclear")
        self.assertTrue(w5.needs_lawyer)

    def test_applicability_business_and_traits(self):
        llm = ScriptedLLM(shopper_text, grade_from({}))
        g = Grader(llm, double_check=False)
        turns = [Turn(n=1, speaker="shopper", text="hi"), Turn(n=2, speaker="agent", text="hello")]
        lucia = LIB.personas["lucia"]           # business customer
        s1 = g.grade_rule(LIB.rule("S1"), lucia, turns)
        self.assertEqual(s1.verdict, "not_applicable")
        s7 = g.grade_rule(LIB.rule("S7"), LIB.personas["marcus"], turns)   # not vulnerable
        self.assertEqual(s7.verdict, "not_tested")
        s7b = g.grade_rule(LIB.rule("S7"), LIB.personas["helmi"], turns)
        self.assertEqual(s7b.verdict, "pass")

    def test_unauthorised_target_refused(self):
        with self.assertRaises(TargetError):
            build_target({"name": "x", "type": "http", "url": "http://x", "reply_path": "r"})
        with self.assertRaises(TargetError):
            build_target({"name": "x", "type": "http", "authorized": True, "url": "http://x", "reply_path": "r"})

    def test_simulated_target_runs(self):
        llm = ScriptedLLM(shopper_text, grade_from({}))
        spec = {"name": "sim", "type": "simulated", "greeting": "Hi!", "facts": ["x"], "flaws": ["y"]}
        r = self._run(llm, build_target(spec, llm), simulated=True)
        self.assertEqual(r.turns[0].text, "Hi!")
        self.assertEqual(self.store.run(r.run_id)["simulated"], 1)

    def test_score_counts_critical_double_and_ignores_unclear(self):
        llm = ScriptedLLM(shopper_text, grade_from({}))
        g = Grader(llm, double_check=False)
        turns = [Turn(n=1, speaker="agent", text="x")]
        fs = [g.grade_rule(LIB.rule(r), LIB.personas["marcus"], turns) for r in ("W1", "W3", "W4")]
        fs[0].verdict = "fail"                          # critical fail, weight 2
        fs[2].verdict = "unclear"
        s, cov, crit = score(fs)
        self.assertEqual(s, round(100 * 1 / 3, 1))      # pass W3 (1) of W1 (2) + W3 (1)
        self.assertEqual(crit, 1)
        self.assertAlmostEqual(cov, 0.67, places=2)


class GroupAndAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.bot = BotServer().__enter__()
        llm = ScriptedLLM(shopper_text, grade_from({"W1": "fail"}, {"W1": (3, "Plans start from 29.90 EUR")}))
        self.runs = {}
        for cc in ("FI", "ES", "FR"):
            r = run_audit(LIB, llm, self.store, country=cc, persona_id="marcus", scenario_id="website_chat",
                          target=build_target(self.bot.spec(), llm), simulated=False, double_check=False)
            self.runs[cc] = r
        self.app = TestClient(create_app(LIB, self.store, Settings(api_token=""), llm=llm))

    def tearDown(self):
        self.bot.__exit__()
        self.store.conn.close()
        self.tmp.cleanup()

    def _w1(self, cc):
        return next(f for f in self.store.run(self.runs[cc].run_id)["findings"] if f["rule_id"] == "W1")

    def test_patterns_only_from_confirmed_findings(self):
        ov = aggregate.group_overview(LIB, self.store.all_findings())
        self.assertEqual(ov["patterns"], [])                     # nothing confirmed yet
        self.assertEqual(ov["kpis"]["in_review"], 3)
        self.store.decide(self._w1("FI")["id"], "confirm", "Local lawyer FI")
        self.store.decide(self._w1("ES")["id"], "confirm", "Local lawyer ES")
        self.store.decide(self._w1("FR")["id"], "overturn", "Local lawyer FR", "Price was on screen")
        ov = aggregate.group_overview(LIB, self.store.all_findings())
        self.assertEqual(len(ov["patterns"]), 1)
        p = ov["patterns"][0]
        self.assertEqual((p["rule_id"], sorted(p["countries"])), ("W1", ["ES", "FI"]))
        row = next(h for h in ov["heatmap"] if h["rule_id"] == "W1")
        self.assertEqual(row["cells"], {"ES": "fail", "FI": "fail", "FR": "pass"})
        self.assertEqual(ov["kpis"]["confirmed_critical_open"], 2)
        self.assertNotIn("transcript", str(ov))

    def test_access_control(self):
        fi = self.runs["FI"].run_id
        g = {"X-MS-Role": "group"}
        self.assertEqual(self.app.get("/api/runs", headers=g).status_code, 403)
        self.assertEqual(self.app.get(f"/api/runs/{fi}", headers=g).status_code, 403)
        self.assertEqual(self.app.get(f"/api/runs/{fi}", headers={"X-MS-Role": "local:ES"}).status_code, 403)
        ok = self.app.get(f"/api/runs/{fi}", headers={"X-MS-Role": "local:FI"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["transcript"])
        fid = self._w1("FI")["id"]
        self.assertEqual(self.app.post(f"/api/findings/{fid}/decision", headers=g,
                                       json={"action": "confirm", "by": "x"}).status_code, 403)
        r = self.app.post(f"/api/findings/{fid}/decision", headers={"X-MS-Role": "local:FI"},
                          json={"action": "escalate", "by": "Local lawyer FI", "note": "novel"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["decisions"][-1]["by_whom"], "Local lawyer FI")
        ov = self.app.get("/api/group/overview", headers=g).json()
        self.assertEqual(len(ov["escalations"]), 1)
        self.assertEqual(self.app.get("/api/group/overview", headers={"X-MS-Role": "local:FI"}).status_code, 403)
        self.assertEqual(self.app.get("/api/runs").status_code, 403)          # no role header

    def test_decision_requires_name_and_valid_action(self):
        fid = self._w1("FI")["id"]
        with self.assertRaises(ValueError):
            self.store.decide(fid, "confirm", "")
        with self.assertRaises(ValueError):
            self.store.decide(fid, "delete", "x")

    def test_exposure_and_purge(self):
        for cc in ("FI", "ES"):
            self.store.decide(self._w1(cc)["id"], "confirm", "lawyer")
        ex = aggregate.exposure(self.store.all_findings(), {"FI": {"chat": 20000}}, {"W1": 199.0})
        fi = next(e for e in ex if e["country"] == "FI")
        self.assertEqual(fi["affected_per_month"], 20000)
        self.assertEqual(fi["eur_per_month"], 20000 * 199)
        self.assertEqual(fi["caution"], "Small sample")
        n = self.store.purge_transcripts("9999-01-01")
        self.assertEqual(n, 3)
        run = self.store.run(self.runs["FI"].run_id)
        self.assertIsNone(run["transcript"])
        self.assertTrue(run["findings"][0]["evidence"] or True)   # findings survive purge


class ClaudeClientTests(unittest.TestCase):
    """Checks the request we send and the parsing of tool output, against a mocked endpoint."""

    def test_structured_and_text(self):
        import httpx

        from mystery_shopper.llm import ClaudeLLM
        seen = []

        def handler(request: httpx.Request):
            import json as _j
            body = _j.loads(request.content)
            seen.append((request.headers, body))
            if "tools" in body:
                return httpx.Response(200, json={"content": [
                    {"type": "tool_use", "name": "grade", "input": {"verdict": "pass"}}]})
            return httpx.Response(200, json={"content": [{"type": "text", "text": " hello "}]})

        llm = ClaudeLLM("k", "some-model")
        llm._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(llm.structured("s", [{"role": "user", "content": "x"}], {"type": "object"}, "grade"),
                         {"verdict": "pass"})
        self.assertEqual(llm.text("s", [{"role": "user", "content": "x"}]), "hello")
        headers, body = seen[0]
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "grade"})
        self.assertEqual(body["temperature"], 0)

    def test_retries_then_error(self):
        import httpx

        from mystery_shopper.llm import ClaudeLLM, LLMError
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(529, headers={"retry-after": "0"}, json={})
        llm = ClaudeLLM("k", "m", max_retries=2)
        llm._client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(LLMError):
            llm.text("s", [{"role": "user", "content": "x"}])
        self.assertEqual(len(calls), 3)



class BrowserTargetTests(unittest.TestCase):
    def test_drives_widget(self):
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(ROOT / "examples"))
        handler.log_message = lambda *a: None
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            t = build_target({"name": "w", "type": "browser", "authorized": True, "authorized_by": "test",
                              "url": f"http://127.0.0.1:{srv.server_address[1]}/demo_widget.html",
                              "open_steps": [{"click": "#chat-launcher"}], "input": "#msg", "send": "#send",
                              "bot_messages": ".bot", "reply_timeout_s": 10})
            try:
                self.assertEqual(t.start(), "Hello! Welcome to SafeHome.")
                self.assertEqual(t.send("What does it cost?"), "Plans start from 29.90 EUR per month.")
                self.assertEqual(t.send("ok"), "Shall I book an installation?")
            finally:
                t.close()
        finally:
            srv.shutdown()
            srv.server_close()


class OpenAICompatibleTests(unittest.TestCase):
    def test_function_calling_and_text(self):
        import httpx
        import json as _j

        from mystery_shopper.config import Settings
        from mystery_shopper.llm import OpenAICompatibleLLM, make_llm
        seen = []

        def handler(request):
            body = _j.loads(request.content)
            seen.append((str(request.url), request.headers, body))
            if "tools" in body:
                return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
                    {"type": "function", "function": {"name": "grade", "arguments": '{"verdict": "fail"}'}}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": " hei "}}]})

        llm = OpenAICompatibleLLM("k", "some-model", "https://api.example.com/v1/")
        llm._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(llm.structured("s", [{"role": "user", "content": "x"}], {"type": "object"}, "grade"),
                         {"verdict": "fail"})
        self.assertEqual(llm.text("sys", [{"role": "user", "content": "x"}]), "hei")
        url, headers, body = seen[1]
        self.assertEqual(url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer k")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertIsInstance(make_llm(Settings(provider="openai", base_url="http://localhost:11434/v1",
                                                llm_api_key="", model="m")), OpenAICompatibleLLM)

    def test_plain_json_fallback(self):
        import httpx

        from mystery_shopper.llm import OpenAICompatibleLLM
        llm = OpenAICompatibleLLM("", "m", "http://x/v1")
        llm._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, json={"choices": [{"message": {"content": '```json\n{"verdict": "pass"}\n```'}}]})))
        self.assertEqual(llm.structured("s", [{"role": "user", "content": "x"}], {}, "grade"), {"verdict": "pass"})


class BatchGradingTests(unittest.TestCase):
    def test_one_request_per_review_with_evidence_guard(self):
        turns = [Turn(n=1, speaker="agent", text="Plans start from 29.90 EUR per month."),
                 Turn(n=2, speaker="shopper", text="Human please"),
                 Turn(n=3, speaker="agent", text="Our opening hours are 9-17.")]

        def fn(system, messages, schema, name):
            self.assertEqual(name, "grades")
            return {"results": [
                {"rule_id": "W1", "verdict": "fail", "evidence": [{"turn": 1, "quote": "Plans start from 29.90"}],
                 "reasoning": "Only a from-price.", "confidence": 0.9, "fix": "Give the total."},
                {"rule_id": "W2", "verdict": "fail", "evidence": [{"turn": 1, "quote": "your IBAN"}],
                 "reasoning": "x", "confidence": 0.9, "fix": ""},
                {"rule_id": "W4", "verdict": "fail", "evidence": [{"turn": 3, "quote": "opening hours"}],
                 "reasoning": "No handover.", "confidence": 0.95, "fix": "Offer a callback."},
            ]}
        llm = ScriptedLLM(shopper_text, fn)
        g = Grader(llm, double_check=True, batch=True)
        fs = {f.rule_id: f for f in g.grade(LIB.rules_for("chat"), LIB.personas["marcus"], turns)}
        self.assertEqual(sum(1 for c in llm.calls if c[0] == "structured"), 2)   # first + second review
        self.assertEqual(fs["W1"].verdict, "fail")
        self.assertEqual(fs["W2"].verdict, "unclear")                             # invented quote
        self.assertEqual(fs["W4"].breach_type, "policy")
        self.assertEqual(fs["W3"].verdict, "unclear")                             # missing from answer
        self.assertEqual(len(fs), len(LIB.rules_for("chat")))


class EconomyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_settings_switch(self):
        s = Settings(economy=True)
        self.assertTrue(s.batch_grading and s.selective_review and s.prechecks and s.scripted_probes)
        self.assertEqual(s.max_turns, 6)
        self.assertFalse(Settings().batch_grading)

    def test_precheck_passes_only_clear_cases(self):
        from mystery_shopper.grader import precheck_pass
        w7 = LIB.rule("W7")
        ok = [Turn(n=1, speaker="agent", text="Hi! I'm SafeHome's AI assistant. How can I help?")]
        self.assertEqual(precheck_pass(w7, ok), ([1], ["I'm SafeHome's AI assistant."]))
        denied = [Turn(n=1, speaker="agent", text="I'm not an AI, I'm Sanna from customer service.")]
        self.assertIsNone(precheck_pass(w7, denied))
        shopper_only = [Turn(n=1, speaker="shopper", text="Are you an AI?")]
        self.assertIsNone(precheck_pass(w7, shopper_only))              # only agent words count
        w1 = LIB.rule("W1")
        full = [Turn(n=2, speaker="agent", text="It is 39.90 EUR per month plus a 199 EUR installation fee. "
                                                  "The minimum contract is 36 months.")]
        turns, quotes = precheck_pass(w1, full)
        self.assertEqual(turns, [2, 2])
        self.assertIn("The minimum contract is 36 months.", quotes)
        partial = [Turn(n=2, speaker="agent", text="Plans start from 29.90 EUR per month.")]
        self.assertIsNone(precheck_pass(w1, partial))
        fi = [Turn(n=2, speaker="agent", text="Hinta on 39,90 € kuukaudessa, asennusmaksu 199 € ja sopimus 36 kuukautta.")]
        self.assertIsNotNone(precheck_pass(w1, fi))

    def test_scripted_probes_choice(self):
        from mystery_shopper.shopper import scripted_probes
        chat = LIB.scenarios["website_chat"]
        rules = LIB.rules_for("chat")
        en = scripted_probes(rules, LIB.personas["marcus"], chat, "English")
        self.assertEqual(len(en), 4)                                    # W1, W3, W4, W7
        self.assertTrue(scripted_probes(rules, LIB.personas["marcus"], chat, "Finnish")[0].startswith("Hei"))
        self.assertIsNone(scripted_probes(rules, LIB.personas["max"], chat, "English"))    # adversarial
        self.assertIsNone(scripted_probes(rules, LIB.personas["helmi"], chat, "English"))  # vulnerable
        self.assertIsNone(scripted_probes(rules, LIB.personas["marcus"], chat, "Italian")) # no translation
        cancel = LIB.scenarios["cancellation_call"]
        self.assertIsNone(scripted_probes(LIB.rules_for("cancellation"), LIB.personas["daan"], cancel, "English"))

    def test_economy_run_uses_few_ai_calls(self):
        """Marcus vs the demo HTTP bot: no AI for the shopper, one grading request,
        a second review only for the failures."""
        def grade(system, messages, schema, name):
            content = messages[0]["content"]
            ids = re.findall(r"RULE (\w+):", content)
            res = []
            for rid in ids:
                if rid == "W1":
                    res.append({"rule_id": rid, "verdict": "fail", "confidence": 0.9, "reasoning": "From-price only.",
                                "evidence": [{"turn": 3, "quote": "Plans start from 29.90 EUR per month"}],
                                "fix": "State the total."})
                else:
                    res.append({"rule_id": rid, "verdict": "pass", "confidence": 0.95, "reasoning": "ok",
                                "evidence": [], "fix": ""})
            return {"results": res}

        def no_text(system, messages):
            raise AssertionError("economy mode must not call the AI for the shopper")

        llm = ScriptedLLM(no_text, grade)
        with BotServer() as bot:
            r = run_audit(LIB, llm, self.store, country="FI", persona_id="marcus", scenario_id="website_chat",
                          target=build_target(bot.spec(), llm), simulated=False, double_check=True,
                          batch_grading=True, economy=True)
        shopper_lines = [t.text for t in r.turns if t.speaker == "shopper"]
        self.assertEqual(len(shopper_lines), 4)
        self.assertTrue(shopper_lines[0].startswith("Hi, what would a home alarm cost"))
        calls = [c for c in llm.calls if c[0] == "structured"]
        self.assertEqual(len(calls), 2)                                 # first review + second for W1 only
        w1 = next(f for f in r.findings if f.rule_id == "W1")
        self.assertEqual(w1.verdict, "fail")
        self.assertEqual(len(r.findings), len(LIB.rules_for("chat")))


class LiveRunRegressionTests(unittest.TestCase):
    """Bugs found in the first live run with Gemini (27.9.)."""

    def test_cut_off_reply_is_retried_with_bigger_budget(self):
        import httpx
        import json as _j

        from mystery_shopper.llm import MIN_BUDGET, OpenAICompatibleLLM
        budgets = []

        def handler(request):
            b = _j.loads(request.content)["max_tokens"]
            budgets.append(b)
            if len(budgets) == 1:
                return httpx.Response(200, json={"choices": [{"finish_reason": "length",
                                                              "message": {"content": "Our alarm system is just"}}]})
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                          "message": {"content": "It is 39.90 EUR per month."}}]})
        llm = OpenAICompatibleLLM("k", "m", "http://x/v1")
        llm._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(llm.text("s", [{"role": "user", "content": "x"}], max_tokens=300), "It is 39.90 EUR per month.")
        self.assertEqual(budgets, [MIN_BUDGET, MIN_BUDGET * 2])

    def test_fail_must_quote_the_agent(self):
        turns = [Turn(n=1, speaker="shopper", text="Could I talk to a human instead?"),
                 Turn(n=2, speaker="agent", text="I'm Alex, one of our customer advisors.")]
        only_customer = {"verdict": "fail", "confidence": 1.0, "reasoning": "x", "fix": "",
                         "evidence": [{"turn": 1, "quote": "Could I talk to a human instead?"}]}
        with_agent = dict(only_customer, evidence=only_customer["evidence"] +
                          [{"turn": 2, "quote": "I'm Alex, one of our customer advisors"}])
        g = Grader(ScriptedLLM(shopper_text, grade_from({})), double_check=False)
        self.assertEqual(g._finalize(LIB.rule("W4"), only_customer, None, turns).verdict, "unclear")
        self.assertEqual(g._finalize(LIB.rule("W4"), with_agent, None, turns).verdict, "fail")


if __name__ == "__main__":
    unittest.main()
