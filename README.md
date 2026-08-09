# DevTrail — Local Cross-Tool Memory for AI Coding

DevTrail is a local CLI tool that captures and unifies your AI coding history across tools into a single searchable memory layer on your machine. It extracts conversations, decisions, entities, patterns, and Git activity into a SQLite database so you can recover context after tool switching, context-window loss, or long gaps between sessions.

It is designed for developers who move between Claude, Cursor, Devin, Aider, VS Code + Copilot, Ollama, Git, and similar tools and want one place to ask: *what did I decide, where did this change come from, and what was I doing last time?*

**What DevTrail is:**
- A local SQLite-backed memory database for AI-assisted development.
- A cross-tool layer under per-tool memory files like `CLAUDE.md`, `.claude/memory/`, or `.cascade/memory/`.
- A way to search decisions and activity without replaying entire chat histories.

**What DevTrail is not:**
- Not a hosted service.
- Not a replacement for your IDE or model-specific context files.
- Not useful if you only work in one tool and that tool already gives you enough continuity.

## 2-Minute Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Initialize local database
python3 cli.py init

# Extract from everything DevTrail can currently see
python3 cli.py extract --all

# Search your history
python3 cli.py search "Redis race condition"
```

This creates a local database at `~/.dev-memory/memory.db` and ingests any supported tool data already present on your machine.

## What You’ll See If It Worked

After `init`:

```text
[init] created database at /Users/you/.dev-memory/memory.db
[init] created tables: sessions, turns, entities, decisions, patterns, file_activity, session_embeddings
```

After `extract --all` (example):

```text
[extract] Devin: 3 new sessions
[extract] Claude CLI: 5 new sessions
[extract] Claude Web exports: 2 files imported
[extract] Aider: 4 sessions
[extract] Git: 18 commits, 42 file changes
[extract] total: 32 sessions ingested
```

After a search:

```bash
python3 cli.py search "Redis race condition"
```

```text
[search] query: "Redis race condition"

2025-12-10 14:32  claude_cli  project-x
  - discussed Redis lock implementation
  - decided to use SETNX with expiry
  - files: app/cache.py, tests/test_cache.py

2025-12-11 09:17  git  project-x
  - commit 8f3c2ab "Add Redis lock and tests"
  - files: app/cache.py, tests/test_cache.py
```

If `extract --all` reports zero sessions, that usually means DevTrail did not find any supported source data yet. In that case, try a source-specific extractor or import Claude Web exports manually.

## Supported Sources

| Source | What it captures | How | Status |
|--------|------------------|-----|--------|
| Devin | Chat history, tool calls, file edits | VS Code extension SQLite DB | Ready  |
| Cursor | AI chat, composer sessions | VS Code fork data files | Ready  |
| VS Code + Copilot | GitHub Copilot Chat | Extension globalStorage | Ready  |
| Aider | Pair programming sessions | CLI history files in `~/.aider/` | Ready  |
| Claude CLI | Conversations, project memory | Reads `~/.claude/` files | Ready  |
| Claude Web | Exported chat JSON from claude.ai | Parses export files | Ready  |
| Git | Commits, file changes, diffs | `git log` with stats | Ready  |
| Ollama | Local/cloud model inventory, chat history | Reads `~/.ollama/` + API | Ready  |

## What DevTrail Stores

DevTrail turns raw activity into a few durable primitives:

- **Sessions** — unified session records from all supported tools.
- **Turns** — individual conversation turns within sessions.
- **Entities** — extracted concepts such as libraries, systems, projects, and technologies.
- **Decisions** — architectural and implementation choices with context.
- **Patterns** — repeated fixes, refactors, and conventions.
- **File activity** — which files were touched and when.
- **Embeddings** — semantic vectors for retrieval via `sqlite-vec`.

## Importance Scoring

Every session is auto-scored 1-5 on signal density:

| Score | Label | What it means |
|-------|-------|---------------|
| 1 | Trivial | Chitchat, simple lookups |
| 2 | Minor | One-liner fixes, small changes |
| 3 | Moderate | Routine work, few files |
| 4 | Significant | Architecture decisions, multi-file refactor |
| 5 | Critical | Major design choice, complex bug fix, breaking change |

Scoring factors: decision count, files touched, pattern richness, conversation depth, tool type, and architectural/bug/refactor keyword presence. Use `devtrail_importance` (MCP) or filter searches by `min_importance` to skip the noise.

## Workspace Tagging

Sessions are auto-tagged with their workspace/department based on:
- Git repository name from file paths
- Most common directory from file references
- Tool metadata (working directory hints)

This lets you query scoped memory: "What did I decide about auth in the **backend** workspace?"

## TOON Compact State

Inspired by Robert Ruby II's approach to managing 600k+ token contexts, DevTrail can generate **TOON** (Token-Optimized Object Notation) compact snapshots:

- **Strips**: greetings, rephrased explanations, tool invocation chatter
- **Keeps**: decisions, files, open threads, next steps, blockers, key entities
- **Formats**: structured dict, dense markdown, or single-paragraph injection string

Call `devtrail_compact_state` at session end to get a 200-token summary instead of a 20,000-token conversation dump — perfect for reinjecting into the next session's context window.

Example TOON markdown output:
```
## session_2026-06-12 [4/5]
Workspace: api-service | 2026-06-12

