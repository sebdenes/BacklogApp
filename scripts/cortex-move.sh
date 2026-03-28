#!/bin/bash
# Move a Cortex item between lanes
# Usage: cortex-move.sh <search-query> <lane-id> [priority]
# Lanes: backlog, in-progress, done

CORTEX_API="${CORTEX_API_URL:-https://cortex.athlai.me}"
QUERY="$1"
LANE="$2"
PRIORITY="$3"

if [ -z "$QUERY" ] || [ -z "$LANE" ]; then
  echo "Usage: cortex-move.sh <search-query> <lane-id> [priority]"
  exit 1
fi

# Search for the item
RESULT=$(curl -s "${CORTEX_API}/api/items/search?q=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$QUERY'))")")
COUNT=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))")

if [ "$COUNT" = "0" ]; then
  echo "No items found matching: $QUERY"
  exit 1
fi

# Get first match
ITEM_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['id'])")
ITEM_TITLE=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['title'])")

# Build move payload
if [ -n "$PRIORITY" ]; then
  PAYLOAD="{\"laneId\": \"$LANE\", \"priority\": \"$PRIORITY\"}"
else
  PAYLOAD="{\"laneId\": \"$LANE\"}"
fi

# Move it
MOVE_RESULT=$(curl -s -X PATCH "${CORTEX_API}/api/items/${ITEM_ID}/move" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

STATUS=$(echo "$MOVE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))")

if [ "$STATUS" = "ok" ]; then
  echo "Moved '$ITEM_TITLE' → $LANE"
else
  echo "Failed to move item: $MOVE_RESULT"
  exit 1
fi
