"""Self-improvement bridge — syncs DevTrail patterns/decisions to project lesson files.

This module bridges DevTrail's extracted intelligence (patterns, decisions,
high-importance sessions) into a project's self-improvement file (lessons.json).
It is tool-agnostic: works with any project that has a lessons file, regardless
of whether the sessions were captured from Devin, Cursor, Claude, Aider, etc.

The lessons file format is a JSON object:
  {
    "lessons": [
      {
        "id": "l_...",
        "content": "Avoid repeating: `firebase deploy` — failed 3x ...",
        "category": "failure_avoidance" | "pattern" | "decision" | "session_summary",
        "source": "devtrail_sync_<timestamp>",
        "created": <epoch>,
        "times_reinforced": 1
      }
    ],
    "last_updated": <epoch>
  }

Usage:
  python3 memory/self_improvement.py --sync-lessons \
    --project-dir /path/to/project \
    --lessons-file /path/to/project/.devin/lessons.json \
    --db ~/.dev-memory/memory.db

  # Preview without writing
  python3 memory/self_improvement.py --sync-lessons --dry-run \
    --project-dir /path/to/project

  # Show what's in the lessons file
  python3 memory/self_improvement.py --show-lessons \
    --lessons-file /path/to/project/.devin/lessons.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# Default DB path (matches DevTrail's db.py)
DEFAULT_DB = Path.home() / ".dev-memory" / "memory.db"

# Limits
MAX_PATTERNS_TO_SYNC = 15
MAX_DECISIONS_TO_SYNC = 10
MAX_IMPORTANT_SESSIONS = 5
MAX_TOTAL_LESSONS = 100


def load_lessons(lessons_path: Path) -> dict:
    """Load the lessons file, returning an empty structure if it doesn't exist."""
    if lessons_path.exists():
        try:
            return json.loads(lessons_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"lessons": [], "last_updated": 0}


def save_lessons(lessons_path: Path, data: dict) -> None:
    """Save the lessons file, creating parent dirs if needed."""
    lessons_path.parent.mkdir(parents=True, exist_ok=True)
    data["last_updated"] = time.time()
    lessons_path.write_text(json.dumps(data, indent=2))


