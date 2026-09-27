"""SQLite storage: runs, findings and an append-only log of lawyer decisions."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from .models import AuditResult, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, country TEXT, persona_id TEXT, scenario_id TEXT, channel TEXT,
  target_name TEXT, team TEXT, simulated INTEGER, started_at TEXT, finished_at TEXT,
  score REAL, coverage REAL, critical_failures INTEGER, model TEXT, rulebook_version TEXT,
  transcript TEXT, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id), rule_id TEXT,
  verdict TEXT, critical INTEGER, breach_type TEXT, kind TEXT, question TEXT, reasoning TEXT,
  evidence TEXT, confidence REAL, basis TEXT, consequence TEXT, fix TEXT, needs_lawyer INTEGER,
  guard_notes TEXT, lawyer_status TEXT DEFAULT 'open', escalated INTEGER DEFAULT 0,
  fix_status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id INTEGER REFERENCES findings(id),
  action TEXT, by_whom TEXT, note TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS ix_findings_run ON findings(run_id);
"""

ACTIONS = {"confirm", "overturn", "escalate", "note", "fixed", "verified"}


class Store:
    def __init__(self, path: str = "mystery_shopper.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ writes
    def start_run(self, run_id: str, country: str, persona_id: str, scenario_id: str, channel: str,
                  target_name: str, team: Optional[str], simulated: bool) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO runs (id,country,persona_id,scenario_id,channel,target_name,team,simulated,"
                "started_at,status) VALUES (?,?,?,?,?,?,?,?,?, 'running')",
                (run_id, country, persona_id, scenario_id, channel, target_name, team, int(simulated), now_iso()))
            self.conn.commit()

    def fail_run(self, run_id: str, error: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE runs SET status='error', error=?, finished_at=? WHERE id=?",
                              (error[:2000], now_iso(), run_id))
            self.conn.commit()

    def save_result(self, r: AuditResult) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, score=?, coverage=?, critical_failures=?, model=?,"
                " rulebook_version=?, transcript=?, status='done' WHERE id=?",
                (r.finished_at, r.score, r.coverage, r.critical_failures, r.model, r.rulebook_version,
                 json.dumps([t.model_dump() for t in r.turns], ensure_ascii=False), r.run_id))
            for f in r.findings:
                self.conn.execute(
                    "INSERT INTO findings (run_id,rule_id,verdict,critical,breach_type,kind,question,reasoning,"
                    "evidence,confidence,basis,consequence,fix,needs_lawyer,guard_notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r.run_id, f.rule_id, f.verdict, int(f.critical), f.breach_type, f.kind, f.question,
                     f.reasoning,
                     json.dumps([{"turn": t, "quote": q} for t, q in zip(f.evidence_turns, f.evidence_quotes)],
                                ensure_ascii=False),
                     f.confidence, json.dumps([b.model_dump() for b in f.basis], ensure_ascii=False),
                     f.consequence, f.fix, int(f.needs_lawyer), json.dumps(f.guard_notes, ensure_ascii=False)))
            self.conn.commit()

    def decide(self, finding_id: int, action: str, by_whom: str, note: str = "") -> dict:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {sorted(ACTIONS)}")
        if not by_whom:
            raise ValueError("by_whom is required: every decision is attributable")
        with self.lock:
            row = self.conn.execute("SELECT id FROM findings WHERE id=?", (finding_id,)).fetchone()
            if not row:
                raise KeyError(finding_id)
            self.conn.execute("INSERT INTO decisions (finding_id,action,by_whom,note,at) VALUES (?,?,?,?,?)",
                              (finding_id, action, by_whom, note, now_iso()))
            if action == "confirm":
                self.conn.execute("UPDATE findings SET lawyer_status='confirmed' WHERE id=?", (finding_id,))
            elif action == "overturn":
                self.conn.execute("UPDATE findings SET lawyer_status='overturned' WHERE id=?", (finding_id,))
            elif action == "escalate":
                self.conn.execute("UPDATE findings SET escalated=1 WHERE id=?", (finding_id,))
            elif action in ("fixed", "verified"):
                self.conn.execute("UPDATE findings SET fix_status=? WHERE id=?", (action, finding_id))
            self.conn.commit()
        return self.finding(finding_id)

    def purge_transcripts(self, older_than_iso: str) -> int:
        """Data retention: drop full transcripts, keep findings with their quoted evidence."""
        with self.lock:
            cur = self.conn.execute("UPDATE runs SET transcript=NULL WHERE finished_at < ? AND transcript IS NOT NULL",
                                    (older_than_iso,))
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------- reads
    @staticmethod
    def _finding_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("evidence", "basis", "guard_notes"):
            d[k] = json.loads(d[k] or "[]")
        d["critical"] = bool(d["critical"])
        d["needs_lawyer"] = bool(d["needs_lawyer"])
        d["escalated"] = bool(d["escalated"])
        return d

    def finding(self, finding_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not row:
            raise KeyError(finding_id)
        d = self._finding_row(row)
        d["decisions"] = [dict(x) for x in self.conn.execute(
            "SELECT action,by_whom,note,at FROM decisions WHERE finding_id=? ORDER BY id", (finding_id,))]
        return d

    def runs(self, country: Optional[str] = None, limit: int = 200) -> list[dict]:
        q = ("SELECT id,country,persona_id,scenario_id,channel,target_name,team,simulated,started_at,"
             "finished_at,score,coverage,critical_failures,status FROM runs")
        args: tuple = ()
        if country:
            q += " WHERE country=?"
            args = (country,)
        q += " ORDER BY started_at DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, args + (limit,))]

    def run(self, run_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        d = dict(row)
        d["transcript"] = json.loads(d["transcript"]) if d["transcript"] else None
        d["findings"] = [self._finding_row(r) for r in
                         self.conn.execute("SELECT * FROM findings WHERE run_id=? ORDER BY id", (run_id,))]
        return d

    def all_findings(self, include_simulated: bool = False) -> list[dict]:
        q = ("SELECT f.*, r.country, r.channel, r.persona_id, r.team, r.simulated FROM findings f "
             "JOIN runs r ON r.id=f.run_id WHERE r.status='done'")
        if not include_simulated:
            q += " AND r.simulated=0"
        return [self._finding_row(r) | {"simulated": bool(r["simulated"])} for r in self.conn.execute(q)]
