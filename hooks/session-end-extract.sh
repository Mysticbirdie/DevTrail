#!/bin/bash
# Session-End Extract Hook — tool-agnostic auto-extract + lesson sync
#
# Runs after any AI coding session ends (Devin, Claude, Cursor, etc.).
# It:
#   1. Triggers DevTrail extraction from all available sources
#   2. Syncs extracted patterns/decisions into the project's
#      .devin/lessons.json (or equivalent self-improvement file)
#
# Installation:
#
#   Devin (project-level .devin/hooks.v1.json):
#     "SessionEnd": [
#       { "matcher": "", "hooks": [
#         { "type": "command",
#           "command": "bash /path/to/DevTrail/hooks/session-end-extract.sh",
#           "timeout": 30 }
#       ]}
#     ]
#
#   Claude CLI (add to ~/.bashrc or ~/.zshrc):
#     source /path/to/DevTrail/hooks/session-end-extract.sh
#     # Then use the 'claude' wrapper function
#
#   Cursor / other IDEs: run manually or via a post-session script:
#     bash /path/to/DevTrail/hooks/session-end-extract.sh /path/to/project
#
# Environment:
#   DEVIN_PROJECT_DIR — set by Devin to the project root (optional)
#   $1 (arg)          — project dir for manual invocation
#   DEVTRAIL_REPO     — optional: path to DevTrail checkout (auto-detected)

set -euo pipefail

# --- Resolve project directory ------------------------------------------------

PROJECT_DIR="${1:-${DEVIN_PROJECT_DIR:-$(pwd)}}"

# --- Locate DevTrail -----------------------------------------------------------

DEVTRAIL_REPO="${DEVTRAIL_REPO:-}"

if [ -z "$DEVTRAIL_REPO" ]; then
    for candidate in \
        "$HOME/CascadeProjects/DevTrail" \
        "$HOME/dev/DevTrail" \
        "$HOME/DevTrail" \
        "$(pwd)/DevTrail"; do
        if [ -f "$candidate/cli.py" ]; then
            DEVTRAIL_REPO="$candidate"
            break
        fi
    done
fi

if [ -z "$DEVTRAIL_REPO" ] || [ ! -f "$DEVTRAIL_REPO/cli.py" ]; then
    exit 0  # DevTrail not found — silently exit
fi

# --- Run extraction from all sources ------------------------------------------

python3 "$DEVTRAIL_REPO/cli.py" extract --all --limit 50 2>/dev/null || true

# --- Sync lessons to project (if it has a self-improvement file) ---------------

# Check for common self-improvement file locations across tools
LESSONS_FILE=""

for candidate in \
    "$PROJECT_DIR/.devin/lessons.json" \
    "$PROJECT_DIR/.claude/lessons.json" \
    "$PROJECT_DIR/.cursor/lessons.json" \
    "$PROJECT_DIR/.agents/lessons.json"; do
    if [ -f "$candidate" ]; then
        LESSONS_FILE="$candidate"
        break
    fi
done

# If no lessons file exists yet but the project has a .devin/ or .claude/ dir,
# create one in the appropriate location
if [ -z "$LESSONS_FILE" ]; then
    if [ -d "$PROJECT_DIR/.devin" ]; then
        LESSONS_FILE="$PROJECT_DIR/.devin/lessons.json"
    elif [ -d "$PROJECT_DIR/.claude" ]; then
        LESSONS_FILE="$PROJECT_DIR/.claude/lessons.json"
    elif [ -d "$PROJECT_DIR/.cursor" ]; then
        LESSONS_FILE="$PROJECT_DIR/.cursor/lessons.json"
    fi
fi

if [ -n "$LESSONS_FILE" ]; then
    python3 "$DEVTRAIL_REPO/memory/self_improvement.py" \
        --sync-lessons \
        --project-dir "$PROJECT_DIR" \
        --lessons-file "$LESSONS_FILE" \
        --db "${HOME}/.dev-memory/memory.db" 2>/dev/null || true
fi

exit 0