[devin_local] | Decided: switched from ORM to raw SQL for performance
| Touched 7 files | Topic: database optimization

D:
  • Migrated user queries to raw SQL
  • Decided on connection pooling with PgBouncer

F:
  • src/services/UserService.ts (updated)
  • src/db/pool.ts (refactored)

N:
  → Deploy to staging
  → Update migration docs

O:
  ? Benchmark under load

E: PostgreSQL, PgBouncer, TypeScript
~180 tokens
```

## Why This Exists

Per-tool memory helps with cold start, but not with fragmentation. A `CLAUDE.md`, a plugin-generated `CLAUDE-CONTEXT.md`, or an IDE memory bank can help one assistant recover state after `/clear`, but those memories usually stay trapped inside that one tool. DevTrail is meant to sit underneath them as a neutral local layer that survives switching between models, CLIs, IDEs, and web UIs.

That means you can use model-specific memories where they work best, while keeping one cross-tool trail of what happened, what changed, and why those choices were made.

## Core Workflow

A typical loop looks like this:

1. Work in one or more tools: Claude CLI, Claude Web, Cursor, Devin, Aider, Copilot, Git.
2. Run extraction to pull those traces into one database.
3. Search or inspect the database when you need to recover context.
4. Sync selected decisions and patterns back into your IDE memory files if you use them.

Example commands:

```bash
python3 cli.py init
python3 cli.py extract --all
python3 cli.py recent --days 7
python3 cli.py entities
python3 cli.py decisions
python3 cli.py related Redis
python3 cli.py sync
```

## Architecture

```text
┌────────┐ ┌────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐ ┌────────┐
│ Devin  │ │ Cursor │ │ VS Code  │ │ Aider  │ │ Claude   │ │  Git   │
│        │ │        │ │ Copilot  │ │        │ │ CLI/Web  │ │        │
└───┬────┘ └───┬────┘ └────┬─────┘ └───┬────┘ └────┬─────┘ └───┬────┘
    │          │           │           │           │           │
    └──────────┼───────────┼───────────┼───────────┼───────────┘
               │           │           │
              ┌────────┴────────┐
              │   EXTRACTORS    │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │ SQLite + vec    │
              │ (local)         │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │  INTELLIGENCE   │
              │  - Entities     │
              │  - Summarize    │
              │  - Consolidate  │
              └────────┬────────┘
                       │
              ┌────────┴────────┐
              │  CLI / Agent    │
              └─────────────────┘
```

The design is intentionally simple: extract local traces, normalize them into a common schema, and make them queryable from a CLI or agent workflow.

## Database Schema

The SQLite database lives at `~/.dev-memory/memory.db`.

| Table | Purpose |
|-------|---------|
| `sessions` | Raw sessions from all tools.  |
| `turns` | Individual conversation turns.  |
| `entities` | Extracted concepts with mention counts.  |
| `entity_links` | Co-occurrence relationships between concepts.  |
| `decisions` | Architectural or implementation decisions.  |
| `patterns` | Repeated fixes, conventions, and refactors.  |
| `file_activity` | File changes across sessions and commits.  |
| `session_embeddings` | Semantic vectors for retrieval.  |

This schema is meant to support both simple search and richer context reconstruction: not just “find me the chat,” but “show the decision, related files, and nearby entities.”

## Commands

| Command | Description |
|---------|-------------|
| `init` | Create the local database.  |
| `extract --all` | Pull from all supported tools.  |
| `extract --devin-local` | Extract from Devin.  |
| `extract --cursor` | Extract from Cursor IDE.  |
| `extract --vscode-copilot` | Extract from VS Code + Copilot.  |
| `extract --aider` | Extract from Aider CLI.  |
| `extract --claude` | Extract from Claude CLI.  |
| `extract --git` | Extract from Git history.  |
| `extract --ollama` | Extract Ollama model inventory + chats.  |
| `search <query>` | Full-text search across all tools.  |
| `recent --days N` | Show recent sessions.  |
| `entities` | Show extracted concepts.  |
| `decisions` | Show active decisions.  |
| `stats` | Show database statistics.  |
| `related <entity>` | Show entity relationship graph.  |
| `sync [--dry-run]` | Sync to memory banks.  |
| `import-web [--file]` | Import Claude Web exports.  |

## Claude Web Integration

DevTrail can import exported Claude.ai chats into the same memory store used for local IDE and CLI traces.

```bash
# Import one export
python3 cli.py import-web --file ~/Downloads/chat_export.json

