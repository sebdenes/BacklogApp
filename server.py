"""BacklogApp — Lightweight API server for the AI-powered kanban board.

Endpoints:
    GET  /                      → Redirect to /static/backlog.html
    GET  /api/health            → Health check
    GET  /api/backlog           → Read backlog data
    POST /api/backlog           → Write backlog data
    GET  /api/backlog/inbox     → Fetch pending inbox items (pull-and-ack)
    POST /api/backlog/inbox     → Receive items from webhooks / Power Automate
    GET  /static/*              → Serve frontend assets
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ── Configuration ─────────────────────────────────────────────────────────────

APP_VERSION = os.environ.get("APP_VERSION", "dev")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
BACKLOG_FILE = DATA_DIR / "backlog.json"
INBOX_FILE = DATA_DIR / "backlog_inbox.json"

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
log = logging.getLogger("backlog")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="BacklogApp",
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
    bearer_ok = WEBHOOK_SECRET and auth == f"Bearer {WEBHOOK_SECRET}"
    key_ok = api_key == API_KEY
    if not (bearer_ok or key_ok):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _load_inbox() -> list[dict]:
    if INBOX_FILE.exists():
        try:
            return json.loads(INBOX_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_inbox(items: list[dict]) -> None:
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INBOX_FILE.write_text(json.dumps(items, indent=2))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse("/static/backlog.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


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
    BACKLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKLOG_FILE.write_text(json.dumps(body, indent=2))
    return {"status": "ok"}


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
    if not auth.startswith("Bearer ") or auth[7:] != WEBHOOK_SECRET:
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


# ── Static files (must be last) ──────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
