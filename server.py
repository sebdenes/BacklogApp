"""Cortex — Personal AI operating system.

Endpoints:
    GET  /                          → Redirect to /static/backlog.html
    GET  /api/health                → Health check
    GET  /api/backlog               → Read backlog data
    POST /api/backlog               → Write backlog data
    GET  /api/backlog/inbox         → Fetch pending inbox items (pull-and-ack)
    POST /api/backlog/inbox         → Receive items from webhooks / Power Automate
    GET  /api/meetings              → List all meetings
    POST /api/meetings              → Save full meetings array
    GET  /api/meetings/{id}         → Get single meeting
    DELETE /api/meetings/{id}       → Delete meeting
    POST /api/meetings/{id}/extract → Extract action items via Claude Code CLI
    POST /api/meetings/inbox        → Webhook for external meeting transcripts
    GET  /static/*                  → Serve frontend assets
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ── Configuration ─────────────────────────────────────────────────────────────

APP_VERSION = os.environ.get("APP_VERSION", "dev")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
BACKLOG_FILE = DATA_DIR / "backlog.json"
INBOX_FILE = DATA_DIR / "backlog_inbox.json"
MEETINGS_FILE = DATA_DIR / "meetings.json"

API_KEY: str | None = os.environ.get("BACKLOG_API_KEY")
WEBHOOK_SECRET: str | None = os.environ.get("BACKLOG_WEBHOOK_SECRET")

ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:8000"
).split(",")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cortex")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Cortex",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_auth(request: Request) -> None:
    """Check API key or webhook secret. No-op in dev mode (no key set)."""
    if not API_KEY:
        return  # dev mode — no auth
    api_key = request.headers.get("X-API-Key", "")
    auth = request.headers.get("Authorization", "")
    bearer_ok = WEBHOOK_SECRET and auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], WEBHOOK_SECRET)
    key_ok = secrets.compare_digest(api_key, API_KEY) if api_key else False
    if not (bearer_ok or key_ok):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _load_inbox() -> list[dict]:
    if INBOX_FILE.exists():
        try:
            return json.loads(INBOX_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _atomic_write(path: Path, data: str) -> None:
    """Write data to file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data.encode())
        os.close(fd)
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _save_inbox(items: list[dict]) -> None:
    _atomic_write(INBOX_FILE, json.dumps(items, indent=2))


def _load_meetings() -> list[dict]:
    if MEETINGS_FILE.exists():
        try:
            data = json.loads(MEETINGS_FILE.read_text())
            return data.get("meetings", []) if isinstance(data, dict) else data
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_meetings(meetings: list[dict]) -> None:
    _atomic_write(MEETINGS_FILE, json.dumps({"meetings": meetings}, indent=2))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse("/static/backlog.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


# ── Classify (Claude Code CLI) ────────────────────────────────────────────────

@app.post("/api/classify")
async def classify_text(request: Request):
    """Classify a note into a backlog item using Claude Code CLI."""
    _require_auth(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text provided")

    if not shutil.which("claude"):
        raise HTTPException(status_code=503, detail="Claude CLI not available")

    prompt = (
        "Classify this note into a backlog item. Return ONLY valid JSON with keys: "
        "title (concise, max 80 chars), description (full context), "
        "priority (p1=urgent, p2=important, p3=later), tags (1-3 relevant tags). "
        "Note: " + text
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)

        if proc.returncode != 0:
            log.error("Classify failed (rc=%d): %s", proc.returncode, stderr.decode()[:500])
            raise HTTPException(status_code=502, detail="Classification failed")

        raw = json.loads(stdout.decode())
        result_text = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result_text, str):
            t = result_text.strip()
            if t.startswith("```"):
                t = t.split("\n", 1)[1] if "\n" in t else t[3:]
            if t.endswith("```"):
                t = t[:-3]
            return json.loads(t.strip())
        return result_text

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Classification timed out")
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Failed to parse classification result")


@app.post("/api/transcribe")
async def transcribe_audio(request: Request, file: UploadFile = File(...)):
    """Transcribe an audio file using Claude Code CLI."""
    _require_auth(request)
    if not shutil.which("claude"):
        raise HTTPException(status_code=503, detail="Claude CLI not available")

    # Save uploaded file
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "Transcribe this audio file. Return ONLY the transcribed text, nothing else.",
            "--file", tmp_path, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)

        if proc.returncode != 0:
            log.error("Transcribe failed: %s", stderr.decode()[:500])
            raise HTTPException(status_code=502, detail="Transcription failed")

        raw = json.loads(stdout.decode())
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        text = str(result).strip()
        # Strip markdown fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return {"text": text.strip()}

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Transcription timed out")
    finally:
        os.unlink(tmp_path)


