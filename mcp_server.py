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
    DB_PATH
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

    def _ensure_db(self):
        """Initialize DB if needed."""
        if not self._db_path.exists():
            conn = init_db()
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
                conn = init_db()
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

        conn = init_db()
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
        conn = init_db()
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

    def tool_decisions(self, args: Dict) -> Dict:
        """Get active architectural decisions."""
        limit = args.get("limit", 20)
        conn = init_db()
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
        conn = init_db()
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
        conn = init_db()
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
        conn = init_db()
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

        conn = init_db()
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

        conn = init_db()
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

        conn = init_db()
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
        conn = init_db()
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
        conn = init_db()
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

        conn = init_db()

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

            if hasattr(server, handler_name):
                try:
                    result = getattr(server, handler_name)(arguments)
                    _send(_make_response(req_id, {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}]
                    }))
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
