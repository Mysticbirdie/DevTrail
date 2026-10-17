"""Database setup and operations for Cross-Tool Memory."""

import os
import sqlite3
import json
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import numpy as np

# Enable sqlite-vec
import sqlite_vec

DB_PATH = Path.home() / ".dev-memory" / "memory.db"


def _install_query_guard(conn: sqlite3.Connection, budget_s: float):
    """Abort work on this connection after ~budget_s wall-clock.

    Two mechanisms, because neither alone covers the failure modes:
    - progress handler (op-count check) catches high-op-count statements
      early — but LIKE over MB-size TEXT spends its wall time *inside* a
      single likeFunc VDBE op and can run far past deadline between ticks.
    - a threading.Timer calling conn.interrupt() — documented thread-safe —
      which aborts even a statement stuck inside one op. If no statement is
      running at deadline, interrupt() is a no-op.
    Both surface as OperationalError('interrupted') to the caller.
    """
    deadline = time.monotonic() + budget_s

    def _over_budget() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_over_budget, 50_000)

    def _fire():
        try:
            conn.interrupt()
        except Exception:
            pass

    timer = threading.Timer(budget_s, _fire)
    timer.daemon = True
    timer.start()


def init_db(db_path: Optional[Path] = None, query_budget_s: Optional[float] = None) -> sqlite3.Connection:
    """Initialize the database with all tables.

    query_budget_s: wall-clock ceiling for work on this connection; 0/None
    means unlimited. Falls back to env DEVTRAIL_QUERY_BUDGET_S when unset.
    Callers holding a connection for long batch work (extraction) should
    leave it unbounded; request-scoped readers should pass a budget.
    """
    if query_budget_s is None:
        query_budget_s = float(os.environ.get("DEVTRAIL_QUERY_BUDGET_S", "0"))
    db_path = db_path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if query_budget_s > 0:
        _install_query_guard(conn, query_budget_s)
    
    conn.row_factory = sqlite3.Row
    
    # Sessions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            tool TEXT NOT NULL,
            session_id TEXT NOT NULL UNIQUE,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            summary TEXT,
            tags TEXT,
            raw_content TEXT,
            importance INTEGER DEFAULT 3,
            workspace TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Turns table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_number INTEGER,
            role TEXT,
            content TEXT,
            tool_calls TEXT,
            timestamp TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    
    # Entities table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            type TEXT,
            first_seen TIMESTAMP,
            last_seen TIMESTAMP,
            mention_count INTEGER DEFAULT 1,
            context TEXT
        )
    """)
    
    # Entity links
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_links (
            entity_a TEXT NOT NULL,
            entity_b TEXT NOT NULL,
            strength REAL DEFAULT 1.0,
            context TEXT,
            PRIMARY KEY (entity_a, entity_b),
            FOREIGN KEY (entity_a) REFERENCES entities(name),
            FOREIGN KEY (entity_b) REFERENCES entities(name)
        )
    """)
    
    # Decisions table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            title TEXT NOT NULL,
            context TEXT,
            decision TEXT,
            rationale TEXT,
            impact TEXT,
            status TEXT DEFAULT 'active',
            decided_at TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    
    # Patterns table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            pattern_type TEXT,
            description TEXT,
            code_example TEXT,
            related_files TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    
    # File activity
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_activity (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            file_path TEXT,
            activity_type TEXT,
            lines_changed INTEGER,
            timestamp TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id)
        )
    """)
    
    # Embeddings virtual table (sqlite-vec)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS session_embeddings USING vec0(
            session_id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        )
    """)
    
    # Indexes for performance
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_activity_path ON file_activity(file_path)")

    # ── Schema migrations (safe for existing DBs) ─────────────────────────
    _migrate_sessions(conn)

    conn.commit()
    return conn


def _migrate_sessions(conn: sqlite3.Connection):
    """Add columns that may be missing from older DBs."""
    cursor = conn.execute("PRAGMA table_info(sessions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "importance" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN importance INTEGER DEFAULT 3")
    if "workspace" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT")


