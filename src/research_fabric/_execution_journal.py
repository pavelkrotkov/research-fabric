"""Append-only attempt facts and atomic budget decisions, private to execution.

A start reserves a request and its maximum duration before network I/O. An
outcome transaction serializes known-usage acceptance across processes. A crash
leaves a visible start without an outcome; changing profiles then fails closed.
No source text, prompt, credential, or arbitrary provider error is stored here.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from research_fabric._execution_profiles import ExecutionError, _encode


@contextmanager
def _connect(root):
    path = Path(root) / "execution.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS revisions (id INTEGER PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS starts (id INTEGER PRIMARY KEY, revision INTEGER, role TEXT, task TEXT,
            source TEXT, profile TEXT, started REAL, timeout REAL);
        CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY, outcome TEXT, actual_model TEXT,
            usage TEXT, elapsed REAL);
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def configure(root, config):
    """Record an immutable configuration revision only between requests."""
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        last = db.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
        if db.execute("SELECT 1 FROM starts LEFT JOIN outcomes USING(id) WHERE outcome IS NULL").fetchone():
            raise ExecutionError("in_flight_or_interrupted_request")
        if last and last["config"] == _encode(config):
            return last["id"]
        return db.execute("INSERT INTO revisions(config) VALUES (?)", (_encode(config),)).lastrowid


def configured(root):
    with _connect(root) as db:
        row = db.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        raise ExecutionError("run_not_configured")
    return row["id"], json.loads(row["config"])


def _known_tokens(rows):
    return sum(json.loads(row["usage"]).get("total_tokens") or 0 for row in rows if row["usage"])


def _budget_outcome(db, budget, usage, elapsed, timeout, outcome):
    tokens = usage["total_tokens"] or 0
    used = _known_tokens(db.execute("SELECT usage FROM outcomes").fetchall())
    exceeded = (elapsed > timeout, tokens > budget["attempt_tokens"], used + tokens > budget["tokens"])
    return "budget_exhausted" if any(exceeded) else outcome


def _remaining(records, budget):
    used_time = sum(r["elapsed"] if r["outcome"] else r["timeout"] for r in records)
    used_tokens = _known_tokens(records)
    if len(records) >= budget["requests"] or used_time >= budget["seconds"] or used_tokens >= budget["tokens"]:
        raise ExecutionError("budget_exhausted")
    return min(budget["attempt_seconds"], budget["seconds"] - used_time)


def history(root):
    with _connect(root) as db:
        revisions = [
            {"revision": r["id"], "config": json.loads(r["config"])} for r in db.execute("SELECT * FROM revisions")
        ]
        rows = [dict(r) for r in db.execute("SELECT * FROM starts LEFT JOIN outcomes USING(id) ORDER BY id")]
    for row in rows:
        row["profile"] = json.loads(row["profile"])
        row["usage"] = json.loads(row["usage"]) if row["usage"] else None
    return {
        "revisions": revisions,
        "attempts": rows,
        "unknown_usage_calls": sum(not r["usage"] or r["usage"]["total_tokens"] is None for r in rows),
    }


def reserve(root, record, budget):
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute("SELECT max(id) FROM revisions").fetchone()[0]
        if latest != record[0]:
            raise ExecutionError("configuration_changed_at_checkpoint")
        records = db.execute("SELECT * FROM starts LEFT JOIN outcomes USING(id)").fetchall()
        timeout = _remaining(records, budget)
        attempt = db.execute(
            "INSERT INTO starts(revision,role,task,source,profile,started,timeout) VALUES (?,?,?,?,?,?,?)",
            (*record, time.time(), timeout),
        ).lastrowid
    return attempt, timeout


def complete(root, attempt, outcome, actual, usage, elapsed, budget):
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        timeout = db.execute("SELECT timeout FROM starts WHERE id=?", (attempt,)).fetchone()[0]
        outcome = _budget_outcome(db, budget, usage, elapsed, timeout, outcome)
        db.execute("INSERT INTO outcomes VALUES (?,?,?,?,?)", (attempt, outcome, actual, _encode(usage), elapsed))
    if outcome == "budget_exhausted":
        raise ExecutionError(outcome)