@app.post("/api/describe-image")
async def describe_image(request: Request, file: UploadFile = File(...)):
    """Describe an image using Claude Code CLI. Returns classification JSON."""
    _require_auth(request)
    if not shutil.which("claude"):
        raise HTTPException(status_code=503, detail="Claude CLI not available")

    suffix = Path(file.filename or "image.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        prompt = (
            "Describe this image for a task/note capture. Return ONLY valid JSON with keys: "
            "title (concise, max 80 chars), description (what you see and any action items), "
            "tags (1-3 relevant tags)."
        )
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--file", tmp_path, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)

        if proc.returncode != 0:
            log.error("Describe image failed: %s", stderr.decode()[:500])
            raise HTTPException(status_code=502, detail="Image description failed")

        raw = json.loads(stdout.decode())
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result, str):
            t = result.strip()
            if t.startswith("```"):
                t = t.split("\n", 1)[1] if "\n" in t else t[3:]
            if t.endswith("```"):
                t = t[:-3]
            return json.loads(t.strip())
        return result

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Image description timed out")
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Failed to parse image description")
    finally:
        os.unlink(tmp_path)


# ── Backlog CRUD ──────────────────────────────────────────────────────────────

@app.get("/api/backlog")
async def get_backlog(request: Request):
    """Read full backlog data from disk."""
    _require_auth(request)
    if BACKLOG_FILE.exists():
        return json.loads(BACKLOG_FILE.read_text())
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.post("/api/backlog")
async def save_backlog(request: Request):
    """Write full backlog data to disk."""
    _require_auth(request)
    body = await request.json()
    _atomic_write(BACKLOG_FILE, json.dumps(body, indent=2))
    return {"status": "ok"}


@app.get("/api/items/search")
async def search_items(request: Request, q: str = ""):
    """Search items by title across all projects. Returns matches with location."""
    _require_auth(request)
    if not BACKLOG_FILE.exists():
        return JSONResponse({"items": [], "count": 0})
    projects = json.loads(BACKLOG_FILE.read_text())
    if isinstance(projects, dict):
        projects = projects.get("projects", [])
    q_lower = q.lower()
    results = []
    for p in projects:
        for lane in p.get("lanes", []):
            for item in lane.get("items", []):
                if q_lower in (item.get("title") or "").lower():
                    results.append({
                        **item,
                        "projectId": p["id"],
                        "projectName": p["name"],
                        "laneId": lane["id"],
                        "laneTitle": lane["title"],
                    })
    return JSONResponse({"items": results, "count": len(results)})


@app.patch("/api/items/{item_id}/move")
async def move_item(item_id: str, request: Request):
    """Move an item to a different lane. Body: {laneId, priority?}"""
    _require_auth(request)
    if not BACKLOG_FILE.exists():
        raise HTTPException(status_code=404, detail="No backlog data")
    body = await request.json()
    target_lane = body.get("laneId", "")
    new_priority = body.get("priority")

    projects = json.loads(BACKLOG_FILE.read_text())
    if isinstance(projects, dict):
        projects = projects.get("projects", [])

    # Find and move the item
    found = None
    source_lane = None
    target_project = None
    for p in projects:
        for lane in p.get("lanes", []):
            for item in lane.get("items", []):
                if item.get("id") == item_id:
                    found = item
                    source_lane = lane
                    target_project = p
                    break
            if found:
                break
        if found:
            break

    if not found:
        raise HTTPException(status_code=404, detail="Item not found")

    # Update priority if provided
    if new_priority:
        found["priority"] = new_priority

    # Move to target lane
    if target_lane and target_lane != source_lane["id"]:
        dest = next((l for l in target_project["lanes"] if l["id"] == target_lane), None)
        if not dest:
            raise HTTPException(status_code=400, detail=f"Lane '{target_lane}' not found")
        source_lane["items"] = [i for i in source_lane["items"] if i["id"] != item_id]
        dest["items"].append(found)

    _atomic_write(BACKLOG_FILE, json.dumps(projects, indent=2))
    return {"status": "ok", "item": found["title"], "lane": target_lane or source_lane["id"]}


