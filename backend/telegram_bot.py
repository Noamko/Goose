import html
import io
import logging
import os
from typing import Callable

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .chat import run_chat
from .database import list_runs, list_templates, list_widgets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------

# run_id -> chat_id: which Telegram chat to notify about this run
_run_to_chat: dict[str, int] = {}

# chat_id -> (run_id, call_id): an ask_user waiting for a response
_pending_input: dict[int, tuple[str, str]] = {}

# chat_id -> {"choosing": True, "templates": [...]} or {"template_id": ..., "template_name": ...}
_awaiting_goal: dict[int, dict] = {}

# chat_id -> message list: conversation history with the meta-agent
_chat_history: dict[int, list] = {}

# ---------------------------------------------------------------------------
# Functions injected from main.py at startup
# ---------------------------------------------------------------------------

_start_run_fn: Callable | None = None
_provide_input_fn: Callable | None = None
_allowed_ids: set[str] = set()


def setup(start_run: Callable, provide_input: Callable, allowed_ids: str = ""):
    global _start_run_fn, _provide_input_fn, _allowed_ids
    _start_run_fn = start_run
    _provide_input_fn = provide_input
    _allowed_ids = {x.strip() for x in allowed_ids.split(",") if x.strip()}


# ---------------------------------------------------------------------------
# Security: only respond to allowed chat IDs
# ---------------------------------------------------------------------------

def _allowed(update: Update) -> bool:
    if not _allowed_ids:
        return True
    return str(update.effective_chat.id) in _allowed_ids


# ---------------------------------------------------------------------------
# Event hook — called from main.py's _broadcast for every run event
# ---------------------------------------------------------------------------