# Or scan Downloads for all exports
python3 cli.py import-web --all
```

Imported chats preserve full conversation turns and can contribute to entities, decisions, patterns, and cross-links with other sessions.

## Memory Bank Bridge

If you already use IDE memory files, DevTrail can write selected information back out so other tools start with better context.

```bash
# Sync decisions and patterns into memory files
python3 cli.py sync

# Preview without writing
python3 cli.py sync --dry-run
```

Current sync targets:
- Decisions → `.cascade/memory/decisions.md`
- Patterns → `.cascade/memory/patterns.md`
- Progress → `.claude/memory/progress.md`

## MCP Server (Auto-Extracting)

DevTrail now exposes its core capabilities as an MCP server. This means your IDE agent (Claude Code, Devin, Cursor, etc.) can query your cross-tool memory directly — no manual `extract` step required.

**What changes:**
- The MCP server auto-extracts from all available sources on startup (if data is >24h old).
- Your agent can call `devtrail_search`, `devtrail_decisions`, `devtrail_recent`, and more as native tools.
- At the end of a session, your agent can call `devtrail_capture_session` to push the conversation into DevTrail immediately.

**Setup:**

Add to your IDE's MCP settings:

```json
{
  "mcpServers": {
    "devtrail": {
      "command": "python3",
      "args": ["/path/to/DevTrail/mcp_server.py"]
    }
  }
}
```

For Claude Code (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "devtrail": {
      "command": "python3",
      "args": ["/path/to/DevTrail/mcp_server.py"]
    }
  }
}
```

For Windsurf/Cascade, add to your user settings under `mcpServers`.

**Available MCP Tools:**

| Tool | Purpose |
|------|---------|
| `devtrail_search` | Search sessions, decisions, and context (supports workspace + importance filters) |
| `devtrail_recent` | Recent sessions across tools |
| `devtrail_decisions` | Active architectural decisions |
| `devtrail_patterns` | Learned conventions and fixes |
| `devtrail_entities` | Extracted libraries, systems, technologies |
| `devtrail_stats` | Database stats, importance distribution, workspace breakdown |
| `devtrail_related` | Related concepts in the entity graph |
| `devtrail_importance` | High-signal sessions only (score 3-5) |
| `devtrail_workspaces` | List departments and session counts |
| `devtrail_compact_state` | TOON token-optimized snapshot for context reinjection |
| `devtrail_extract` | Force extraction from all sources |
| `devtrail_sync` | Sync to IDE memory banks |
| `devtrail_capture_session` | Push current session into memory |
| `devtrail_project_brain` | Read project brain docs |
| `devtrail_refine` | Sync patterns/decisions into project's self-improvement lessons file |

**How it works:**

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐
│  Agent   │────▶│ DevTrail MCP │────▶│  SQLite DB   │
│  (IDE)   │◀────│   Server     │◀────│  + vec       │
└──────────┘     └──────────────┘     └──────────────┘
                        │
                        ▼ auto-extract on startup
                 ┌──────────────┐
                 │ Devin/Cursor │
                 │ Claude/Git   │
                 └──────────────┘
```

## Self-Improvement Bridge (Cross-Session Learning)

DevTrail can feed its extracted intelligence (patterns, decisions, high-importance sessions) back into a project's self-improvement file — a `lessons.json` that accumulates reusable lessons across sessions and tools.

**How it works:**

```
Session ends (any tool)
  → DevTrail extracts patterns, decisions, sessions
  → self_improvement.py merges them into the project's lessons.json
  → Next session starts → lessons.json is loaded into context
  → Agent avoids repeating failures, follows established patterns