def insert_session(conn: sqlite3.Connection, session_data: Dict) -> str:
    """Insert a session and return its ID."""
    session_id = session_data.get("session_id", f"{session_data['tool']}_{datetime.now().isoformat()}")
    
    conn.execute("""
        INSERT OR REPLACE INTO sessions (tool, session_id, started_at, ended_at, summary, tags, raw_content, importance, workspace)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_data.get("tool"),
        session_id,
        session_data.get("started_at"),
        session_data.get("ended_at"),
        session_data.get("summary"),
        json.dumps(session_data.get("tags", [])) if isinstance(session_data.get("tags"), list) else session_data.get("tags"),
        session_data.get("raw_content"),
        session_data.get("importance", 3),
        session_data.get("workspace")
    ))
    
    # Insert turns
    for i, turn in enumerate(session_data.get("turns", [])):
        conn.execute("""
            INSERT OR REPLACE INTO turns (session_id, turn_number, role, content, tool_calls, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            i,
            turn.get("role"),
            turn.get("content"),
            json.dumps(turn.get("tool_calls")) if turn.get("tool_calls") else None,
            turn.get("timestamp")
        ))
    
    conn.commit()

    # Stella write-through: mirror the session into the local topology
    # store. Best-effort — a sidecar failure must never break canonical
    # SQLite writes.
    try:
        try:
            from .stella_store import mirror_session
        except ImportError:
            from memory.stella_store import mirror_session
        mirror_session(session_id, session_data)
    except Exception:
        pass

    return session_id


def insert_entity(conn: sqlite3.Connection, name: str, entity_type: str, context: str = ""):
    """Insert or update an entity."""
    now = datetime.now().isoformat()
    
    # Try to update existing
    cursor = conn.execute(
        "UPDATE entities SET last_seen = ?, mention_count = mention_count + 1 WHERE name = ?",
        (now, name)
    )
    
    if cursor.rowcount == 0:
        # Insert new
        conn.execute("""
            INSERT INTO entities (name, type, first_seen, last_seen, mention_count, context)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (name, entity_type, now, now, context))
    
    conn.commit()


def insert_decision(conn: sqlite3.Connection, decision: Dict) -> int:
    """Insert a decision. Returns the ID."""
    cursor = conn.execute("""
        INSERT INTO decisions (session_id, title, context, decision, rationale, impact, status, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        decision.get("session_id"),
        decision.get("title"),
        decision.get("context"),
        decision.get("decision"),
        decision.get("rationale"),
        decision.get("impact"),
        decision.get("status", "active"),
        decision.get("decided_at", datetime.now().isoformat())
    ))
    conn.commit()
    return cursor.lastrowid


def insert_pattern(conn: sqlite3.Connection, pattern: Dict) -> int:
    """Insert a pattern. Returns the ID."""
    cursor = conn.execute("""
        INSERT INTO patterns (session_id, pattern_type, description, code_example, related_files)
        VALUES (?, ?, ?, ?, ?)
    """, (
        pattern.get("session_id"),
        pattern.get("pattern_type"),
        pattern.get("description"),
        pattern.get("code_example"),
        json.dumps(pattern.get("related_files", []))
    ))
    conn.commit()
    return cursor.lastrowid