def extract_patterns_from_db(db_path: Path, limit: int = MAX_PATTERNS_TO_SYNC) -> list[dict]:
    """Extract recent patterns from the DevTrail DB."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pattern_type, description, related_files, created_at "
            "FROM patterns ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # table doesn't exist or DB not initialized
    finally:
        conn.close()


def extract_decisions_from_db(db_path: Path, limit: int = MAX_DECISIONS_TO_SYNC) -> list[dict]:
    """Extract recent active decisions from the DevTrail DB."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT title, decision, rationale, status, decided_at "
            "FROM decisions WHERE status = 'active' "
            "ORDER BY decided_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def extract_important_sessions(db_path: Path, limit: int = MAX_IMPORTANT_SESSIONS) -> list[dict]:
    """Extract high-importance recent sessions from the DevTrail DB."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT tool, summary, tags, importance, started_at "
            "FROM sessions WHERE importance >= 3 "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def patterns_to_lessons(patterns: list[dict]) -> list[dict]:
    """Convert DevTrail patterns into lesson entries."""
    lessons = []
    now = time.time()
    for p in patterns:
        desc = p.get("description", "").strip()
        if not desc:
            continue
        ptype = p.get("pattern_type", "general")
        files = p.get("related_files", "")
        content = f"[{ptype}] {desc}"
        if files:
            content += f" (files: {files})"
        lessons.append({
            "id": f"l_pattern_{int(now)}_{abs(hash(desc)) % 10000}",
            "content": content[:300],
            "category": "pattern",
            "source": f"devtrail_sync_{int(now)}",
            "created": now,
            "times_reinforced": 1,
        })
    return lessons


def decisions_to_lessons(decisions: list[dict]) -> list[dict]:
    """Convert DevTrail decisions into lesson entries."""
    lessons = []
    now = time.time()
    for d in decisions:
        title = d.get("title", "").strip()
        decision = d.get("decision", "").strip()
        if not title and not decision:
            continue
        content = f"Decision: {title}"
        if decision:
            content += f" — {decision}"
        rationale = d.get("rationale", "")
        if rationale:
            content += f" (rationale: {rationale})"
        lessons.append({
            "id": f"l_decision_{int(now)}_{abs(hash(title)) % 10000}",
            "content": content[:300],
            "category": "decision",
            "source": f"devtrail_sync_{int(now)}",
            "created": now,
            "times_reinforced": 1,
        })
    return lessons


def sessions_to_lessons(sessions: list[dict]) -> list[dict]:
    """Convert important sessions into lesson entries (session summaries)."""
    lessons = []
    now = time.time()
    for s in sessions:
        summary = s.get("summary", "").strip()
        if not summary:
            continue
        tool = s.get("tool", "unknown")
        tags = s.get("tags", "")
        content = f"[{tool}] {summary}"
        if tags:
            content += f" (tags: {tags})"
        lessons.append({
            "id": f"l_session_{int(now)}_{abs(hash(summary)) % 10000}",
            "content": content[:300],
            "category": "session_summary",
            "source": f"devtrail_sync_{int(now)}",
            "created": now,
            "times_reinforced": 1,
        })
    return lessons


def _shares_key_token(a: str, b: str) -> bool:
    """Check if two strings share a significant token (for deduplication)."""
    for token in a.split():
        if len(token) > 8 and token in b:
            return True
    return False


def merge_lessons(existing: dict, new_lessons: list[dict]) -> dict:
    """Merge new lessons into existing, deduplicating by content similarity.

    If a new lesson shares a key token with an existing one of the same category,
    the existing lesson's times_reinforced counter is incremented instead of
    adding a duplicate.
    """
    all_lessons = existing.get("lessons", [])
    for new_l in new_lessons:
        found_similar = False
        for existing_l in all_lessons:
            if existing_l.get("category") == new_l.get("category"):
                if _shares_key_token(new_l.get("content", ""), existing_l.get("content", "")):
                    existing_l["times_reinforced"] = existing_l.get("times_reinforced", 1) + 1
                    existing_l["last_reinforced"] = time.time()
                    found_similar = True
                    break
        if not found_similar:
            all_lessons.append(new_l)

    # Cap total lessons, keeping most reinforced + most recent
    if len(all_lessons) > MAX_TOTAL_LESSONS:
        all_lessons.sort(
            key=lambda l: (l.get("times_reinforced", 1), l.get("created", 0)),
            reverse=True,
        )
        all_lessons = all_lessons[:MAX_TOTAL_LESSONS]

    existing["lessons"] = all_lessons
    return existing


def sync_lessons(
    db_path: Path,
    lessons_path: Path,
    dry_run: bool = False,
) -> dict:
    """Sync DevTrail patterns/decisions/sessions into the lessons file.

    Returns a summary dict with counts.
    """
    # Extract from DevTrail DB
    patterns = extract_patterns_from_db(db_path)
    decisions = extract_decisions_from_db(db_path)
    sessions = extract_important_sessions(db_path)

    # Convert to lesson format
    new_lessons = (
        patterns_to_lessons(patterns)
        + decisions_to_lessons(decisions)
        + sessions_to_lessons(sessions)
    )

    if not new_lessons:
        return {"synced": 0, "patterns": 0, "decisions": 0, "sessions": 0, "total": 0}

    # Load existing lessons and merge
    existing = load_lessons(lessons_path)
    before_count = len(existing.get("lessons", []))
    merged = merge_lessons(existing, new_lessons)
    after_count = len(merged.get("lessons", []))

    if not dry_run:
        save_lessons(lessons_path, merged)

    return {
        "synced": after_count - before_count,
        "reinforced": len(new_lessons) - (after_count - before_count),
        "patterns": len(patterns),
        "decisions": len(decisions),
        "sessions": len(sessions),
        "total": after_count,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync DevTrail intelligence to a project's self-improvement lessons file."
    )
    parser.add_argument("--sync-lessons", action="store_true", help="Sync patterns/decisions to lessons file")
    parser.add_argument("--show-lessons", action="store_true", help="Show current lessons in the lessons file")
    parser.add_argument("--project-dir", type=str, default=".", help="Project root directory")
    parser.add_argument("--lessons-file", type=str, default="", help="Path to lessons.json (auto-detected if omitted)")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB), help="Path to DevTrail memory.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    db_path = Path(args.db)

    # Auto-detect lessons file if not specified
    if args.lessons_file:
        lessons_path = Path(args.lessons_file)
    else:
        project_dir = Path(args.project_dir)
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
            # Default to .devin/lessons.json in the project dir
            lessons_path = project_dir / ".devin" / "lessons.json"

    if args.show_lessons:
        data = load_lessons(lessons_path)
        lessons = data.get("lessons", [])
        print(f"Lessons file: {lessons_path}")
        print(f"Total lessons: {len(lessons)}")
        print(f"Last updated: {data.get('last_updated', 'never')}")
        for l in lessons:
            reinforce = f" (reinforced {l.get('times_reinforced', 1)}x)" if l.get("times_reinforced", 1) > 1 else ""
            print(f"  [{l.get('category', '?')}] {l.get('content', '')[:100]}{reinforce}")
        return 0

    if args.sync_lessons:
        result = sync_lessons(db_path, lessons_path, dry_run=args.dry_run)
        prefix = "[dry-run] " if args.dry_run else ""
        print(f"{prefix}Synced {result['synced']} new lessons, reinforced {result['reinforced']} existing")
        print(f"  Patterns: {result['patterns']}, Decisions: {result['decisions']}, Sessions: {result['sessions']}")
        print(f"  Total lessons in file: {result['total']}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
