"""
SQLite-backed event store for the jira-bitbucket-worker dashboard.
Thread-safe — every public function opens its own connection.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.db")

# -- Schema -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    issue_key   TEXT PRIMARY KEY,
    summary     TEXT,
    priority    TEXT,
    issue_type  TEXT,
    components  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    queued_at   REAL,
    started_at  REAL,
    finished_at REAL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    issue_key  TEXT,
    event_type TEXT    NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key    TEXT NOT NULL,
    repo_slug    TEXT,
    workspace    TEXT,
    branch       TEXT,
    dest_branch  TEXT,
    pr_url       TEXT,
    pr_id        TEXT,
    status       TEXT DEFAULT 'open',
    created_at   REAL
);

CREATE TABLE IF NOT EXISTS ticket_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_key  TEXT NOT NULL,
    ts         REAL NOT NULL,
    stream     TEXT NOT NULL DEFAULT 'stdout',
    line       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_health (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_received   REAL,
    total_received  INTEGER DEFAULT 0,
    sig_failures    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

INSERT OR IGNORE INTO webhook_health (id, last_received, total_received, sig_failures)
VALUES (1, NULL, 0, 0);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# -- SSE subscribers ----------------------------------------------------------

_subscribers: list = []


def subscribe():
    """Return a new list that will receive SSE event dicts."""
    q: list = []
    _subscribers.append(q)
    return q


def unsubscribe(q):
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def _notify(event_type: str, data: dict):
    msg = {"event": event_type, "data": data}
    for q in list(_subscribers):
        q.append(msg)


# -- Ticket lifecycle ---------------------------------------------------------

def ticket_queued(issue_key: str):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tickets (issue_key, status, queued_at) VALUES (?, 'queued', ?)",
            (issue_key, now),
        )
    _log_event(issue_key, "queued", f"Ticket {issue_key} added to queue")
    _notify("ticket_update", {"issue_key": issue_key, "status": "queued"})


def ticket_started(issue_key: str, summary: str = "", priority: str = "",
                   issue_type: str = "", components: str = ""):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """UPDATE tickets
               SET status='processing', started_at=?, summary=?, priority=?,
                   issue_type=?, components=?
               WHERE issue_key=?""",
            (now, summary, priority, issue_type, components, issue_key),
        )
    _log_event(issue_key, "started", f"Processing started for {issue_key}: {summary}")
    _notify("ticket_update", {"issue_key": issue_key, "status": "processing"})


def ticket_phase(issue_key: str, phase: str, detail: str = ""):
    with _connect() as conn:
        conn.execute("UPDATE tickets SET status=? WHERE issue_key=?", (phase, issue_key))
    _log_event(issue_key, phase, detail or f"{issue_key} entered phase: {phase}")
    _notify("ticket_update", {"issue_key": issue_key, "status": phase})


def ticket_cancelled(issue_key: str):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status='cancelled', finished_at=? WHERE issue_key=?",
            (now, issue_key),
        )
    _log_event(issue_key, "cancelled", f"{issue_key} was cancelled by user")
    _notify("ticket_update", {"issue_key": issue_key, "status": "cancelled"})


def ticket_finished(issue_key: str, error: str | None = None):
    now = time.time()
    status = "failed" if error else "done"
    with _connect() as conn:
        conn.execute(
            "UPDATE tickets SET status=?, finished_at=?, error=? WHERE issue_key=?",
            (status, now, error, issue_key),
        )
    detail = f"{issue_key} finished ({status})" + (f": {error}" if error else "")
    _log_event(issue_key, status, detail)
    _notify("ticket_update", {"issue_key": issue_key, "status": status})


# -- Pull requests ------------------------------------------------------------

def pr_created(issue_key: str, repo_slug: str, workspace: str,
               branch: str, dest_branch: str, pr_url: str, pr_id: str = ""):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO pull_requests
               (issue_key, repo_slug, workspace, branch, dest_branch, pr_url, pr_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (issue_key, repo_slug, workspace, branch, dest_branch, pr_url, pr_id, now),
        )
    _log_event(issue_key, "pr_created", f"PR created: {pr_url}")
    _notify("pr_created", {"issue_key": issue_key, "pr_url": pr_url, "repo_slug": repo_slug})


# -- Events / activity log ---------------------------------------------------

def _log_event(issue_key: str | None, event_type: str, detail: str = ""):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events (ts, issue_key, event_type, detail) VALUES (?, ?, ?, ?)",
            (now, issue_key, event_type, detail),
        )


def log_event(issue_key: str | None, event_type: str, detail: str = ""):
    _log_event(issue_key, event_type, detail)
    _notify("event", {"issue_key": issue_key, "event_type": event_type, "detail": detail})


# -- Ticket logs --------------------------------------------------------------

def log_line(issue_key: str, line: str, stream: str = "stdout"):
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO ticket_logs (issue_key, ts, stream, line) VALUES (?, ?, ?, ?)",
            (issue_key, now, stream, line),
        )
    _notify("log_line", {"issue_key": issue_key, "ts": now, "stream": stream, "line": line})


def get_ticket_logs(issue_key: str, since_id: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ticket_logs WHERE issue_key=? AND id>? ORDER BY id ASC",
            (issue_key, since_id),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def clear_ticket_logs(issue_key: str):
    with _connect() as conn:
        conn.execute("DELETE FROM ticket_logs WHERE issue_key=?", (issue_key,))


# -- Webhook health -----------------------------------------------------------

def webhook_received():
    with _connect() as conn:
        conn.execute(
            "UPDATE webhook_health SET last_received=?, total_received=total_received+1 WHERE id=1",
            (time.time(),),
        )


def webhook_sig_failure():
    with _connect() as conn:
        conn.execute(
            "UPDATE webhook_health SET sig_failures=sig_failures+1 WHERE id=1",
        )


# -- Queries ------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


def get_worker_status() -> dict:
    """Return current processing state."""
    with _connect() as conn:
        processing = conn.execute(
            "SELECT * FROM tickets WHERE status NOT IN ('queued','done','failed','cancelled') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        queue_size = conn.execute(
            "SELECT COUNT(*) as cnt FROM tickets WHERE status='queued'"
        ).fetchone()["cnt"]
    return {
        "current_ticket": _row_to_dict(processing) if processing else None,
        "queue_size": queue_size,
        "state": "busy" if processing else "idle",
    }


def get_queue() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status='queued' ORDER BY queued_at ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_recent_tickets(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY COALESCE(started_at, queued_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_recent_events(limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pull_requests(issue_key: str | None = None, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        if issue_key:
            rows = conn.execute(
                "SELECT * FROM pull_requests WHERE issue_key=? ORDER BY created_at DESC LIMIT ?",
                (issue_key, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pull_requests ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_errors(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status='failed' ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_webhook_health() -> dict:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM webhook_health WHERE id=1").fetchone()
    return _row_to_dict(row)


def get_stats() -> dict:
    with _connect() as conn:
        now = time.time()
        day_ago = now - 86400
        week_ago = now - 86400 * 7

        total = conn.execute("SELECT COUNT(*) as c FROM tickets").fetchone()["c"]
        today = conn.execute(
            "SELECT COUNT(*) as c FROM tickets WHERE COALESCE(started_at, queued_at) > ?",
            (day_ago,),
        ).fetchone()["c"]
        this_week = conn.execute(
            "SELECT COUNT(*) as c FROM tickets WHERE COALESCE(started_at, queued_at) > ?",
            (week_ago,),
        ).fetchone()["c"]

        done = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status='done'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) as c FROM tickets WHERE status='failed'").fetchone()["c"]
        success_rate = (done / (done + failed) * 100) if (done + failed) > 0 else 0

        avg_row = conn.execute(
            "SELECT AVG(finished_at - started_at) as avg_dur FROM tickets WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
        ).fetchone()
        avg_duration = avg_row["avg_dur"] if avg_row and avg_row["avg_dur"] else 0

        total_prs = conn.execute("SELECT COUNT(*) as c FROM pull_requests").fetchone()["c"]

    return {
        "total_tickets": total,
        "tickets_today": today,
        "tickets_this_week": this_week,
        "success_rate": round(success_rate, 1),
        "avg_duration_seconds": round(avg_duration, 1),
        "total_prs": total_prs,
        "done": done,
        "failed": failed,
    }


# -- Settings -----------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def get_all_settings() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# Initialize on import
init_db()