# ── Inbox (webhook receiver + poll endpoint) ──────────────────────────────────

@app.post("/api/backlog/inbox")
async def inbox_post(request: Request):
    """Receive items from external sources (Power Automate, webhooks, etc.).

    Auth: Bearer token via BACKLOG_WEBHOOK_SECRET.
    When secret is not set, the endpoint is disabled (404).
    """
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Webhook not configured")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    body = await request.json()
    new_items = body.get("items", [])
    if not new_items:
        raise HTTPException(status_code=400, detail="No items provided")

    inbox = _load_inbox()
    added = 0
    for item in new_items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        inbox.append({
            "id": f"inbox-{secrets.token_hex(6)}",
            "title": title,
            "description": (item.get("description") or "").strip(),
            "source": item.get("source", "webhook"),
            "meeting_subject": item.get("meeting_subject", ""),
            "timestamp": item.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "priority": item.get("priority", ""),
            "tags": item.get("tags", []),
            "status": "pending",
        })
        added += 1

    _save_inbox(inbox)
    log.info("Inbox: received %d items (total pending: %d)", added, len(inbox))
    return JSONResponse({"ok": True, "added": added, "total_pending": len(inbox)})


@app.get("/api/backlog/inbox")
async def inbox_get(request: Request):
    """Fetch pending inbox items (pull-and-ack pattern).

    Query: ?ack=true to clear items after reading.
    """
    _require_auth(request)
    ack = request.query_params.get("ack", "false").lower() == "true"
    inbox = _load_inbox()

    if ack and inbox:
        _save_inbox([])
        log.info("Inbox: acknowledged and cleared %d items", len(inbox))

    return JSONResponse({"items": inbox, "count": len(inbox)})


# ── Meetings ─────────────────────────────────────────────────────────────────

@app.get("/api/meetings")
async def get_meetings(request: Request):
    """List all meetings."""
    _require_auth(request)
    meetings = _load_meetings()
    return JSONResponse({"meetings": meetings, "count": len(meetings)})


@app.post("/api/meetings")
async def save_meetings(request: Request):
    """Save full meetings array (matches backlog sync pattern)."""
    _require_auth(request)
    body = await request.json()
    meetings = body.get("meetings", body) if isinstance(body, dict) else body
    _save_meetings(meetings)
    return {"status": "ok"}


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, request: Request):
    """Get a single meeting by ID."""
    _require_auth(request)
    meetings = _load_meetings()
    meeting = next((m for m in meetings if m.get("id") == meeting_id), None)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str, request: Request):
    """Delete a meeting by ID."""
    _require_auth(request)
    meetings = _load_meetings()
    before = len(meetings)
    meetings = [m for m in meetings if m.get("id") != meeting_id]
    if len(meetings) == before:
        raise HTTPException(status_code=404, detail="Meeting not found")
    _save_meetings(meetings)
    return {"status": "ok"}


_EXTRACT_PROMPT_TEMPLATE = """\
You are an expert at extracting action items from meeting transcripts.
You understand implicit commitments, speaker attribution, and context.

Meeting: "{title}"
Date: {date}

Transcript:
\"\"\"
{transcript}
\"\"\"

Extract and return ONLY valid JSON (no markdown fences, no explanation):
{{
  "summary": ["bullet 1", "bullet 2", "bullet 3"],
  "decisions": ["Decision that was made"],
  "openQuestions": ["Unresolved question"],
  "actionItems": [
    {{
      "title": "concise action item title",
      "desc": "context from the discussion",
      "assignee": "person responsible (empty string if unknown)",
      "priority": "p1 or p2 or p3",
      "dueHint": "YYYY-MM-DD or empty string"
    }}
  ]
}}

Rules:
- Only extract ACTIONABLE items (not discussion points or FYIs)
- Each item must be independently understandable
- Include relevant context from the discussion in desc
- Attribute assignee from speaker context ("I'll do X" = speaker, "Bob should" = Bob)
- Infer priority: urgent/blocker/critical = p1, should/next/important = p2, nice-to-have/later = p3
- Convert relative dates to absolute (today is {today})
- Decisions = things that were resolved/agreed upon
- Open questions = things explicitly left unresolved
- Summary = 3-5 bullet points covering the key topics discussed
- If no clear action items exist, return empty arrays
- Maximum 20 action items per meeting\
"""


