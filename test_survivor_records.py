"""Tests for survivor records: pin_survivor + devtrail_orient + context reflex.

Run: python3 -m pytest test_survivor_records.py -v   (or: python3 test_survivor_records.py)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import memory.db as db
import mcp_server


def _server(tmpdir):
    """MCP server whose reads land on a fresh temp DB."""
    orig = db.DB_PATH
    db.DB_PATH = Path(tmpdir) / "t.db"
    srv = mcp_server.DevTrailMCPServer()
    # point the live-session probe at nothing — tests exercise the counter path
    srv._sessions_db = Path(tmpdir) / "no-sessions.db"
    return srv, orig


def _restore(orig):
    db.DB_PATH = orig


def _git_repo(d):
    """Init a git repo with one committed file. Returns (dir, sha, relfile)."""
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    (Path(d) / "results").mkdir(exist_ok=True)
    (Path(d) / "results" / "receipt.md").write_text("verdict\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                         capture_output=True, text=True, check=True).stdout.strip()
    return d, sha, "results/receipt.md"


def test_table_created_by_init_db():
    with tempfile.TemporaryDirectory() as d:
        conn = db.init_db(Path(d) / "t.db")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(survivor_records)")}
        assert {"id", "domain_key", "claim", "receipt_ref", "watched_files",
                "supersedes_id", "status", "created_at", "updated_at"} <= cols
        conn.close()


def test_pin_accepts_real_file_receipt():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            receipt = Path(d) / "results" / "r.md"
            receipt.parent.mkdir()
            receipt.write_text("verdict: PASS\n")
            r = srv.tool_pin_survivor({
                "domain_key": "devtrail/db",
                "claim": "bounded query budget killed the 2.3GB search wedge",
                "receipt_ref": str(receipt),
                "watched_files": ["memory/db.py"],
            })
            assert "id" in r, r
            conn = db.init_db(Path(d) / "t.db")
            row = conn.execute("SELECT * FROM survivor_records").fetchone()
            assert row["status"] == "VALID"
            assert json.loads(row["watched_files"]) == ["memory/db.py"]
            conn.close()
        finally:
            _restore(orig)


def test_pin_rejects_missing_file_and_bad_sha():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            r = srv.tool_pin_survivor({
                "domain_key": "devtrail/db", "claim": "x",
                "receipt_ref": "/nonexistent/nope.md"})
            assert "error" in r and "id" not in r
            repo, _, _ = _git_repo(d)
            r = srv.tool_pin_survivor({
                "domain_key": "devtrail/db", "claim": "x",
                "receipt_ref": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "repo": repo})
            assert "error" in r and "id" not in r
            conn = db.init_db(Path(d) / "t.db")
            assert conn.execute("SELECT COUNT(*) FROM survivor_records").fetchone()[0] == 0
            conn.close()
        finally:
            _restore(orig)


def test_pin_accepts_git_sha():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            repo, sha, _ = _git_repo(d)
            r = srv.tool_pin_survivor({
                "domain_key": "devtrail/db", "claim": "sha-pinned finding",
                "receipt_ref": sha, "repo": repo})
            assert "id" in r, r
        finally:
            _restore(orig)


def test_pin_rejects_claim_over_500_chars():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            receipt = Path(d) / "r.md"
            receipt.write_text("x")
            r = srv.tool_pin_survivor({
                "domain_key": "d", "claim": "x" * 501,
                "receipt_ref": str(receipt)})
            assert "error" in r and "id" not in r
            conn = db.init_db(Path(d) / "t.db")
            assert conn.execute("SELECT COUNT(*) FROM survivor_records").fetchone()[0] == 0
            conn.close()
        finally:
            _restore(orig)


def test_supersede_marks_old_obsolete():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            receipt = Path(d) / "r.md"
            receipt.write_text("x")
            a = srv.tool_pin_survivor({
                "domain_key": "d", "claim": "old claim",
                "receipt_ref": str(receipt)})
            b = srv.tool_pin_survivor({
                "domain_key": "d", "claim": "new claim",
                "receipt_ref": str(receipt), "supersedes_id": a["id"]})
            conn = db.init_db(Path(d) / "t.db")
            rows = {r["id"]: r["status"] for r in
                    conn.execute("SELECT id, status FROM survivor_records")}
            assert rows[a["id"]] == "OBSOLETE"
            assert rows[b["id"]] == "VALID"
            conn.close()
        finally:
            _restore(orig)


def test_orient_block_under_2kb_and_contains_record():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            repo, _, relfile = _git_repo(d)
            srv.tool_pin_survivor({
                "domain_key": "v2d/t2-e5c",
                "claim": "recall-blend verdict: KILLED at declared gates",
                "receipt_ref": str(Path(repo) / relfile),
                "watched_files": [relfile]})
            r = srv.tool_orient({"repo": repo})
            block = r["orient"]
            assert len(block.encode()) <= 2048
            assert "v2d/t2-e5c" in block
            assert "recall-blend verdict" in block
        finally:
            _restore(orig)


def test_orient_caps_ten_per_domain():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            repo, _, relfile = _git_repo(d)
            receipt = str(Path(repo) / relfile)
            for i in range(11):
                srv.tool_pin_survivor({
                    "domain_key": "d", "claim": f"claim-{i:02d}",
                    "receipt_ref": receipt})
            block = srv.tool_orient({"repo": repo})["orient"]
            assert "claim-10" in block
            assert "claim-00" not in block  # oldest pushed off the ≤10 cap
        finally:
            _restore(orig)


def test_orient_marks_stale_when_watched_file_drifted():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            repo, _, relfile = _git_repo(d)
            srv.tool_pin_survivor({
                "domain_key": "d", "claim": "watching a file",
                "receipt_ref": str(Path(repo) / relfile),
                "watched_files": [relfile]})
            # drift: new commit to the watched file, stamped in the future
            (Path(repo) / relfile).write_text("changed\n")
            env = dict(os.environ, GIT_COMMITTER_DATE="2030-01-01T00:00:00Z",
                       GIT_AUTHOR_DATE="2030-01-01T00:00:00Z")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
            subprocess.run(["git", "commit", "-qm", "drift"], cwd=repo,
                           check=True, env=env)
            block = srv.tool_orient({"repo": repo})["orient"]
            assert "[STALE?]" in block
        finally:
            _restore(orig)


def test_context_warning_fires_over_threshold():
    with tempfile.TemporaryDirectory() as d:
        srv, orig = _server(d)
        try:
            orig_window = mcp_server.CONTEXT_WINDOW_BYTES
            mcp_server.CONTEXT_WINDOW_BYTES = 100
            srv._ctx_bytes = 80  # 80%
            content = [{"type": "text", "text": "{}"}]
            srv._append_ctx_warning(content)
            assert len(content) == 2
            assert "pin_survivor()" in content[1]["text"]
            srv._ctx_bytes = 10  # 10% — below threshold
            content = [{"type": "text", "text": "{}"}]
            srv._append_ctx_warning(content)
            assert len(content) == 1
            mcp_server.CONTEXT_WINDOW_BYTES = orig_window
        finally:
            _restore(orig)


if __name__ == "__main__":
    try:
        import pytest
        raise SystemExit(pytest.main([__file__, "-v"]))
    except ImportError:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_"):
                fn()
                print(f"PASS {name}")