```

The lessons file is tool-agnostic. DevTrail auto-detects it in these locations (checked in order):

- `.devin/lessons.json` (Devin)
- `.claude/lessons.json` (Claude Code)
- `.cursor/lessons.json` (Cursor)
- `.agents/lessons.json` (generic)

**Lesson format:**

```json
{
  "lessons": [
    {
      "id": "l_pattern_...",
      "content": "[fix] Embedding generation: replace embed() function",
      "category": "pattern | decision | failure_avoidance | session_summary",
      "source": "devtrail_sync_<timestamp>",
      "created": 1786302708,
      "times_reinforced": 3
    }
  ],
  "last_updated": 1786302708
}
```

Repeated lessons (same pattern seen across sessions) are deduplicated and their `times_reinforced` counter increments — so frequently recurring patterns rise to the top.

**Usage:**

```bash
# Sync DevTrail intelligence into a project's lessons file
python3 memory/self_improvement.py --sync-lessons \
  --project-dir /path/to/project

# Preview without writing
python3 memory/self_improvement.py --sync-lessons \
  --project-dir /path/to/project --dry-run

# Show current lessons
python3 memory/self_improvement.py --show-lessons \
  --lessons-file /path/to/project/.devin/lessons.json
```

**Via MCP (from your IDE agent):**

Call `devtrail_refine` with `project_dir` to sync lessons programmatically. The agent can call this at session end or after extraction.

**Via session-end hook (automatic):**

The `hooks/session-end-extract.sh` script runs DevTrail extraction + lesson sync automatically when a session ends. Install it in your tool's hook system:

```json
// Devin (.devin/hooks.v1.json)
{
  "SessionEnd": [
    { "matcher": "", "hooks": [
      { "type": "command",
        "command": "bash /path/to/DevTrail/hooks/session-end-extract.sh",
        "timeout": 30 }
    ]}
  ]
}
```

For Claude CLI, source it in your shell:

```bash
source /path/to/DevTrail/hooks/session-end-extract.sh
```

For other tools, run it manually or via a post-session script:

```bash
bash /path/to/DevTrail/hooks/session-end-extract.sh /path/to/project
```

## Project Brains

DevTrail can host a private local “project brain” for each repository. These are designed to stay out of public repos by default because they often include private decisions, risks, notes, and planning artifacts.

Each project can include:
- `project-map.md` — architecture, stack, modules
- `decision-log.md` — why things are the way they are
- `open-threads.md` — unresolved issues and risks
- `release/` — release briefs with scope and acceptance criteria
- `docs-index.json` — indexed docs with freshness tags
- `repo-facts.json` — machine-readable repo stats
- `prompts/` — reusable agent prompts for planning

Project brains live in `projects/<name>/` and are **gitignored by default** — they contain private decisions, risks, and release plans that should never be committed to a public repo. Store them locally or in a private repo.

**To use a project brain:**

```bash
projects/<name>/
├── project-map.md
├── decision-log.md
├── open-threads.md
├── docs-index.json
├── repo-facts.json
├── release/
└── prompts/
```

To create one:

```bash
mkdir -p projects/<name>/{release,prompts}
# create project-map.md, decision-log.md, open-threads.md
```

## Claude CLI Hook

DevTrail can auto-extract when you exit Claude CLI.

```bash
# Add to ~/.bashrc or ~/.zshrc
source /path/to/dev-memory/hooks/claude-cli-exit.sh

# Wrapper now auto-extracts on exit
claude
# ... work ...
/exit
```

Manual options:

```bash
claude-extract
claude-quick-extract
```

## Adding New Tool Support

The extractor model is intentionally simple so new tool integrations are cheap to add.

1. Create an extractor in `memory/extractors/`.
2. Register it in `__init__.py`.
3. Add a CLI flag.
4. Call it from `cmd_extract()`.

Minimal example:

```python
from .base import BaseExtractor

class MyIDEExtractor(BaseExtractor):
    TOOL_NAME = "my_ide"
    DISPLAY_NAME = "My IDE"

    def is_available(self) -> bool:
        return True

    def extract(self, limit=None):
        sessions = []
        return sessions
```

The goal is to normalize a new source into the same core concepts so search, sync, and downstream memory tools work automatically.

## Best Fit

DevTrail is a strong fit if you:
- switch between multiple models or interfaces,
- use Git as part of your context trail,
- want to preserve reasoning outside a single assistant session,
- already use memory files but want something underneath them,
- want local-first storage and search.

It is a weaker fit if you:
- stay inside one assistant all day,
- do not care about history after the current session,
- already have a workflow that makes cross-tool context irrelevant.

## Roadmap

Planned or proposed work includes:
- Zed IDE support
- JetBrains IDE support
- Codeium support
- Supermaven support
- Continue.dev support
- Real embedding model integration
- Consolidation pipeline
- ~~MCP server for Claude CLI~~ ✅ Done
- Automatic context injection for any IDE
- Web UI for browsing memory
- Cross-project entity linking

## License

MIT.
