"""Extractor for Devin Local chat history.

Reads from Devin Local's internal SQLite database and cached session data.
"""

import sqlite3
import json
import re
import os
import tempfile
import shutil
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
from .base import BaseExtractor


class DevinLocalExtractor(BaseExtractor):
    """Extracts conversation history from Devin Local."""
    
    TOOL_NAME = "devin_local"
    DISPLAY_NAME = "Devin Local"
    
    # Primary database location
    DIPS_DB = Path.home() / "Library" / "Application Support" / "Windsurf" / "DIPS"

    # Devin CLI/app session store — the CANONICAL live transcript
    # source (Windsurf paths above are legacy/dead). Tables:
    #   sessions(id, working_directory, title, created_at,
    #            last_activity_at, hidden)
    #   message_nodes(session_id, node_id, chat_message json{role,
    #            content}, created_at)
    SESSIONS_DB = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"
    TURN_CONTENT_CAP = 50_000  # bytes per turn; bounds raw_content

    # Cached data directory
    CACHED_DATA = Path.home() / "Library" / "Application Support" / "Windsurf" / "CachedData"

    # Claude logs directory (contains actual conversations)
    LOGS_DIR = Path.home() / "Library" / "Application Support" / "Windsurf" / "logs"

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or self.DIPS_DB
        self.sessions: List[Dict] = []

    def is_available(self) -> bool:
        """Check if Devin Local data is accessible."""
        return (self.SESSIONS_DB.exists() or self.db_path.exists()
                or self.CACHED_DATA.exists() or self.LOGS_DIR.exists())
    
    def get_stats(self) -> Dict:
        """Return statistics about available Devin Local data."""
        stats = super().get_stats()
        
        # Count log files
        log_count = 0
        if self.LOGS_DIR.exists():
            for date_dir in self.LOGS_DIR.iterdir():
                if date_dir.is_dir():
                    for window_dir in date_dir.iterdir():
                        if window_dir.is_dir():
                            claude_log = window_dir / "exthost" / "Anthropic.claude-code" / "Claude VSCode.log"
                            if claude_log.exists():
                                log_count += 1
        
        stats["log_files"] = log_count
        stats["dips_db_exists"] = self.db_path.exists()
        stats["cached_data_exists"] = self.CACHED_DATA.exists()
        return stats
    
    def extract(self, limit: Optional[int] = None, dry_run: bool = False) -> List[Dict]:
        """Extract sessions from Devin Local data sources.
        
        Args:
            limit: Max sessions to extract
            dry_run: If True, return preview stats without full session data
            
        Returns list of session dicts with format:
        {
            "tool": "devin_local",
            "session_id": str,
            "started_at": str,
            "ended_at": str,
            "summary": str,
            "tags": list,
            "raw_content": str,
            "turns": [
                {
                    "role": "user" | "assistant" | "tool",
                    "content": str,
                    "timestamp": str,
                    "tool_calls": list (optional)
                }
            ]
        }
        """
        if dry_run:
            stats = self.get_stats()
            return [{
                "tool": self.TOOL_NAME,
                "session_id": f"{self.TOOL_NAME}_dry_run",
                "started_at": datetime.now().isoformat(),
                "ended_at": datetime.now().isoformat(),
                "summary": f"Dry run: {stats.get('log_files', 0)} log files available",
                "tags": ["dry-run"],
                "raw_content": json.dumps(stats),
                "turns": []
            }]
        
        sessions = []

        # Devin CLI sessions.db first — the live canonical store.
        if self.SESSIONS_DB.exists():
            try:
                sessions.extend(self._extract_from_sessions_db(limit))
            except Exception as e:
                print(f"  Warning: Could not read sessions.db: {e}")

        # Legacy: Windsurf DIPS database
        if self.db_path.exists():
            try:
                sessions.extend(self._extract_from_dips(limit))
            except Exception as e:
                print(f"  Warning: Could not read DIPS database: {e}")
        
        # Fall back to cached data files
        if not sessions and self.CACHED_DATA.exists():
            try:
                sessions.extend(self._extract_from_cache(limit))
            except Exception as e:
                print(f"  Warning: Could not read cached data: {e}")
        
        # Extract from Claude VSCode.log files (primary source for conversations)
        try:
            sessions.extend(self._extract_from_claude_logs(limit))
        except Exception as e:
            print(f"  Warning: Could not read Claude logs: {e}")
        
        # Validate and filter sessions
        return self.filter_valid_sessions(sessions)
    
    def _extract_from_dips(self, limit: Optional[int] = None) -> List[Dict]:
        """Extract from Devin Local's DIPS SQLite database.
        
        Copies the database first to avoid locking issues since
        Devin Local holds the file open while running.
        """
        sessions = []
        
        import tempfile
        import shutil
        
        # Copy database to temp location to avoid locking
        temp_db = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                temp_db = tmp.name
            shutil.copy2(str(self.db_path), temp_db)
        except Exception as e:
            print(f"Warning: Could not copy DIPS database: {e}")
            return sessions
        
        try:
            conn = sqlite3.connect(temp_db)
            conn.row_factory = sqlite3.Row
            
            # Try to find conversation tables
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            
            # Look for conversation-related tables
            conv_tables = [t for t in tables if any(kw in t.lower() for kw in 
                          ['conversation', 'chat', 'message', 'session', 'prompt'])]
            
            if not conv_tables:
                # Try common table names
                conv_tables = ['conversations', 'chats', 'messages', 'interactions']
            
            for table in conv_tables:
                try:
                    # Get table schema
                    cursor = conn.execute(f"PRAGMA table_info({table})")
                    columns = [row[1] for row in cursor.fetchall()]
                    
                    # Try to extract messages
                    query = f"SELECT * FROM {table} ORDER BY timestamp DESC"
                    if limit:
                        query += f" LIMIT {limit}"
                    
                    cursor = conn.execute(query)
                    rows = cursor.fetchall()
                    
                    if rows:
                        session = self._parse_dips_rows(rows, columns, table)
                        if session:
                            sessions.append(session)
                
                except sqlite3.OperationalError:
                    continue
            
            conn.close()
        
        except Exception as e:
            print(f"DIPS extraction failed: {e}")
        
        finally:
            # Clean up temp database copy
            if temp_db and os.path.exists(temp_db):
                try:
                    os.remove(temp_db)
                except Exception:
                    pass
        
        return sessions
    
    def _parse_dips_rows(self, rows: List[sqlite3.Row], columns: List[str], table_name: str) -> Optional[Dict]:
        """Parse database rows into a session structure."""
        turns = []
        
        for row in rows:
            row_dict = dict(row)
            
            # Map common column names
            content = (row_dict.get('content') or 
                      row_dict.get('message') or 
                      row_dict.get('text') or 
                      row_dict.get('body') or 
                      row_dict.get('prompt') or 
                      "")
            
            role = (row_dict.get('role') or 
                   row_dict.get('sender') or 
                   row_dict.get('actor') or 
                   "unknown")
            
            timestamp = (row_dict.get('timestamp') or 
                        row_dict.get('created_at') or 
                        row_dict.get('time') or 
                        datetime.now().isoformat())
            
            if content:
                turns.append({
                    "role": role,
                    "content": content,
                    "timestamp": timestamp
                })
        
        if not turns:
            return None
        
        # Reverse to chronological order
        turns.reverse()
        
        return {
            "tool": "devin_local",
            "session_id": f"devin_local_{table_name}_{hash(str(turns[0]['timestamp']))}",
            "started_at": turns[0].get("timestamp"),
            "ended_at": turns[-1].get("timestamp"),
            "summary": self._generate_summary(turns),
            "tags": self._extract_tags(turns),
            "raw_content": json.dumps(turns),
            "turns": turns
        }
    
    def _extract_from_sessions_db(self, limit: Optional[int] = None) -> List[Dict]:
        """Extract live Devin CLI/app sessions from sessions.db.

        WAL mode: copy the -wal/-shm sidecars too, else the most recent
        writes are invisible in the copied DB.
        """
        sessions = []

        temp_db = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                temp_db = tmp.name
            shutil.copy2(str(self.SESSIONS_DB), temp_db)
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.SESSIONS_DB) + suffix)
                if side.exists():
                    shutil.copy2(str(side), temp_db + suffix)
        except Exception as e:
            print(f"Warning: Could not copy sessions.db: {e}")
            return sessions

        try:
            conn = sqlite3.connect(temp_db)
            conn.row_factory = sqlite3.Row

            q = ("SELECT id, working_directory, title, created_at, "
                 "last_activity_at FROM sessions WHERE hidden = 0 "
                 "ORDER BY last_activity_at DESC")
            if limit:
                q += f" LIMIT {int(limit)}"
            rows = conn.execute(q).fetchall()

            for row in rows:
                try:
                    session = self._parse_sessions_db_row(conn, row)
                    if session:
                        sessions.append(session)
                except Exception as e:
                    print(f"Warning: skipping session {row['id']}: {e}")
                    continue

            conn.close()
        except Exception as e:
            print(f"sessions.db extraction failed: {e}")
        finally:
            if temp_db:
                for suffix in ("", "-wal", "-shm"):
                    p = temp_db + suffix
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass

        return sessions

    def _parse_sessions_db_row(self, conn: sqlite3.Connection,
                               row: sqlite3.Row) -> Optional[Dict]:
        sid = row["id"]
        msgs = conn.execute(
            "SELECT chat_message, created_at FROM message_nodes "
            "WHERE session_id = ? ORDER BY row_id", (sid,)).fetchall()
        if not msgs:
            return None

        turns = []
        for m in msgs:
            try:
                msg = json.loads(m["chat_message"])
            except Exception:
                continue
            role = msg.get("role") or "unknown"
            content = msg.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "") if isinstance(part, dict)
                    else str(part) for part in content)
            if not content:
                continue
            content = str(content)[:self.TURN_CONTENT_CAP]
            ts = m["created_at"]
            try:
                ts = datetime.fromtimestamp(int(ts)).isoformat()
            except Exception:
                ts = str(ts)
            turns.append({"role": role, "content": content,
                          "timestamp": ts})
        if not turns:
            return None

        started = row["created_at"]
        ended = row["last_activity_at"]
        try:
            started = datetime.fromtimestamp(int(started)).isoformat()
            ended = datetime.fromtimestamp(int(ended)).isoformat()
        except Exception:
            pass

        summary = row["title"] or self._generate_summary(turns)
        return {
            "tool": self.TOOL_NAME,
            "session_id": f"devincli_{sid}",
            "started_at": started,
            "ended_at": ended,
            "summary": summary,
            "workspace": row["working_directory"],
            "tags": self._extract_tags(turns),
            "raw_content": json.dumps(turns),
            "turns": turns,
        }

    def _extract_from_cache(self, limit: Optional[int] = None) -> List[Dict]:
        """Extract from Devin Local's cached data files."""
        sessions = []
        
        # Look for cached data directories (hashed workspace IDs)
        for workspace_dir in self.CACHED_DATA.iterdir():
            if not workspace_dir.is_dir():
                continue
            
            # Look for chrome/cached data
            chrome_dir = workspace_dir / "chrome"
            if chrome_dir.exists():
                # Try to find any JSON files with conversation data
                for json_file in chrome_dir.rglob("*.json"):
                    try:
                        data = json.loads(json_file.read_text())
                        session = self._parse_cached_json(data, json_file.name)
                        if session:
                            sessions.append(session)
                    except (json.JSONDecodeError, Exception):
                        continue
        
        return sessions[:limit] if limit else sessions
    
    def _parse_cached_json(self, data: dict, filename: str) -> Optional[Dict]:
        """Parse cached JSON data into a session."""
        # Look for conversation arrays in the JSON
        if isinstance(data, list):
            turns = []
            for item in data:
                if isinstance(item, dict):
                    content = item.get('content') or item.get('text') or item.get('message')
                    if content:
                        turns.append({
                            "role": item.get('role', 'unknown'),
                            "content": content,
                            "timestamp": item.get('timestamp', datetime.now().isoformat())
                        })
            
            if turns:
                return {
                    "tool": "devin_local",
                    "session_id": f"devin_local_cache_{filename}",
                    "started_at": turns[0].get("timestamp"),
                    "ended_at": turns[-1].get("timestamp"),
                    "summary": self._generate_summary(turns),
                    "tags": self._extract_tags(turns),
                    "raw_content": json.dumps(turns),
                    "turns": turns
                }
        
        return None
    
    def _extract_from_claude_logs(self, limit: Optional[int] = None) -> List[Dict]:
        """Extract from Claude VSCode.log files in Devin Local.
        
        These logs contain actual conversation data from the Anthropic.claude-code extension.
        Located at: Windsurf/logs/<date>/window<N>/exthost/Anthropic.claude-code/Claude VSCode.log
        """
        sessions = []
        
        logs_dir = Path.home() / "Library" / "Application Support" / "Windsurf" / "logs"
        if not logs_dir.exists():
            return sessions
        
        # Find all Claude VSCode.log files
        log_files = []
        for date_dir in logs_dir.iterdir():
            if not date_dir.is_dir():
                continue
            for window_dir in date_dir.iterdir():
                if not window_dir.is_dir():
                    continue
                claude_log = window_dir / "exthost" / "Anthropic.claude-code" / "Claude VSCode.log"
                if claude_log.exists():
                    log_files.append(claude_log)
        
        # Sort by modification time (newest first)
        log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        
        if limit:
            log_files = log_files[:limit]
        
        for log_file in log_files:
            try:
                session = self._parse_claude_log(log_file)
                if session:
                    sessions.append(session)
            except Exception as e:
                print(f"Warning: Could not parse {log_file}: {e}")
        
        return sessions
    
    def _parse_claude_log(self, log_file: Path) -> Optional[Dict]:
        """Parse a Claude VSCode.log file for conversation data."""
        turns = []
        session_id = None
        
        # Read log file
        content = log_file.read_text(errors='ignore')
        
        # Find io_message entries (actual conversation)
        # Look for: "io_message"..."message":{"role":"user"... or assistant
        io_pattern = r'"type":"io_message".*?"message":(\{[^}]+"role":"(?:user|assistant)"[^}]*\})'
        matches = re.findall(io_pattern, content, re.DOTALL)
        
        for match in matches:
            try:
                # Extract content text
                content_match = re.search(r'"text":"([^"]+)"', match)
                role_match = re.search(r'"role":"([^"]+)"', match)
                
                if content_match and role_match:
                    turns.append({
                        "role": role_match.group(1),
                        "content": content_match.group(1).replace('\\n', '\n').replace('\\t', '\t'),
                        "timestamp": datetime.now().isoformat()
                    })
            except Exception:
                continue
        
        if not turns:
            return None
        
        # Generate session ID from file path
        session_id = f"devin_local_{log_file.parent.parent.name}_{log_file.parent.name}"
        
        return {
            "tool": "devin_local",
            "session_id": session_id,
            "started_at": datetime.fromtimestamp(log_file.stat().st_mtime).isoformat(),
            "ended_at": datetime.fromtimestamp(log_file.stat().st_mtime).isoformat(),
            "summary": self._generate_summary(turns),
            "tags": self._extract_tags(turns),
            "raw_content": json.dumps(turns),
            "turns": turns
        }
    
    def _generate_summary(self, turns: List[Dict]) -> str:
        """Generate a one-line summary from conversation turns."""
        # Get first user message as summary hint
        user_messages = [t["content"] for t in turns if t.get("role") == "user"]
        if user_messages:
            first_msg = user_messages[0]
            # Truncate to first sentence or 100 chars
            summary = first_msg.split('.')[0][:100]
            return summary + "..." if len(first_msg) > 100 else summary
        return "Devin Local session"
    
    def _extract_tags(self, turns: List[Dict]) -> List[str]:
        """Extract tags from conversation content."""
        all_text = " ".join(t["content"] for t in turns if t.get("content"))
        
        # Extract technical terms
        tech_patterns = [
            r'\b(Stella|Triad|Redis|Firebase|PostgreSQL|Supabase)\b',
            r'\b(React|TypeScript|Python|FastAPI|Docker)\b',
            r'\b(bug|fix|debug|error|issue|refactor)\b',
            r'\b(feature|implement|add|create)\b',
        ]
        
        tags = set()
        for pattern in tech_patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            tags.update(m.lower() for m in matches)
        
        return list(tags)[:10]  # Limit to 10 tags
