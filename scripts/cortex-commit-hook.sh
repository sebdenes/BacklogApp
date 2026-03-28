#!/bin/bash
# Post-commit hook: scan commit message for Cortex card references
# Moves matching cards based on keywords:
#   "fixes <title>"  / "closes <title>"  → Done
#   "starts <title>" / "wip <title>"     → In Progress
#   "refs <title>"                       → no move, just logs
#
# Install: ln -sf /path/to/cortex-commit-hook.sh .git/hooks/post-commit
# Or add to Claude Code hooks in settings.json

CORTEX_API="${CORTEX_API_URL:-https://cortex.athlai.me}"
MOVE_SCRIPT="$(dirname "$0")/cortex-move.sh"

# Get the latest commit message
MSG=$(git log -1 --pretty=%B 2>/dev/null)

if [ -z "$MSG" ]; then
  exit 0
fi

# Check for "fixes/closes <search>" patterns → move to done
for keyword in fixes closes fixed closed completes completed; do
  MATCH=$(echo "$MSG" | grep -ioP "(?<=$keyword\s).{3,80}" | head -1)
  if [ -n "$MATCH" ]; then
    echo "[Cortex] Moving '$MATCH' → done"
    "$MOVE_SCRIPT" "$MATCH" "done" "done" 2>/dev/null
  fi
done

# Check for "starts/wip <search>" patterns → move to in-progress
for keyword in starts working wip "in progress"; do
  MATCH=$(echo "$MSG" | grep -ioP "(?<=$keyword\s).{3,80}" | head -1)
  if [ -n "$MATCH" ]; then
    echo "[Cortex] Moving '$MATCH' → in-progress"
    "$MOVE_SCRIPT" "$MATCH" "in-progress" 2>/dev/null
  fi
done