def insert_file_activity(conn: sqlite3.Connection, session_id: str, file_path: str,
                         activity_type: str = "referenced", lines_changed: int = 0,
                         timestamp: Optional[str] = None):
    """Record that a file was touched/referenced in a session."""
    conn.execute("""
        INSERT INTO file_activity (session_id, file_path, activity_type, lines_changed, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, file_path, activity_type, lines_changed, timestamp or datetime.now().isoformat()))
    conn.commit()


def insert_entity_links(conn: sqlite3.Connection, entities: List[Dict]):
    """Build co-occurrence links among a session's entities.

    Entities mentioned together are linked; repeated co-occurrence accumulates
    strength. Pairs are order-normalized so (a,b) and (b,a) share one row.
    """
    names = [e["name"] for e in entities]
    pairs: Dict[Tuple[str, str], float] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a == b:
                continue
            key = tuple(sorted((a, b)))
            pairs[key] = pairs.get(key, 0.0) + 1.0
    if not pairs:
        return
    conn.executemany("""
        INSERT INTO entity_links (entity_a, entity_b, strength)
        VALUES (?, ?, ?)
        ON CONFLICT(entity_a, entity_b) DO UPDATE SET strength = strength + excluded.strength
    """, [(a, b, s) for (a, b), s in pairs.items()])
    conn.commit()


def _fetch_sessions_by_ids(conn: sqlite3.Connection, session_ids: List[str],
                           workspace: Optional[str] = None,
                           min_importance: Optional[int] = None) -> List[Dict]:
    """Fetch full session rows for a list of session_ids, with the same
    optional filters used by scoped search."""
    if not session_ids:
        return []
    sql = f"SELECT * FROM sessions WHERE session_id IN ({','.join('?' * len(session_ids))})"
    params: List = list(session_ids)
    if workspace:
        sql += " AND workspace = ?"
        params.append(workspace)
    if min_importance is not None:
        sql += " AND importance >= ?"
        params.append(min_importance)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def search_sessions(conn: sqlite3.Connection, query: str, limit: int = 10) -> List[Dict]:
    """Full-text search across sessions.

    Two-phase: scan the small sessions table first, then only if needed a
    bounded DISTINCT-session_id scan over turns. The previous single-query
    form (sessions LEFT JOIN turns + OR'ed LIKEs + GROUP BY/ORDER BY) forced
    SQLite to materialize and sort every joined row before LIMIT could
    apply — a full byte-position LIKE scan over the entire turns table
    (~hours on a multi-GB DB). The join's turn_content was never consumed.
    """
    search_term = f"%{query}%"

    cursor = conn.execute("""
        SELECT * FROM sessions
        WHERE summary LIKE ? OR raw_content LIKE ?
        ORDER BY started_at DESC
        LIMIT ?
    """, (search_term, search_term, limit))
    hits = [dict(row) for row in cursor.fetchall()]

    if len(hits) < limit:
        seen = {h["session_id"] for h in hits}
        cursor = conn.execute(
            "SELECT DISTINCT session_id FROM turns WHERE content LIKE ? LIMIT ?",
            (search_term, limit))
        extra = [r[0] for r in cursor.fetchall() if r[0] not in seen]
        if extra:
            hits.extend(_fetch_sessions_by_ids(conn, extra))
            hits.sort(key=lambda r: r.get("started_at") or "", reverse=True)

    return hits[:limit]


def get_recent_sessions(
    conn: sqlite3.Connection,
    days: int = 7,
    tool: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict]:
    """Get recent sessions, optionally capped to `limit` results."""
    query = """
        SELECT * FROM sessions
        WHERE started_at >= datetime('now', '-{} days')
    """.format(days)

    params = []
    if tool:
        query += " AND tool = ?"
        params.append(tool)

    query += " ORDER BY started_at DESC"

    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def get_entities(conn: sqlite3.Connection, entity_type: Optional[str] = None, min_mentions: int = 1) -> List[Dict]:
    """Get entities, optionally filtered."""
    query = "SELECT * FROM entities WHERE mention_count >= ?"
    params = [min_mentions]
    
    if entity_type:
        query += " AND type = ?"
        params.append(entity_type)
    
    query += " ORDER BY mention_count DESC"
    
    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def get_active_decisions(conn: sqlite3.Connection) -> List[Dict]:
    """Get all active architectural decisions."""
    cursor = conn.execute("""
        SELECT * FROM decisions
        WHERE status = 'active'
        ORDER BY decided_at DESC
    """)
    return [dict(row) for row in cursor.fetchall()]


def get_entity_graph(conn: sqlite3.Connection, entity_name: str, depth: int = 1) -> Dict:
    """Get entity and its connected neighbors."""
    # Get the entity
    cursor = conn.execute("SELECT * FROM entities WHERE name = ?", (entity_name,))
    row = cursor.fetchone()
    entity = dict(row) if row else None

    if not entity:
        return {}
    
    # Get direct connections
    cursor = conn.execute("""
        SELECT e.*, el.strength
        FROM entities e
        JOIN entity_links el ON (e.name = el.entity_a AND el.entity_b = ?)
            OR (e.name = el.entity_b AND el.entity_a = ?)
        ORDER BY el.strength DESC
    """, (entity_name, entity_name))
    
    neighbors = [dict(row) for row in cursor.fetchall()]
    
    return {
        "entity": entity,
        "neighbors": neighbors,
        "depth": depth
    }


# Simple embedding for now (random for placeholder; replace with real model)
def generate_embedding(text: str) -> bytes:
    """Generate a 384-dim embedding. Placeholder - replace with real model."""
    # For now, use a hash-based deterministic vector
    # In production: use sentence-transformers or similar
    import hashlib
    np.random.seed(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    vec = np.random.randn(384).astype(np.float32)
    vec = vec / np.linalg.norm(vec)  # normalize
    return vec.tobytes()


def insert_embedding(conn: sqlite3.Connection, session_id: str, text: str):
    """Store embedding for a session."""
    embedding = generate_embedding(text)
    conn.execute(
        "INSERT OR REPLACE INTO session_embeddings (session_id, embedding) VALUES (?, ?)",
        (session_id, embedding)
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection) -> Dict:
    """Get database statistics."""
    stats = {}

    for table in ["sessions", "turns", "entities", "decisions", "patterns", "file_activity"]:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        stats[table] = cursor.fetchone()[0]

    # Tool breakdown
    cursor = conn.execute("SELECT tool, COUNT(*) FROM sessions GROUP BY tool")
    stats["tools"] = {row[0]: row[1] for row in cursor.fetchall()}

    # Importance distribution
    cursor = conn.execute("SELECT importance, COUNT(*) FROM sessions GROUP BY importance")
    stats["importance_distribution"] = {row[0]: row[1] for row in cursor.fetchall()}

    # Workspace breakdown
    cursor = conn.execute("SELECT workspace, COUNT(*) FROM sessions WHERE workspace IS NOT NULL GROUP BY workspace")
    stats["workspaces"] = {row[0]: row[1] for row in cursor.fetchall()}

    return stats


def get_sessions_by_importance(conn: sqlite3.Connection, min_importance: int = 3, limit: int = 20) -> List[Dict]:
    """Get sessions at or above a given importance threshold."""
    cursor = conn.execute("""
        SELECT * FROM sessions
        WHERE importance >= ?
        ORDER BY importance DESC, started_at DESC
        LIMIT ?
    """, (min_importance, limit))
    return [dict(row) for row in cursor.fetchall()]


def get_sessions_by_workspace(conn: sqlite3.Connection, workspace: str, limit: int = 20) -> List[Dict]:
    """Get sessions for a specific workspace/department."""
    cursor = conn.execute("""
        SELECT * FROM sessions
        WHERE workspace = ?
        ORDER BY started_at DESC
        LIMIT ?
    """, (workspace, limit))
    return [dict(row) for row in cursor.fetchall()]


def search_sessions_scoped(conn: sqlite3.Connection, query: str, workspace: Optional[str] = None,
                           min_importance: Optional[int] = None, limit: int = 10) -> List[Dict]:
    """Search with workspace and importance filters. Same two-phase shape
    as search_sessions — see its docstring for why the join was removed."""
    search_term = f"%{query}%"
    sql = """
        SELECT * FROM sessions
        WHERE (summary LIKE ? OR raw_content LIKE ?)
    """
    params = [search_term, search_term]

    if workspace:
        sql += " AND workspace = ?"
        params.append(workspace)

    if min_importance is not None:
        sql += " AND importance >= ?"
        params.append(min_importance)

    sql += " ORDER BY importance DESC, started_at DESC LIMIT ?"
    params.append(limit)

    hits = [dict(row) for row in conn.execute(sql, params).fetchall()]

    if len(hits) < limit:
        seen = {h["session_id"] for h in hits}
        cursor = conn.execute(
            "SELECT DISTINCT session_id FROM turns WHERE content LIKE ? LIMIT ?",
            (search_term, limit))
        extra = [r[0] for r in cursor.fetchall() if r[0] not in seen]
        if extra:
            hits.extend(_fetch_sessions_by_ids(conn, extra, workspace=workspace,
                                             min_importance=min_importance))
            hits.sort(key=lambda r: (r.get("importance") or 0, r.get("started_at") or ""),
                      reverse=True)

    return hits[:limit]