@app.post("/api/meetings/{meeting_id}/extract")
async def extract_action_items(meeting_id: str, request: Request):
    """Extract action items from a meeting transcript using Claude Code CLI."""
    _require_auth(request)

    meetings = _load_meetings()
    meeting = next((m for m in meetings if m.get("id") == meeting_id), None)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    transcript = (meeting.get("transcript") or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="No transcript to process")

    if not shutil.which("claude"):
        raise HTTPException(status_code=503, detail="Claude Code CLI not found on server")

    # Build prompt
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _EXTRACT_PROMPT_TEMPLATE.format(
        title=meeting.get("title", "Untitled"),
        date=meeting.get("date", today),
        transcript=transcript,
        today=today,
    )

    # Update status to processing
    meeting["status"] = "processing"
    _save_meetings(meetings)

    try:
        # Write prompt to temp file to avoid shell escaping issues
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            os.unlink(prompt_file)
            meeting["status"] = "failed"
            _save_meetings(meetings)
            log.error("Claude CLI timed out after 120s")
            raise HTTPException(status_code=504, detail="Extraction timed out — Claude Code took too long")

        os.unlink(prompt_file)

        if proc.returncode != 0:
            meeting["status"] = "failed"
            _save_meetings(meetings)
            log.error("Claude CLI failed: %s", stderr.decode())
            raise HTTPException(status_code=502, detail="Claude Code extraction failed")

        # Parse Claude Code JSON output
        raw = json.loads(stdout.decode())
        # Claude Code --output-format json wraps the result; extract the text
        result_text = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result_text, str):
            # Strip markdown fences if present
            text = result_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            extraction = json.loads(text.strip())
        else:
            extraction = result_text

        # Update meeting with results
        meeting["status"] = "processed"
        meeting["summary"] = extraction.get("summary", [])
        meeting["decisions"] = extraction.get("decisions", [])
        meeting["openQuestions"] = extraction.get("openQuestions", [])
        meeting["extractionRaw"] = extraction
        _save_meetings(meetings)

        return JSONResponse(extraction)

    except json.JSONDecodeError as e:
        meeting["status"] = "failed"
        _save_meetings(meetings)
        log.error("Failed to parse extraction result: %s", e)
        raise HTTPException(status_code=502, detail="Failed to parse extraction result")
    except Exception as e:
        if not isinstance(e, HTTPException):
            meeting["status"] = "failed"
            _save_meetings(meetings)
            log.error("Extraction error: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
        raise


@app.post("/api/meetings/inbox")
async def meetings_inbox(request: Request):
    """Receive meeting transcripts from external sources via webhook."""
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="Webhook not configured")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not secrets.compare_digest(auth[7:], WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    body = await request.json()
    meeting = {
        "id": f"mtg-{secrets.token_hex(6)}",
        "title": body.get("title", "Untitled Meeting"),
        "date": body.get("date", datetime.now(timezone.utc).isoformat()),
        "attendees": body.get("attendees", []),
        "status": "pending",
        "transcript": body.get("transcript", ""),
        "transcriptSource": "webhook",
        "summary": [],
        "decisions": [],
        "openQuestions": [],
        "actionItemIds": [],
        "extractionRaw": {},
        "tags": body.get("tags", []),
        "created": datetime.now(timezone.utc).isoformat(),
    }

    meetings = _load_meetings()
    meetings.append(meeting)
    _save_meetings(meetings)
    log.info("Meetings inbox: received '%s'", meeting["title"])
    return JSONResponse({"ok": True, "meeting_id": meeting["id"]})


# ── Static files (must be last) ──────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
