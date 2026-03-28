#!/bin/bash
# Scan a codebase for TODO/FIXME/HACK comments and sync to Cortex backlog
# Usage: cortex-scan-todos.sh <project-dir> [cortex-project-name]
#
# Skips: .venv, node_modules, .git, __pycache__, docs/reports

CORTEX_API="${CORTEX_API_URL:-https://cortex.athlai.me}"
PROJECT_DIR="${1:-.}"
PROJECT_NAME="${2:-}"

if [ -z "$PROJECT_DIR" ]; then
  echo "Usage: cortex-scan-todos.sh <project-dir> [cortex-project-name]"
  exit 1
fi

# Scan for TODOs
TODOS=$(grep -rn --include="*.py" --include="*.js" --include="*.ts" --include="*.html" --include="*.jsx" --include="*.tsx" \
  -E "(TODO|FIXME|HACK|XXX):" "$PROJECT_DIR" \
  --exclude-dir=.venv --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=docs 2>/dev/null)

if [ -z "$TODOS" ]; then
  echo "No TODOs found in $PROJECT_DIR"
  exit 0
fi

COUNT=$(echo "$TODOS" | wc -l | tr -d ' ')
echo "Found $COUNT TODOs in $PROJECT_DIR"
echo ""

# Build inbox items
ITEMS="[]"
while IFS= read -r line; do
  FILE=$(echo "$line" | cut -d: -f1 | sed "s|$PROJECT_DIR/||")
  LINENO=$(echo "$line" | cut -d: -f2)
  TEXT=$(echo "$line" | cut -d: -f3- | sed 's/.*\(TODO\|FIXME\|HACK\|XXX\):\s*//' | sed 's/^ *//' | head -c 200)
  TYPE=$(echo "$line" | grep -oE "(TODO|FIXME|HACK|XXX)" | head -1)

  # Set priority based on type
  case "$TYPE" in
    FIXME|HACK) PRIORITY="p1" ;;
    TODO) PRIORITY="p2" ;;
    XXX) PRIORITY="p3" ;;
    *) PRIORITY="p2" ;;
  esac

  TITLE=$(echo "$TEXT" | head -c 80)
  DESC="$TYPE at $FILE:$LINENO — $TEXT"

  ITEMS=$(echo "$ITEMS" | python3 -c "
import sys, json
items = json.load(sys.stdin)
items.append({
    'title': '''$TITLE''',
    'description': '''$DESC''',
    'priority': '$PRIORITY',
    'tags': ['$TYPE', 'code-scan'],
    'source': 'todo-scan'
})
json.dump(items, sys.stdout)
")

  echo "  [$PRIORITY] $TYPE: $TITLE ($FILE:$LINENO)"
done <<< "$TODOS"

# Post to Cortex inbox
echo ""
echo "Posting $COUNT items to Cortex inbox..."
RESULT=$(curl -s -X POST "${CORTEX_API}/api/backlog/inbox" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${BACKLOG_WEBHOOK_SECRET:-}" \
  -d "{\"items\": $ITEMS}")

echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Added:', d.get('added', 0))" 2>/dev/null || echo "Failed: $RESULT"
