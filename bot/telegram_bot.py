"""Cortex Telegram Bot — capture text, voice, and photos on the go.

Messages are classified by Claude Code CLI and posted to the Cortex API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

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


async def _classify_with_claude(text: str) -> dict:
    """Call Claude Code CLI to classify a capture into a backlog item."""
    if not shutil.which("claude"):
        log.warning("Claude CLI not found, using raw text")
        return {"title": text[:120], "description": text, "priority": "", "tags": []}

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
            log.error("Claude classify failed (rc=%d): %s", proc.returncode, stderr.decode()[:500])
            return {"title": text[:120], "description": text, "priority": "", "tags": []}

        raw = json.loads(stdout.decode())
        result_text = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result_text, str):
            t = result_text.strip()
            # Strip markdown code fences (```json ... ``` or ``` ... ```)
            if t.startswith("```"):
                # Remove opening fence line
                t = t.split("\n", 1)[1] if "\n" in t else t[3:]
            if t.endswith("```"):
                t = t[:-3]
            t = t.strip()
            parsed = json.loads(t)
            log.info("Classified: %s", json.dumps(parsed, ensure_ascii=False)[:200])
            return parsed
        return result_text

    except asyncio.TimeoutError:
        log.error("Classification timed out")
        return {"title": text[:120], "description": text, "priority": "", "tags": []}
    except json.JSONDecodeError as e:
        log.error("Classification JSON parse failed: %s — raw: %s", e, result_text[:200] if 'result_text' in dir() else "n/a")
        return {"title": text[:120], "description": text, "priority": "", "tags": []}
    except Exception as e:
        log.error("Classification failed: %s", e)
        return {"title": text[:120], "description": text, "priority": "", "tags": []}


async def _transcribe_voice(file_path: str) -> str:
    """Transcribe a voice message using Claude Code CLI."""
    if not shutil.which("claude"):
        return "[Voice message — Claude CLI not available for transcription]"

    prompt = f"Transcribe this audio file and return only the transcribed text, nothing else."

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--file", file_path, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)

        if proc.returncode != 0:
            log.error("Transcription failed: %s", stderr.decode()[:200])
            return "[Voice message — transcription failed]"

        raw = json.loads(stdout.decode())
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        return str(result).strip()

    except Exception as e:
        log.error("Transcription error: %s", e)
        return "[Voice message — transcription error]"


async def _describe_photo(file_path: str, caption: str = "") -> str:
    """Describe a photo using Claude Code CLI."""
    if not shutil.which("claude"):
        return caption or "[Photo — Claude CLI not available]"

    context = f" The user added this caption: \"{caption}\"" if caption else ""
    prompt = f"Describe what's in this image in 1-2 sentences for a task/note capture.{context} Then suggest a short title (max 80 chars) and any action items. Return ONLY JSON: {{\"title\": \"...\", \"description\": \"...\", \"tags\": [\"...\"]}}"

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--file", file_path, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

        if proc.returncode != 0:
            return caption or "[Photo — description failed]"

        raw = json.loads(stdout.decode())
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result, str):
            t = result.strip()
            if t.startswith("```"):
                t = t.split("\n", 1)[1] if "\n" in t else t[3:]
            if t.endswith("```"):
                t = t[:-3]
            return t.strip()
        return json.dumps(result)

    except Exception as e:
        log.error("Photo description error: %s", e)
        return caption or "[Photo — description error]"


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

    classified = await _classify_with_claude(text)

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
        result_text = await _describe_photo(tmp_path, caption)

        # Try to parse as JSON from Claude
        try:
            result = json.loads(result_text)
            title = result.get("title", caption or "Photo capture")
            desc = result.get("description", caption)
            tags = result.get("tags", [])
        except (json.JSONDecodeError, TypeError):
            title = caption or "Photo capture"
            desc = result_text
            tags = []

        item = {
            "title": title[:120],
            "description": desc,
            "priority": "p3",
            "tags": tags + ["telegram", "photo"],
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
