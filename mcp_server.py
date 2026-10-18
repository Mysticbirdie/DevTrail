#!/usr/bin/env python3
"""DevTrail MCP Server — Auto-extracting memory layer for AI coding tools.

Serves DevTrail's core capabilities as MCP tools over stdio, with automatic
background extraction on startup so your memory is always fresh.

To use:
    Add to your IDE's MCP config:
    {
      "devtrail": {
        "command": "python3",
        "args": ["/path/to/DevTrail/mcp_server.py"]
      }
    }
"""

import json
import sys
import os
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Ensure DevTrail modules are importable
SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from memory.db import (
    init_db, search_sessions, search_sessions_scoped, get_recent_sessions, get_entities,
    get_active_decisions, get_stats, get_sessions_by_importance, get_sessions_by_workspace,
    pin_survivor, get_valid_survivors, DB_PATH
)
from memory.extractors import (
    DevinLocalExtractor, CursorExtractor, VSCodeCopilotExtractor,
    AiderExtractor, ClaudeCLIExtractor, GitExtractor, OllamaExtractor
)
from memory.intelligence import EntityExtractor, SessionSummarizer, ImportanceScorer, TOONCompactState, WorkspaceInferrer
from memory.bridge import sync_all


# ── Configuration ──────────────────────────────────────────────────────────
AUTO_EXTRACT_INTERVAL_HOURS = 24  # Auto-extract if DB is older than this
AUTO_EXTRACT_ON_STARTUP = True    # Run background extraction when server starts
BACKGROUND_EXTRACT_MIN_AGE = 5    # Minutes: don't re-extract if done this recently
# Wall-clock ceiling for any single tool request's DB work. The serving loop
# is single-threaded — one pathological query (leading-wildcard LIKE over a
# multi-GB turns table) would otherwise wedge every subsequent MCP call.
QUERY_BUDGET_S = float(os.environ.get("DEVTRAIL_QUERY_BUDGET_S", "15"))

# Context-pressure reflex (advisory only — never gates behavior).
# CONTEXT_WINDOW_BYTES: assumed live-context capacity in bytes (~4 chars/token
# for English). The live-session probe reads the Devin sessions.db; a read-only
# WAL snapshot can lag the newest un-checkpointed turns, and under concurrent
# windows it may measure the wrong session — the served-byte counter is the
# floor via max(). Tune with DEVTRAIL_CONTEXT_WINDOW_BYTES / _WARN_PCT.
CONTEXT_WINDOW_BYTES = int(os.environ.get("DEVTRAIL_CONTEXT_WINDOW_BYTES", "4000000"))
CONTEXT_WARN_PCT = float(os.environ.get("DEVTRAIL_CONTEXT_WARN_PCT", "0.75"))
DEVIN_SESSIONS_DB = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"
ORIENT_MAX_BYTES = 2048

# ── MCP Protocol Helpers ────────────────────────────────────────────────────

def _send(msg: Dict):
    """Send a JSON-RPC message to stdout."""
    payload = json.dumps(msg, default=str)
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def _read() -> Optional[Dict]:
    """Read a single JSON-RPC message from stdin."""
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _make_response(request_id: Any, result: Any) -> Dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error(request_id: Any, code: int, message: str) -> Dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# ── Tool Implementations ────────────────────────────────────────────────────