async def on_run_event(run_id: str, event: dict, bot):
    chat_id = _run_to_chat.get(run_id)
    if chat_id is None:
        return

    t = event.get("type")

    if t == "user_input_required":
        _pending_input[chat_id] = (run_id, event["call_id"])
        question = event.get("question", "Please provide information:")
        await bot.send_message(
            chat_id=chat_id,
            text=f"🤖 <b>Agent is asking:</b>\n\n{h(question)}",
            parse_mode="HTML",
        )

    elif t == "run_complete":
        _run_to_chat.pop(run_id, None)
        _pending_input.pop(chat_id, None)
        content = (event.get("content") or "Task completed.")[:3000]
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ <b>Done!</b>\n\n{h(content)}",
            parse_mode="HTML",
        )

    elif t == "status_change" and event.get("status") in ("failed", "cancelled"):
        _run_to_chat.pop(run_id, None)
        _pending_input.pop(chat_id, None)
        icon = "❌" if event["status"] == "failed" else "🚫"
        await bot.send_message(
            chat_id=chat_id,
            text=f"{icon} Run <b>{event['status']}</b>.",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h(text: str) -> str:
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await update.message.reply_text(
        "👋 Hey! I'm <b>Goose</b>, your personal AI assistant.\n\n"
        "• Just <b>type anything</b> to chat or ask questions\n"
        "• /agents — list your agents\n"
        "• /run — start an agent run\n"
        "• /runs — view recent runs\n"
        "• /dashboard — view dashboard widgets\n"
        "• /help — show this message\n\n"
        "Agents that ask for your input will message you here.",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await cmd_start(update, ctx)


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    templates = await list_templates()
    if not templates:
        await update.message.reply_text("No agents yet. Create one in the dashboard.")
        return
    lines = ["<b>Available agents:</b>\n"]
    for t in templates:
        pin = "★ " if t.get("pinned") else ""
        desc = t.get("description") or "No description"
        model = t.get("model") or "gpt-4o"
        lines.append(f"• {pin}<b>{h(t['name'])}</b> — {h(desc)} <i>[{h(model)}]</i>")
    lines.append("\nUse /run to start one.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    runs = await list_runs()
    if not runs:
        await update.message.reply_text("No runs yet.")
        return
    icons = {
        "completed": "✅", "running": "⚡", "failed": "❌",
        "cancelled": "🚫", "pending": "⏳", "waiting_for_user": "💬",
    }
    lines = ["<b>Recent runs:</b>\n"]
    for r in runs[:8]:
        icon = icons.get(r["status"], "•")
        goal = (r.get("user_goal") or "")[:60]
        name = r.get("template_name") or "Ad-hoc"
        lines.append(f"{icon} <b>{h(name)}</b>: {h(goal)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    widgets = await list_widgets()
    if not widgets:
        await update.message.reply_text("No dashboard widgets yet.")
        return
    parts = [f"<b>Dashboard — {len(widgets)} widget(s):</b>"]
    for w in widgets:
        d = w.get("data", {})
        wt = w["widget_type"]
        parts.append(f"\n<b>{h(w['title'])}</b>")
        if wt == "metric":
            parts.append(f"  {h(str(d.get('value', '')))} {h(d.get('label', ''))}")
        elif wt == "list":
            for item in (d.get("items") or [])[:6]:
                parts.append(f"  • {h(str(item))}")
        elif wt == "text":
            parts.append(f"  {h(str(d.get('content', ''))[:300])}")
        elif wt == "table":
            cols = d.get("columns", [])
            rows = d.get("rows", [])[:4]
            if cols:
                parts.append(f"  <i>{' | '.join(h(c) for c in cols)}</i>")
            for row in rows:
                cells = row if isinstance(row, list) else [row]
                parts.append(f"  {' | '.join(h(str(c)) for c in cells)}")
        elif wt == "status":
            dot = {"up": "🟢", "down": "🔴", "degraded": "🟡"}
            for item in (d.get("items") or []):
                parts.append(f"  {dot.get(item.get('status', 'up'), '•')} {h(item.get('name', ''))}")
    await update.message.reply_text("\n".join(parts), parse_mode="HTML")


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    chat_id = update.effective_chat.id
    templates = await list_templates()
    if not templates:
        await update.message.reply_text("No agents available. Create one in the dashboard first.")
        return

    query = " ".join(ctx.args).strip().lower() if ctx.args else ""

    if query:
        match = next((t for t in templates if query in t["name"].lower()), None)
        if not match:
            names = "\n".join(f"• {h(t['name'])}" for t in templates)
            await update.message.reply_text(
                f"No agent matching <b>{h(query)}</b>.\n\nAvailable:\n{names}",
                parse_mode="HTML",
            )
            return
        _awaiting_goal[chat_id] = {"template_id": match["id"], "template_name": match["name"]}
        await update.message.reply_text(
            f"<b>{h(match['name'])}</b> selected.\nWhat should it do?",
            parse_mode="HTML",
        )
    else:
        lines = ["<b>Choose an agent — reply with its number:</b>\n"]
        for i, t in enumerate(templates, 1):
            lines.append(f"  {i}. <b>{h(t['name'])}</b> — {h(t.get('description') or 'No description')}")
        _awaiting_goal[chat_id] = {"choosing": True, "templates": templates}
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Message handler (free text)
# ---------------------------------------------------------------------------

async def _handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Core routing logic shared by text and voice messages."""
    chat_id = update.effective_chat.id

    # 1. Reply to a waiting ask_user
    if chat_id in _pending_input:
        run_id, call_id = _pending_input.pop(chat_id)
        if _provide_input_fn:
            _provide_input_fn(run_id, call_id, text)
        await update.message.reply_text("✉️ Sent — agent is continuing...")
        return

    # 2. Completing a /run flow
    if chat_id in _awaiting_goal:
        state = _awaiting_goal[chat_id]

        if state.get("choosing"):
            templates = state["templates"]
            match = None
            try:
                idx = int(text) - 1
                if 0 <= idx < len(templates):
                    match = templates[idx]
            except ValueError:
                match = next((t for t in templates if text.lower() in t["name"].lower()), None)

            if match:
                _awaiting_goal[chat_id] = {"template_id": match["id"], "template_name": match["name"]}
                await update.message.reply_text(
                    f"<b>{h(match['name'])}</b> selected.\nWhat should it do?",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text("Reply with the number or part of the agent name.")
            return

        if "template_id" in state:
            template_id = state["template_id"]
            template_name = state["template_name"]
            del _awaiting_goal[chat_id]

            await update.message.reply_text(
                f"⚡ Starting <b>{h(template_name)}</b>...\nI'll message you when it needs input or finishes.",
                parse_mode="HTML",
            )
            if _start_run_fn:
                run_id = await _start_run_fn(template_id, template_name, text)
                _run_to_chat[run_id] = chat_id
            return

    # 3. General chat with the Goose meta-agent
    history = _chat_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": text})

    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        result = await run_chat(list(history))
        reply = result.get("reply") or "..."
        history.append({"role": "assistant", "content": reply})

        if len(history) > 20:
            _chat_history[chat_id] = history[-20:]

        action = result.get("action")
        msg = h(reply[:3800])
        if action and action.get("type") == "agent_created":
            msg += f"\n\n✅ Agent <b>{h(action['name'])}</b> created in your dashboard."

        await update.message.reply_text(msg, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Chat error: {e}")
        await update.message.reply_text(f"Sorry, something went wrong: {h(str(e))}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await _handle_text(update, ctx, text)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return

    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        tg_file = await ctx.bot.get_file(update.message.voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)
        buf.name = "voice.ogg"

        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        transcript = await client.audio.transcriptions.create(model="whisper-1", file=buf)
        text = transcript.text.strip()

        if not text:
            await update.message.reply_text("Sorry, I couldn't make out what you said.")
            return

        await update.message.reply_text(f"🎤 <i>{h(text)}</i>", parse_mode="HTML")
        await _handle_text(update, ctx, text)

    except Exception as e:
        logger.error(f"Voice transcription error: {e}")
        await update.message.reply_text(f"Sorry, couldn't process the voice message: {h(str(e))}")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start_bot(token: str) -> Application:
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("runs", cmd_runs))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await app.bot.set_my_commands([
        BotCommand("agents", "List available agents"),
        BotCommand("run", "Start an agent run"),
        BotCommand("runs", "View recent runs"),
        BotCommand("dashboard", "View dashboard widgets"),
        BotCommand("help", "Show help"),
    ])

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Telegram bot started")
    return app


async def stop_bot(app: Application):
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Telegram bot stopped")
