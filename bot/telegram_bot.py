"""Cortex Telegram Bot — capture text, voice, and photos on the go.

Messages are classified via the Cortex API (which uses Claude Code CLI)
and posted to the inbox.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Configuration ──

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CORTEX_API = os.environ.get("CORTEX_API_URL", "http://cortex:8000")
CORTEX_API_KEY = os.environ.get("BACKLOG_API_KEY", "")
CORTEX_WEBHOOK_SECRET = os.environ.get("BACKLOG_WEBHOOK_SECRET", "")
ALLOWED_USERS = {
    int(uid) for uid in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if uid.strip()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("cortex-bot")

# ── Helpers ──

def _auth_headers() -> dict:
    if CORTEX_WEBHOOK_SECRET:
        return {"Authorization": f"Bearer {CORTEX_WEBHOOK_SECRET}"}
    if CORTEX_API_KEY:
        return {"X-API-Key": CORTEX_API_KEY}
    return {}


def _check_user(update: Update) -> bool:
    """Return True if user is allowed (or no allowlist configured)."""
    if not ALLOWED_USERS:
        return True
    return update.effective_user and update.effective_user.id in ALLOWED_USERS


async def _classify_with_cortex(text: str) -> dict:
    """Call Cortex API /api/classify to classify a capture."""
    async with httpx.AsyncClient(timeout=90.0) as client:
        try:
            resp = await client.post(
                f"{CORTEX_API}/api/classify",
                json={"text": text},
                headers={**_auth_headers(), "Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                result = resp.json()
                log.info("Classified: %s", json.dumps(result, ensure_ascii=False)[:200])
                return result
            log.error("Classify API error: %d %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("Classify API failed: %s", e)
    return {"title": text[:120], "description": text, "priority": "", "tags": []}


async def _transcribe_voice(file_path: str) -> str:
    """Voice transcription — currently returns placeholder.
    TODO: Add server-side transcription endpoint.
    """
    return "[Voice message received — transcription coming soon]"


async def _post_to_cortex(item: dict) -> bool:
    """Post an item to the Cortex inbox API."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{CORTEX_API}/api/backlog/inbox",
                json={"items": [item]},
                headers={**_auth_headers(), "Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return True
            log.error("Cortex API error: %d %s", resp.status_code, resp.text[:200])
            return False
        except Exception as e:
            log.error("Cortex API unreachable: %s", e)
            return False


# ── Handlers ──

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_user(update):
        await update.message.reply_text("Not authorized.")
        return
    await update.message.reply_text(
        "Welcome to Cortex!\n\n"
        "Send me anything and I'll add it to your backlog:\n"
        "- Text messages — captured as notes/tasks\n"
        "- Voice messages — transcribed and captured\n"
        "- Photos — described and captured\n\n"
        "Commands:\n"
        "/start — this message\n"
        "/ping — check if bot is connected"
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_user(update):
        return
    # Check Cortex API health
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{CORTEX_API}/api/health")
            if resp.status_code == 200:
                await update.message.reply_text("Cortex is online.")
                return
    except Exception:
        pass
    await update.message.reply_text("Cortex API is unreachable.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_user(update):
        return
    text = update.message.text.strip()
    if not text:
        return

    msg = await update.message.reply_text("Classifying...")

    classified = await _classify_with_cortex(text)

    item = {
        "title": classified.get("title", text[:120]),
        "description": classified.get("description", text),
        "priority": classified.get("priority", ""),
        "tags": classified.get("tags", []) + ["telegram"],
        "source": "telegram",
    }

    ok = await _post_to_cortex(item)

    priority_emoji = {"p1": "🔴", "p2": "🟡", "p3": "🟢"}.get(item["priority"], "⚪")
    tags_str = " ".join(f"#{t}" for t in item["tags"])

    if ok:
        await msg.edit_text(
            f"Added to Cortex\n\n"
            f"{priority_emoji} {item['title']}\n"
            f"{tags_str}"
        )
    else:
        await msg.edit_text("Failed to add to Cortex. API might be down.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_user(update):
        return

    msg = await update.message.reply_text("Transcribing voice...")

    voice = update.message.voice or update.message.audio
    if not voice:
        await msg.edit_text("Could not read voice message.")
        return

    # Download voice file
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        transcript = await _transcribe_voice(tmp_path)
        await msg.edit_text(f"Transcribed: {transcript[:200]}...\n\nClassifying...")

        classified = await _classify_with_claude(transcript)

        item = {
            "title": classified.get("title", transcript[:120]),
            "description": classified.get("description", transcript),
            "priority": classified.get("priority", ""),
            "tags": classified.get("tags", []) + ["telegram", "voice"],
            "source": "telegram-voice",
        }

        ok = await _post_to_cortex(item)

        priority_emoji = {"p1": "🔴", "p2": "🟡", "p3": "🟢"}.get(item["priority"], "⚪")
        tags_str = " ".join(f"#{t}" for t in item["tags"])

        if ok:
            await msg.edit_text(
                f"Added to Cortex\n\n"
                f"{priority_emoji} {item['title']}\n"
                f"{tags_str}\n\n"
                f"Transcript: {transcript[:300]}"
            )
        else:
            await msg.edit_text("Failed to add to Cortex.")
    finally:
        os.unlink(tmp_path)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_user(update):
        return

    msg = await update.message.reply_text("Analyzing photo...")

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        await msg.edit_text("Could not read photo.")
        return

    caption = update.message.caption or ""

    # Download photo
    file = await context.bot.get_file(photo.file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        text = caption or "Photo capture"
        classified = await _classify_with_cortex(text)

        item = {
            "title": classified.get("title", text[:120]),
            "description": classified.get("description", text),
            "priority": classified.get("priority", "p3"),
            "tags": classified.get("tags", []) + ["telegram", "photo"],
            "source": "telegram-photo",
        }

        ok = await _post_to_cortex(item)

        tags_str = " ".join(f"#{t}" for t in item["tags"])

        if ok:
            await msg.edit_text(
                f"Added to Cortex\n\n"
                f"🟢 {item['title']}\n"
                f"{tags_str}\n\n"
                f"{desc[:300]}"
            )
        else:
            await msg.edit_text("Failed to add to Cortex.")
    finally:
        os.unlink(tmp_path)


# ── Main ──

def main() -> None:
    log.info("Starting Cortex Telegram bot...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot is running. Polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