class DevTrailMCPServer:
    """MCP server wrapping DevTrail's memory layer."""

    def __init__(self):
        self._last_extract_time: Optional[datetime] = None
        self._extract_lock = threading.Lock()
        self._db_path = DB_PATH
        self._sessions_db = DEVIN_SESSIONS_DB
        self._ctx_bytes = 0  # cumulative tool args+result bytes this process

    def _read_db(self):
        """Request-scoped DB connection for tool handlers — bounded so a
        runaway query errors out instead of hanging the serving loop."""
        return init_db(query_budget_s=QUERY_BUDGET_S)

    def _ensure_db(self):
        """Initialize DB if needed."""
        if not self._db_path.exists():
            conn = self._read_db()
            conn.close()

    def _run_auto_extract(self):
        """Background thread: extract from all available sources."""
        with self._extract_lock:
            # Check if we already extracted recently
            if self._last_extract_time:
                age = (datetime.now() - self._last_extract_time).total_seconds() / 60
                if age < BACKGROUND_EXTRACT_MIN_AGE:
                    return

            try:
                conn = init_db()  # long-lived extraction conn — no query budget
                extractor = EntityExtractor()
                summarizer = SessionSummarizer()
                importance_scorer = ImportanceScorer()
                workspace_inferrer = WorkspaceInferrer()
                total = 0

                for ExtractorClass in [
                    DevinLocalExtractor, CursorExtractor, VSCodeCopilotExtractor,
                    AiderExtractor, ClaudeCLIExtractor, GitExtractor, OllamaExtractor
                ]:
                    try:
                        ext = ExtractorClass()
                        if ext.is_available():
                            sessions = ext.extract(limit=100)
                            for session in sessions:
                                intel = extractor.extract_from_session(session)
                                session["tags"] = intel["tags"]
                                session["summary"] = summarizer.summarize(session)
                                session["importance"] = importance_scorer.score(session, intel)
                                session["workspace"] = workspace_inferrer.infer(session)
                                from memory.db import (
                                    insert_session, insert_entity, insert_decision,
                                    insert_pattern, insert_file_activity, insert_entity_links
                                )
                                sid = insert_session(conn, session)
                                for ent in intel["entities"]:
                                    insert_entity(conn, ent["name"], ent["type"], ent.get("context", ""))
                                for dec in intel["decisions"]:
                                    dec["session_id"] = sid
                                    insert_decision(conn, dec)
                                for pat in intel["patterns"]:
                                    insert_pattern(conn, {
                                        "session_id": sid,
                                        "pattern_type": pat.get("type"),
                                        "description": pat.get("description"),
                                        "code_example": pat.get("code_example"),
                                        "related_files": pat.get("related_files", []),
                                    })
                                for fpath in intel["files"]:
                                    insert_file_activity(conn, sid, fpath)
                                insert_entity_links(conn, intel["entities"])
                                total += 1
                    except Exception:
                        pass  # Silently skip failing extractors

                conn.close()
                self._last_extract_time = datetime.now()
            except Exception:
                pass  # Fail silently in background

    def auto_extract(self):
        """Trigger background extraction if conditions met."""
        should_extract = False

        if not self._db_path.exists():
            should_extract = True
        else:
            # Check last session timestamp in DB
            try:
                import sqlite3
                conn = sqlite3.connect(str(self._db_path))
                cursor = conn.execute(
                    "SELECT MAX(started_at) FROM sessions"
                )
                row = cursor.fetchone()
                conn.close()
                if row and row[0]:
                    last_session = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                    if (datetime.now() - last_session) > timedelta(hours=AUTO_EXTRACT_INTERVAL_HOURS):
                        should_extract = True
                else:
                    should_extract = True
            except Exception:
                should_extract = True

        if should_extract and AUTO_EXTRACT_ON_STARTUP:
            thread = threading.Thread(target=self._run_auto_extract, daemon=True)
            thread.start()

    # ── Tool handlers ─────────────────────────────────────────────────────

    def tool_search(self, args: Dict) -> Dict:
        """Search across all stored sessions, decisions, and patterns."""
        query = args.get("query", "")
        limit = args.get("limit", 20)
        workspace = args.get("workspace")
        min_importance = args.get("min_importance")
        if not query:
            return {"results": [], "message": "No query provided"}

        conn = self._read_db()
        if workspace or min_importance is not None:
            results = search_sessions_scoped(conn, query, workspace=workspace, min_importance=min_importance, limit=limit)
        else:
            results = search_sessions(conn, query, limit=limit)
        conn.close()

        return {
            "query": query,
            "workspace": workspace,
            "min_importance": min_importance,
            "count": len(results),
            "results": [
                {
                    "tool": r.get("tool"),
                    "workspace": r.get("workspace"),
                    "importance": r.get("importance", 3),
                    "date": r.get("started_at", "")[:10] if r.get("started_at") else "unknown",
                    "summary": r.get("summary", "No summary"),
                    "session_id": r.get("session_id"),
                }
                for r in results
            ]
        }

    def tool_recent(self, args: Dict) -> Dict:
        """Get recent sessions across all tools."""
        days = args.get("days", 7)
        limit = args.get("limit", 20)
        conn = self._read_db()
        results = get_recent_sessions(conn, days=days, limit=limit)
        conn.close()

        return {
            "days": days,
            "count": len(results),
            "sessions": [
                {
                    "tool": r.get("tool"),
                    "date": r.get("started_at", "")[:10] if r.get("started_at") else "unknown",
                    "summary": r.get("summary", "No summary")[:120],
                }
                for r in results
            ]
        }

    def tool_stella(self, args: Dict) -> Dict:
        """Query the local Stella topology store (persistent, T1/T2+octa)."""
        from memory.stella_store import get_store

        store = get_store()
        action = args.get("action", "stats")

        if action == "stats":
            return store.status()

        if action == "query":
            return store.query(
                text=args.get("text"),
                k=int(args.get("k", 4)),
                face=args.get("face", "ALL"),
                backend=args.get("backend"),
            )

        if action == "parity":
            return store.parity_report(
                text=args.get("text", ""),
                k=int(args.get("k", 4)),
            )

        return {"error": f"unknown action '{action}' (use 'stats', 'query', or 'parity')"}


    def tool_decisions(self, args: Dict) -> Dict:
        """Get active architectural decisions."""
        limit = args.get("limit", 20)
        conn = self._read_db()
        results = get_active_decisions(conn, limit=limit)
        conn.close()

        return {
            "count": len(results),
            "decisions": [
                {
                    "title": r.get("title"),
                    "decision": r.get("decision"),
                    "rationale": r.get("rationale"),
                    "context": r.get("context"),
                    "status": r.get("status"),
                    "date": r.get("decided_at", "")[:10] if r.get("decided_at") else "unknown",
                }
                for r in results
            ]
        }

    def tool_patterns(self, args: Dict) -> Dict:
        """Get learned patterns and conventions."""
        conn = self._read_db()
        cursor = conn.execute(
            """
            SELECT pattern_type, description, code_example, created_at
            FROM patterns
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.get("limit", 20),)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        return {
            "count": len(rows),
            "patterns": [
                {
                    "type": r.get("pattern_type"),
                    "description": r.get("description"),
                    "code_example": r.get("code_example"),
                    "date": r.get("created_at", "")[:10] if r.get("created_at") else "unknown",
                }
                for r in rows
            ]
        }

    def tool_entities(self, args: Dict) -> Dict:
        """Get extracted entities (libraries, systems, technologies)."""
        conn = self._read_db()
        results = get_entities(conn, limit=args.get("limit", 50))
        conn.close()

        return {
            "count": len(results),
            "entities": [
                {
                    "name": r.get("name"),
                    "type": r.get("type"),
                    "mentions": r.get("mention_count"),
                    "last_seen": r.get("last_seen", "")[:10] if r.get("last_seen") else "unknown",
                }
                for r in results
            ]
        }

    def tool_stats(self, args: Dict) -> Dict:
        """Get database statistics."""
        conn = self._read_db()
        stats = get_stats(conn)
        conn.close()

        return {
            "database": str(self._db_path),
            "stats": stats,
            "last_auto_extract": self._last_extract_time.isoformat() if self._last_extract_time else None,
        }

    def tool_related(self, args: Dict) -> Dict:
        """Find concepts related to a given entity."""
        entity = args.get("entity", "")
        if not entity:
            return {"entity": "", "related": [], "message": "No entity provided"}

        conn = self._read_db()
        cursor = conn.execute(
            """
            SELECT entity_b, strength, context FROM entity_links
            WHERE entity_a = ?
            UNION
            SELECT entity_a, strength, context FROM entity_links
            WHERE entity_b = ?
            ORDER BY strength DESC
            LIMIT ?
            """,
            (entity, entity, args.get("limit", 10))
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        return {
            "entity": entity,
            "related": [
                {"name": r.get("entity_b") or r.get("entity_a"), "strength": r.get("strength")}
                for r in rows
            ]
        }

    def tool_extract(self, args: Dict) -> Dict:
        """Force extraction from all available sources."""
        dry_run = args.get("dry_run", False)
        sources = args.get("sources", [])  # empty = all

        conn = self._read_db()
        extractor = EntityExtractor()
        summarizer = SessionSummarizer()
        total = 0
        source_results = {}

        extractor_map = {
            "devin": (DevinLocalExtractor, "devin_local"),
            "cursor": (CursorExtractor, "cursor"),
            "vscode_copilot": (VSCodeCopilotExtractor, "vscode_copilot"),
            "aider": (AiderExtractor, "aider"),
            "claude": (ClaudeCLIExtractor, "claude_cli"),
            "git": (GitExtractor, "git"),
            "ollama": (OllamaExtractor, "ollama"),
        }

        for key, (ExtractorClass, tool_name) in extractor_map.items():
            if sources and key not in sources:
                continue
            try:
                ext = ExtractorClass()
                if ext.is_available():
                    sessions = ext.extract(limit=args.get("limit", 100))
                    if dry_run:
                        source_results[key] = {"available": True, "sessions_found": len(sessions)}
                        continue

                    importance_scorer = ImportanceScorer()
                    workspace_inferrer = WorkspaceInferrer()
                    count = 0
                    for session in sessions:
                        intel = extractor.extract_from_session(session)
                        session["tags"] = intel["tags"]
                        session["summary"] = summarizer.summarize(session)
                        session["importance"] = importance_scorer.score(session, intel)
                        session["workspace"] = workspace_inferrer.infer(session)
                        from memory.db import (
                            insert_session, insert_entity, insert_decision,
                            insert_pattern, insert_file_activity, insert_entity_links
                        )
                        sid = insert_session(conn, session)
                        for ent in intel["entities"]:
                            insert_entity(conn, ent["name"], ent["type"], ent.get("context", ""))
                        for dec in intel["decisions"]:
                            dec["session_id"] = sid
                            insert_decision(conn, dec)
                        for pat in intel["patterns"]:
                            insert_pattern(conn, {
                                "session_id": sid,
                                "pattern_type": pat.get("type"),
                                "description": pat.get("description"),
                                "code_example": pat.get("code_example"),
                                "related_files": pat.get("related_files", []),
                            })
                        for fpath in intel["files"]:
                            insert_file_activity(conn, sid, fpath)
                        insert_entity_links(conn, intel["entities"])
                        count += 1
                    source_results[key] = {"stored": count}
                    total += count
                else:
                    source_results[key] = {"available": False}
            except Exception as e:
                source_results[key] = {"error": str(e)}

        conn.close()
        self._last_extract_time = datetime.now()

        return {
            "dry_run": dry_run,
            "total_stored": total,
            "sources": source_results,
        }

    def tool_sync(self, args: Dict) -> Dict:
        """Sync decisions and patterns to memory banks."""
        dry_run = args.get("dry_run", False)
        try:
            result = sync_all(dry_run=dry_run)
            return {"synced": result, "dry_run": dry_run}
        except Exception as e:
            return {"error": str(e), "dry_run": dry_run}

    def tool_refine(self, args: Dict) -> Dict:
        """Sync DevTrail patterns/decisions/sessions into a project's lessons file.

        Tool-agnostic self-improvement bridge: extracts intelligence from the
        DevTrail DB and merges it into the project's .devin/lessons.json (or
        .claude/lessons.json, .cursor/lessons.json, etc.). Call after extraction
        or at session end to feed cross-session learning.
        """
        import os
        from memory.self_improvement import sync_lessons, load_lessons
        from pathlib import Path

        project_dir = Path(args.get("project_dir", os.getcwd()))
        dry_run = args.get("dry_run", False)
        db_path = DB_PATH

        # Auto-detect lessons file
        lessons_file_arg = args.get("lessons_file", "")
        if lessons_file_arg:
            lessons_path = Path(lessons_file_arg)
        else:
            for candidate in [
                project_dir / ".devin" / "lessons.json",
                project_dir / ".claude" / "lessons.json",
                project_dir / ".cursor" / "lessons.json",
                project_dir / ".agents" / "lessons.json",
            ]:
                if candidate.exists():
                    lessons_path = candidate
                    break
            else:
                lessons_path = project_dir / ".devin" / "lessons.json"

        result = sync_lessons(db_path, lessons_path, dry_run=dry_run)
        result["lessons_file"] = str(lessons_path)
        return result

    def tool_capture_session(self, args: Dict) -> Dict:
        """Manually capture a session into DevTrail memory.

        Use this at the end of a conversation or after a significant
        decision to ensure it is preserved even if the tool's native
        extractor misses it.
        """
        tool_name = args.get("tool_name", "manual")
        session_id = args.get("session_id", f"{tool_name}_{datetime.now().isoformat()}")
        turns = args.get("turns", [])
        summary = args.get("summary", "")
        tags = args.get("tags", [])
        files = args.get("files", [])

        if not turns and not summary:
            return {"error": "Provide either turns or a summary"}

        session = {
            "tool": tool_name,
            "session_id": session_id,
            "started_at": args.get("started_at", datetime.now().isoformat()),
            "ended_at": args.get("ended_at", datetime.now().isoformat()),
            "summary": summary or f"Captured session with {len(turns)} turns",
            "tags": tags,
            "raw_content": json.dumps(turns),
            "turns": turns,
        }

        conn = self._read_db()
        extractor = EntityExtractor()
        summarizer = SessionSummarizer()
        importance_scorer = ImportanceScorer()
        workspace_inferrer = WorkspaceInferrer()

        intel = extractor.extract_from_session(session)
        session["tags"] = list(set(session["tags"] + intel["tags"]))
        if not summary:
            session["summary"] = summarizer.summarize(session)
        session["importance"] = importance_scorer.score(session, intel)
        session["workspace"] = workspace_inferrer.infer(session)

        from memory.db import (
            insert_session, insert_entity, insert_decision,
            insert_pattern, insert_file_activity, insert_entity_links
        )
        sid = insert_session(conn, session)
        for ent in intel["entities"]:
            insert_entity(conn, ent["name"], ent["type"], ent.get("context", ""))
        for dec in intel["decisions"]:
            dec["session_id"] = sid
            insert_decision(conn, dec)
        for pat in intel["patterns"]:
            insert_pattern(conn, {
                "session_id": sid,
                "pattern_type": pat.get("type"),
                "description": pat.get("description"),
                "code_example": pat.get("code_example"),
                "related_files": pat.get("related_files", []),
            })
        for fpath in (files or intel["files"]):
            insert_file_activity(conn, sid, fpath)
        insert_entity_links(conn, intel["entities"])
        conn.close()

        return {
            "stored": True,
            "session_id": sid,
            "importance": session["importance"],
            "workspace": session["workspace"],
            "entities_found": len(intel["entities"]),
            "decisions_found": len(intel["decisions"]),
            "patterns_found": len(intel["patterns"]),
        }

    def tool_project_brain(self, args: Dict) -> Dict:
        """Read the project brain for a given repo path."""
        repo_path = args.get("repo_path", str(Path.cwd()))
        brain_dir = Path.home() / ".dev-memory" / "projects" / Path(repo_path).name

        if not brain_dir.exists():
            return {
                "repo": Path(repo_path).name,
                "brain_dir": str(brain_dir),
                "exists": False,
                "message": "No project brain found. Run `python3 cli.py init-project-brain` to create one.",
            }

        files = {}
        for f in brain_dir.rglob("*.md"):
            try:
                files[str(f.relative_to(brain_dir))] = f.read_text()
            except Exception:
                pass

        # Also include JSON indices
        for f in brain_dir.rglob("*.json"):
            try:
                files[str(f.relative_to(brain_dir))] = json.loads(f.read_text())
            except Exception:
                pass

        return {
            "repo": Path(repo_path).name,
            "brain_dir": str(brain_dir),
            "exists": True,
            "files": files,
        }

    def tool_importance(self, args: Dict) -> Dict:
        """Get high-importance sessions (score 3-5)."""
        min_score = args.get("min_score", 3)
        limit = args.get("limit", 20)
        conn = self._read_db()
        results = get_sessions_by_importance(conn, min_importance=min_score, limit=limit)
        conn.close()

        return {
            "min_score": min_score,
            "count": len(results),
            "sessions": [
                {
                    "tool": r.get("tool"),
                    "workspace": r.get("workspace"),
                    "date": r.get("started_at", "")[:10] if r.get("started_at") else "unknown",
                    "importance": r.get("importance", 3),
                    "summary": r.get("summary", "No summary")[:120],
                }
                for r in results
            ]
        }

    def tool_workspaces(self, args: Dict) -> Dict:
        """List all workspaces/departments and session counts."""
        conn = self._read_db()
        cursor = conn.execute(
            "SELECT workspace, COUNT(*), AVG(importance) FROM sessions WHERE workspace IS NOT NULL GROUP BY workspace ORDER BY COUNT(*) DESC"
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        return {
            "count": len(rows),
            "workspaces": [
                {
                    "name": r.get("workspace"),
                    "sessions": r.get("COUNT(*)"),
                    "avg_importance": round(r.get("AVG(importance)", 3), 1),
                }
                for r in rows
            ]
        }

    def tool_compact_state(self, args: Dict) -> Dict:
        """Generate a TOON-compact state snapshot for a session or workspace.

        This is the key tool for token-efficient context reinjection.
        Call it at session end to get a dense, machine-parseable summary
        that can be injected into the next session without burning tokens.
        """
        session_id = args.get("session_id")
        workspace = args.get("workspace")
        format_type = args.get("format", "dict")  # dict, markdown, injection

        conn = self._read_db()

        if session_id:
            # Single session compact
            cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                return {"error": f"Session {session_id} not found"}
            session = dict(row)
            # Reconstruct turns
            cursor = conn.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY turn_number", (session_id,))
            session["turns"] = [dict(r) for r in cursor.fetchall()]
        elif workspace:
            # Aggregate compact for workspace
            cursor = conn.execute(
                "SELECT * FROM sessions WHERE workspace = ? ORDER BY started_at DESC LIMIT 10",
                (workspace,)
            )
            sessions = [dict(r) for r in cursor.fetchall()]
            if not sessions:
                conn.close()
                return {"error": f"No sessions found for workspace: {workspace}"}
            # Aggregate into a workspace state
            return self._aggregate_workspace_compact(conn, sessions, format_type)
        else:
            conn.close()
            return {"error": "Provide session_id or workspace"}

        conn.close()

        # Generate TOON compact
        toon = TOONCompactState()
        from memory.intelligence import EntityExtractor
        intel = EntityExtractor().extract_from_session(session)
        compact = toon.compact(session, intel)

        if format_type == "markdown":
            return {"compact": toon.render_markdown(compact), "session_id": session_id}
        elif format_type == "injection":
            return {"compact": toon.render_injection(compact), "session_id": session_id}
        else:
            return {"compact": compact, "session_id": session_id}

    def _aggregate_workspace_compact(self, conn, sessions: List[Dict], format_type: str) -> Dict:
        """Aggregate TOON state across multiple sessions in a workspace."""
        toon = TOONCompactState()
        from memory.intelligence import EntityExtractor
        extractor = EntityExtractor()

        all_decisions = []
        all_files = []
        all_open_threads = []
        all_next_steps = []
        all_entities = set()
        all_blockers = []

        for sess in sessions:
            cursor = conn.execute("SELECT * FROM turns WHERE session_id = ? ORDER BY turn_number", (sess["session_id"],))
            sess["turns"] = [dict(r) for r in cursor.fetchall()]
            intel = extractor.extract_from_session(sess)
            compact = toon.compact(sess, intel)

            all_decisions.extend(compact.get("decisions", []))
            all_files.extend(compact.get("files", []))
            all_open_threads.extend(compact.get("open_threads", []))
            all_next_steps.extend(compact.get("next_steps", []))
            all_entities.update(compact.get("entities", []))
            all_blockers.extend(compact.get("blockers", []))

        # Deduplicate
        def dedupe(items):
            seen = set()
            out = []
            for i in items:
                k = i.lower()[:50]
                if k not in seen:
                    seen.add(k)
                    out.append(i)
            return out

        aggregate = {
            "toon_version": "1.0-aggregate",
            "workspace": sessions[0].get("workspace", "unknown"),
            "session_count": len(sessions),
            "decisions": dedupe(all_decisions)[:10],
            "files": dedupe(all_files)[:15],
            "open_threads": dedupe(all_open_threads)[:8],
            "next_steps": dedupe(all_next_steps)[:8],
            "entities": sorted(all_entities)[:15],
            "blockers": dedupe(all_blockers)[:5],
        }

        if format_type == "markdown":
            lines = [
                f"## {aggregate['workspace']} Workspace State",
                f"Based on {aggregate['session_count']} recent sessions",
                "",
            ]
            if aggregate["decisions"]:
                lines.append("D:")
                for d in aggregate["decisions"]:
                    lines.append(f"  • {d}")
                lines.append("")
            if aggregate["next_steps"]:
                lines.append("N:")
                for n in aggregate["next_steps"]:
                    lines.append(f"  → {n}")
                lines.append("")
            if aggregate["open_threads"]:
                lines.append("O:")
                for o in aggregate["open_threads"]:
                    lines.append(f"  ? {o}")
                lines.append("")
            return {"compact": "\n".join(lines), "workspace": aggregate["workspace"]}
        elif format_type == "injection":
            parts = []
            if aggregate["decisions"]:
                parts.append("Decided: " + "; ".join(aggregate["decisions"][:3]))
            if aggregate["next_steps"]:
                parts.append("Next: " + "; ".join(aggregate["next_steps"][:3]))
            if aggregate["open_threads"]:
                parts.append("Open: " + "; ".join(aggregate["open_threads"][:2]))
            text = " | ".join(parts)
            if len(text) > 1000:
                text = text[:997] + "..."
            return {"compact": text, "workspace": aggregate["workspace"]}
        else:
            return {"compact": aggregate, "workspace": aggregate["workspace"]}

    # ── Survivor records (pin / orient / context reflex) ──────────────────

    def _receipt_exists(self, ref: str, repo: Optional[str]):
        """True iff `ref` resolves to an existing file or a git object.
        Relative paths resolve against `repo` then the server cwd."""
        roots = []
        if repo:
            roots.append(Path(repo).expanduser())
        roots.append(Path.cwd())
        try:
            p = Path(ref).expanduser()
            if p.is_absolute():
                if p.exists():
                    return True, "file"
            else:
                for r in roots:
                    if (r / p).exists():
                        return True, "file"
        except OSError:
            pass
        # cat-file -e is a real existence check — `rev-parse --verify` is
        # syntax-only for full-length hex and accepts nonexistent SHAs.
        if not ref.startswith("-"):
            for r in roots:
                try:
                    res = subprocess.run(
                        ["git", "-C", str(r), "cat-file", "-e", ref],
                        capture_output=True, timeout=5)
                    if res.returncode == 0:
                        return True, "git"
                except Exception:
                    pass
        return False, None

    def tool_pin_survivor(self, args: Dict) -> Dict:
        """Pin a verified survivor record — a small claim bound to an existing
        receipt. The receipt-existence gate is the whole design against
        immortal hallucinations: no resolvable receipt, no write."""
        domain_key = (args.get("domain_key") or "").strip()
        claim = args.get("claim") or ""
        receipt_ref = (args.get("receipt_ref") or "").strip()
        watched_files = args.get("watched_files") or []
        supersedes_id = args.get("supersedes_id")
        repo = args.get("repo")  # optional resolution root

        if not domain_key:
            return {"error": "domain_key is required"}
        if not claim:
            return {"error": "claim is required"}
        if len(claim) > 500:
            return {"error": f"claim is {len(claim)} chars — cap is 500; shorten it"}
        if not receipt_ref:
            return {"error": "receipt_ref is required"}
        if not isinstance(watched_files, list) or not all(
                isinstance(f, str) for f in watched_files):
            return {"error": "watched_files must be a list of repo-relative paths"}

        ok, kind = self._receipt_exists(receipt_ref, repo)
        if not ok:
            return {"error": f"receipt_ref {receipt_ref!r} resolves to nothing — "
                             "write rejected (needs an existing file or git object)"}

        conn = self._read_db()
        try:
            new_id = pin_survivor(conn, domain_key, claim, receipt_ref,
                                  watched_files=watched_files,
                                  supersedes_id=supersedes_id)
        finally:
            conn.close()
        return {"id": new_id, "status": "VALID", "receipt_kind": kind}

    def _git(self, cwd: Path, argv: List[str]) -> str:
        try:
            res = subprocess.run(["git", "-C", str(cwd)] + argv,
                                 capture_output=True, text=True, timeout=5)
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""

    def _survivor_stale(self, repo: Path, rec: Dict) -> bool:
        """True if any watched file's newest commit postdates the record.
        Advisory only — never writes status back to the DB."""
        try:
            files = json.loads(rec.get("watched_files") or "[]")
            created = datetime.fromisoformat(rec["created_at"])
        except Exception:
            return False
        for rel in files:
            p = Path(rel).expanduser()
            f = p if p.is_absolute() else repo / rel
            last = self._git(f.parent, ["log", "-1", "--format=%cI", "--", str(f)])
            if not last:
                continue
            try:
                if datetime.fromisoformat(last) > created:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def tool_orient(self, args: Dict) -> Dict:
        """Deterministic <2KB hydration block: active goal + current subgoal,
        VALID survivor records (<=10 per domain_key, newest first, [STALE?] on
        watched-file drift), git status + last 3 commits for the active repo.
        No raw session dumps, no FTS."""
        repo = Path(args.get("repo") or os.getcwd()).expanduser()
        conn = self._read_db()
        try:
            block = self._build_orient_block(conn, repo)
        finally:
            conn.close()
        return {"orient": block, "bytes": len(block.encode())}

    def _build_orient_block(self, conn, repo: Path) -> str:
        lines: List[str] = ["▶ ORIENT"]

        # (a) active goal + current subgoal — project .devin state, best-effort
        try:
            goals = json.loads((repo / ".devin" / "active_goals.json").read_text())
            for g in goals.get("goals", []):
                if g.get("status") == "active":
                    lines.append(f"goal: {(g.get('objective') or '')[:110]}")
        except Exception:
            pass
        try:
            bank = json.loads((repo / ".devin" / "memory-bank.json").read_text())
            subs = (bank.get("status") or {}).get("open_subgoals") or []
            for s in subs[:2]:
                name = Path(str(s)).name or str(s)
                lines.append(f"subgoal: {name[:90]}")
        except Exception:
            pass

        # (b)+(d) VALID survivor records, <=10 per domain_key, [STALE?] on drift
        survivors = get_valid_survivors(conn, per_domain=10)
        if survivors:
            lines.append("▶ SURVIVORS")
            for rec in survivors:
                claim = rec["claim"][:140] + ("…" if len(rec["claim"]) > 140 else "")
                stale = "[STALE?] " if self._survivor_stale(repo, rec) else ""
                lines.append(f"{stale}{rec['domain_key']}#{rec['id']}: {claim}")

        # (c) git status + last 3 commits for the active repo
        if self._git(repo, ["rev-parse", "--is-inside-work-tree"]) == "true":
            lines.append(f"▶ GIT {repo.name}")
            status = self._git(repo, ["status", "--short"])
            if status:
                st = status.splitlines()
                lines.extend(l[:100] for l in st[:8])
                if len(st) > 8:
                    lines.append(f"…+{len(st) - 8} more")
            for l in (self._git(repo, ["log", "-3", "--format=%h %s"]) or "").splitlines():
                lines.append(l[:120])

        block = "\n".join(lines)
        if len(block.encode()) > ORIENT_MAX_BYTES:
            block = block.encode()[:ORIENT_MAX_BYTES - 2].decode(errors="ignore") + "…"
        return block

    def _live_session_bytes(self) -> Optional[int]:
        """Byte-sum of the live ancestor chain of the most relevant Devin
        session — the context-pressure signal. Walking parent_node_id keeps
        the measure to live context (dead branches/dropped turns excluded);
        indexed, depth-capped at 4000, read-only. A WAL snapshot can lag the
        newest turns; None when unavailable."""
        if not self._sessions_db.exists():
            return None
        import sqlite3
        for uri in (f"file:{self._sessions_db}?mode=ro",
                    f"file:{self._sessions_db}?mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=0)
                try:
                    row = conn.execute(
                        "SELECT id FROM sessions ORDER BY (working_directory = ?) "
                        "DESC, last_activity_at DESC LIMIT 1",
                        (os.getcwd(),)).fetchone()
                    if not row:
                        return None
                    sid = row[0]
                    r = conn.execute("""
                        WITH RECURSIVE chain(rid, parent, n) AS (
                          SELECT row_id, parent_node_id, LENGTH(chat_message) FROM (
                            SELECT row_id, parent_node_id, chat_message
                            FROM message_nodes
                            WHERE session_id=? ORDER BY row_id DESC LIMIT 1)
                          UNION ALL
                          SELECT m.row_id, m.parent_node_id,
                                 LENGTH(m.chat_message)
                            FROM message_nodes m JOIN chain c
                              ON m.session_id = ? AND m.node_id = c.parent
                        )
                        SELECT COALESCE(SUM(n),0) FROM
                            (SELECT n FROM chain LIMIT 4000)
                    """, (sid, sid)).fetchone()
                    return int(r[0])
                finally:
                    conn.close()
            except Exception:
                continue
        return None

    def _estimate_context_pct(self) -> float:
        """Coarse live-context estimate as a fraction. Advisory — feeds only
        the >75% pin reminder, never a decision."""
        live = self._live_session_bytes() or 0
        return max(self._ctx_bytes, live) / CONTEXT_WINDOW_BYTES

    def _append_ctx_warning(self, content: List[Dict]) -> None:
        """Append the >75% reminder as a separate content item so the primary
        text stays parseable JSON."""
        try:
            pct = self._estimate_context_pct()
        except Exception:
            return
        if pct >= CONTEXT_WARN_PCT:
            content.append({"type": "text", "text":
                f"[SYSTEM: context ~{pct * 100:.0f}% — compaction imminent; "
                "pin_survivor() any unrecorded verified findings NOW]"})


# ── Tool Schema Definitions ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "devtrail_search",
        "description": "Search DevTrail memory for sessions, decisions, and context matching a query. Optionally filter by workspace and importance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g., 'Redis lock', 'auth refactor')"},
                "limit": {"type": "integer", "default": 20, "description": "Max results to return"},
                "workspace": {"type": "string", "description": "Filter to a specific workspace/department"},
                "min_importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "Filter out low-importance sessions"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "devtrail_recent",
        "description": "Get recent sessions from all tools (last N days).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "description": "Number of days back"},
                "limit": {"type": "integer", "default": 20, "description": "Max sessions"},
            },
        },
    },
    {
        "name": "devtrail_decisions",
        "description": "Get active architectural and implementation decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "devtrail_patterns",
        "description": "Get learned patterns, conventions, and repeated fixes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "devtrail_entities",
        "description": "Get extracted entities: libraries, systems, technologies, projects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "devtrail_stats",
        "description": "Get DevTrail database statistics: session counts, tool breakdown, last extraction.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devtrail_stella",
        "description": "Local Stella topology store (persistent, T1 factual / T2 reflective + octa resonance). Sessions are mirrored in on capture. Actions: 'stats' for topology counts, 'query' for top-k recall with resonance partners.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "default": "stats", "description": "'stats', 'query', or 'parity'"},
                "text": {"type": "string", "description": "Query text (for action=query/parity)"},
                "k": {"type": "integer", "default": 4, "description": "Top-k results"},
                "face": {"type": "string", "default": "ALL", "description": "T1, T2, or ALL"},
                "backend": {"type": "string", "description": "flat|stella|shadow — overrides env default"},
            },
        },
    },
    {
        "name": "devtrail_related",
        "description": "Find concepts related to a given entity in the knowledge graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name to look up (e.g., 'React', 'PostgreSQL')"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "devtrail_extract",
        "description": "Force extraction from all available AI tools and Git. Auto-runs on startup, but call this after a long session to ensure freshness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["devin", "cursor", "vscode_copilot", "aider", "claude", "git", "ollama"]},
                    "description": "Specific sources to extract from. Omit for all.",
                },
                "dry_run": {"type": "boolean", "default": False, "description": "Preview what would be extracted without storing"},
                "limit": {"type": "integer", "default": 100, "description": "Max sessions per source"},
            },
        },
    },
    {
        "name": "devtrail_sync",
        "description": "Sync recent decisions and patterns to IDE memory banks (.claude/memory/, .cascade/memory/).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "devtrail_capture_session",
        "description": "Capture the current conversation or work session into DevTrail. Call at end of session or after a big decision.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "default": "manual", "description": "Which tool this session came from (e.g., 'claude_cli', 'devin_local')"},
                "session_id": {"type": "string", "description": "Unique session identifier. Auto-generated if omitted."},
                "turns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant", "tool"]},
                            "content": {"type": "string"},
                            "timestamp": {"type": "string"},
                        },
                    },
                    "description": "Conversation turns to preserve",
                },
                "summary": {"type": "string", "description": "One-line summary of the session"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags like ['bugfix', 'refactor', 'api-design']"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Files touched or discussed"},
                "started_at": {"type": "string", "description": "ISO timestamp"},
                "ended_at": {"type": "string", "description": "ISO timestamp"},
            },
        },
    },
    {
        "name": "devtrail_project_brain",
        "description": "Read the project brain for a repository: architecture docs, decision logs, open threads, release briefs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Path to repository root. Defaults to current working directory."},
            },
        },
    },
    {
        "name": "devtrail_importance",
        "description": "Get high-importance sessions (score 3-5). Filter out noise and chitchat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_score": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5, "description": "Minimum importance score (1-5)"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "devtrail_workspaces",
        "description": "List all workspaces/departments and their session counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devtrail_compact_state",
        "description": "Generate a TOON-compact state snapshot for a session or workspace. Ultra-dense, token-optimized summary for context reinjection. Formats: dict (structured), markdown (human), injection (agent context).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Specific session ID to compact"},
                "workspace": {"type": "string", "description": "Aggregate all recent sessions in a workspace"},
                "format": {"type": "string", "enum": ["dict", "markdown", "injection"], "default": "dict", "description": "Output format: structured dict, markdown, or single-paragraph injection string"},
            },
        },
    },
    {
        "name": "devtrail_refine",
        "description": "Sync DevTrail's extracted patterns, decisions, and high-importance sessions into a project's self-improvement lessons file (.devin/lessons.json, .claude/lessons.json, etc.). Tool-agnostic — works with any project that has a lessons file. Call after extraction or at session end to feed cross-session learning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "Path to project root. Defaults to current working directory."},
                "lessons_file": {"type": "string", "description": "Explicit path to lessons.json. Auto-detected if omitted (checks .devin/, .claude/, .cursor/, .agents/)."},
                "dry_run": {"type": "boolean", "default": False, "description": "Preview what would be synced without writing."},
            },
        },
    },
    {
        # Unprefixed per spec — dispatch maps it to tool_pin_survivor.
        "name": "pin_survivor",
        "description": "Pin a verified survivor record: a small claim (<=500 chars, rejected not truncated) bound to an existing receipt — a file path (absolute preferred; relative resolves against repo then server cwd) or a git object/SHA verified via cat-file -e (rev-parse --verify is syntax-only for full hex — cat-file actually checks the object db). The receipt-existence gate is the design: no receipt, no write. supersedes_id marks the old record OBSOLETE in the same transaction. watched_files (repo-relative paths) earn a [STALE?] flag in devtrail_orient when they drift.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain_key": {"type": "string", "description": "Domain bucket, e.g. 'v2d/t2-pilot' or 'devtrail/db'"},
                "claim": {"type": "string", "description": "The verified finding, <=500 chars"},
                "receipt_ref": {"type": "string", "description": "Existing file path or git SHA — must resolve at write time"},
                "watched_files": {"type": "array", "items": {"type": "string"}, "default": [], "description": "Repo-relative paths whose later commits flag the record [STALE?]"},
                "supersedes_id": {"type": "integer", "description": "Record id to mark OBSOLETE in the same transaction"},
                "repo": {"type": "string", "description": "Optional repo root for resolving relative receipts and git objects"},
            },
            "required": ["domain_key", "claim", "receipt_ref"],
        },
    },
    {
        "name": "devtrail_orient",
        "description": "Deterministic <2KB hydration block: active goal + current subgoal (from .devin/), VALID survivor records (<=10 per domain_key, newest first, [STALE?] when a watched file drifted), git status + last 3 commits for the active repo. No raw session dumps, no FTS over transcripts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Active repo root. Defaults to the server's cwd."},
            },
        },
    },
]


# ── Main Server Loop ───────────────────────────────────────────────────────

def main():
    server = DevTrailMCPServer()
    server._ensure_db()
    server.auto_extract()

    while True:
        msg = _read()
        if msg is None:
            break

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            _send(_make_response(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "devtrail-mcp",
                    "version": "0.2.0",
                },
            }))

        elif method == "notifications/initialized":
            pass  # No response needed

        elif method == "tools/list":
            _send(_make_response(req_id, {"tools": TOOLS}))

        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            handler_name = f"tool_{name.replace('devtrail_', '')}"
            server._ctx_bytes += len(json.dumps(arguments, default=str))

            if hasattr(server, handler_name):
                try:
                    result = getattr(server, handler_name)(arguments)
                    text = json.dumps(result, indent=2, default=str)
                    server._ctx_bytes += len(text)
                    content = [{"type": "text", "text": text}]
                    server._append_ctx_warning(content)
                    _send(_make_response(req_id, {"content": content}))
                except Exception as e:
                    _send(_make_response(req_id, {
                        "content": [{"type": "text", "text": json.dumps({"error": str(e)}, indent=2)}],
                        "isError": True,
                    }))
            else:
                _send(_make_error(req_id, -32601, f"Unknown tool: {name}"))

        elif method == "prompts/list":
            _send(_make_response(req_id, {"prompts": []}))

        elif method == "resources/list":
            _send(_make_response(req_id, {"resources": []}))

        else:
            if req_id is not None:
                _send(_make_error(req_id, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
