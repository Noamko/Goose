import uuid
import json
from datetime import datetime, timezone, timedelta
import aiosqlite

DB_PATH = "goose.db"

_CREATE_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    system_prompt TEXT NOT NULL,
    allowed_tools TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT 'gpt-4o',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    template_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    user_goal TEXT NOT NULL,
    messages TEXT NOT NULL DEFAULT '[]',
    events TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    error TEXT,
    token_usage TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value_enc BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(template_id, key),
    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS widgets (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    widget_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    widget_type TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(template_id, widget_key)
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    next_run TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
);
"""

# Migrations for existing databases (safe to fail if column already exists)
_MIGRATIONS = [
    "ALTER TABLE templates ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-4o'",
    "ALTER TABLE templates ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN token_usage TEXT",
    "ALTER TABLE runs ADD COLUMN model TEXT NOT NULL DEFAULT 'gpt-4o'",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.executescript(_CREATE_SQL)
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass
        await db.commit()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

async def list_templates() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM templates ORDER BY pinned DESC, created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_template(template_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_template(
    name: str,
    description: str,
    system_prompt: str,
    allowed_tools: list[str],
    model: str = "gpt-4o",
    pinned: bool = False,
) -> str:
    tid = new_id()
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "INSERT INTO templates (id, name, description, system_prompt, allowed_tools, model, pinned, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, name, description, system_prompt, json.dumps(allowed_tools), model, 1 if pinned else 0, now, now),
        )
        await db.commit()
    return tid


async def update_template(
    template_id: str,
    name: str,
    description: str,
    system_prompt: str,
    allowed_tools: list[str],
    model: str = "gpt-4o",
):
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE templates SET name=?, description=?, system_prompt=?, allowed_tools=?, model=?, updated_at=? WHERE id=?",
            (name, description, system_prompt, json.dumps(allowed_tools), model, now, template_id),
        )
        await db.commit()


async def set_template_pinned(template_id: str, pinned: bool):
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE templates SET pinned=?, updated_at=? WHERE id=?",
            (1 if pinned else 0, now, template_id),
        )
        await db.commit()


async def delete_template(template_id: str):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("DELETE FROM templates WHERE id=?", (template_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

async def list_runs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, template_id, template_name, status, user_goal, started_at, "
            "completed_at, result, error, token_usage FROM runs ORDER BY started_at DESC LIMIT 100"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_run(run_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs WHERE id=?", (run_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def create_run(
    template_id: str | None,
    template_name: str | None,
    user_goal: str,
    model: str = "gpt-4o",
) -> str:
    rid = new_id()
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "INSERT INTO runs (id, template_id, template_name, status, user_goal, model, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, template_id, template_name, "pending", user_goal, model, now, now),
        )
        await db.commit()
    return rid


async def get_usage_stats() -> dict:
    """Aggregate token usage and estimated costs from completed runs."""
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT template_name, model, token_usage, started_at "
            "FROM runs WHERE status='completed' AND token_usage IS NOT NULL"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    # Pricing per 1M tokens: {model: (input_price, output_price)}
    PRICING = {
        "claude-sonnet-4-6":        (3.00,  15.00),
        "claude-opus-4-6":          (15.00, 75.00),
        "claude-haiku-4-5-20251001": (0.80,  4.00),
        "claude-haiku-4-5":         (0.80,  4.00),
        "gpt-4o":                   (2.50,  10.00),
        "gpt-4o-mini":              (0.15,   0.60),
        "gpt-4-turbo":              (10.00, 30.00),
    }

    def cost_for(model: str, prompt: int, completion: int) -> float:
        inp, out = PRICING.get(model, (2.50, 10.00))
        return (prompt / 1_000_000) * inp + (completion / 1_000_000) * out

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=7)).isoformat()
    month_start = now.replace(day=1).isoformat()

    total_cost = month_cost = week_cost = today_cost = 0.0
    total_prompt = total_completion = total_runs = 0
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}

    for row in rows:
        try:
            usage = json.loads(row["token_usage"])
            prompt = int(usage.get("prompt", 0))
            completion = int(usage.get("completion", 0))
        except Exception:
            continue

        model = row["model"] or "gpt-4o"
        c = cost_for(model, prompt, completion)
        started = row["started_at"] or ""

        total_cost += c
        total_prompt += prompt
        total_completion += completion
        total_runs += 1

        if started[:10] == today:
            today_cost += c
        if started >= week_start:
            week_cost += c
        if started >= month_start:
            month_cost += c

        if model not in by_model:
            by_model[model] = {"model": model, "runs": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        by_model[model]["runs"] += 1
        by_model[model]["prompt_tokens"] += prompt
        by_model[model]["completion_tokens"] += completion
        by_model[model]["cost"] += c

        agent = row["template_name"] or "Ad-hoc"
        if agent not in by_agent:
            by_agent[agent] = {"template_name": agent, "runs": 0, "cost": 0.0}
        by_agent[agent]["runs"] += 1
        by_agent[agent]["cost"] += c

    return {
        "summary": {
            "total_cost": round(total_cost, 4),
            "month_cost": round(month_cost, 4),
            "week_cost": round(week_cost, 4),
            "today_cost": round(today_cost, 4),
            "total_tokens": total_prompt + total_completion,
            "total_runs": total_runs,
        },
        "by_model": sorted(by_model.values(), key=lambda x: x["cost"], reverse=True),
        "by_agent": sorted(by_agent.values(), key=lambda x: x["cost"], reverse=True)[:10],
    }


async def update_run_status(run_id: str, status: str, result: str = None, error: str = None):
    now = utcnow()
    completed_at = now if status in ("completed", "failed", "cancelled") else None
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE runs SET status=?, updated_at=?, completed_at=COALESCE(?, completed_at), "
            "result=COALESCE(?, result), error=COALESCE(?, error) WHERE id=?",
            (status, now, completed_at, result, error, run_id),
        )
        await db.commit()


async def save_run_messages(run_id: str, messages: list[dict]):
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE runs SET messages=?, updated_at=? WHERE id=?",
            (json.dumps(messages), now, run_id),
        )
        await db.commit()


async def update_run_tokens(run_id: str, prompt_tokens: int, completion_tokens: int):
    now = utcnow()
    usage = json.dumps({
        "prompt": prompt_tokens,
        "completion": completion_tokens,
        "total": prompt_tokens + completion_tokens,
    })
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE runs SET token_usage=?, updated_at=? WHERE id=?",
            (usage, now, run_id),
        )
        await db.commit()


async def append_run_event(run_id: str, event: dict):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        async with db.execute("SELECT events FROM runs WHERE id=?", (run_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return
            events = json.loads(row[0])
        events.append(event)
        now = utcnow()
        await db.execute(
            "UPDATE runs SET events=?, updated_at=? WHERE id=?",
            (json.dumps(events), now, run_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

async def get_secret_keys(template_id: str) -> list[str]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        async with db.execute(
            "SELECT key FROM secrets WHERE template_id=? ORDER BY key", (template_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_all_secret_blobs(template_id: str) -> dict[str, bytes]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        async with db.execute(
            "SELECT key, value_enc FROM secrets WHERE template_id=?", (template_id,)
        ) as cur:
            rows = await cur.fetchall()
            return {r[0]: r[1] for r in rows}


async def set_secret_blob(template_id: str, key: str, value_enc: bytes):
    now = utcnow()
    sid = new_id()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "INSERT INTO secrets (id, template_id, key, value_enc, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(template_id, key) DO UPDATE SET value_enc=excluded.value_enc, updated_at=excluded.updated_at",
            (sid, template_id, key, value_enc, now, now),
        )
        await db.commit()


async def delete_secret_entry(template_id: str, key: str):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("DELETE FROM secrets WHERE template_id=? AND key=?", (template_id, key))
        await db.commit()


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

async def upsert_widget(
    template_id: str | None,
    widget_key: str,
    title: str,
    widget_type: str,
    data: dict,
) -> str:
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM widgets WHERE template_id IS ? AND widget_key=?",
            (template_id, widget_key),
        ) as cur:
            row = await cur.fetchone()
        if row:
            wid = row["id"]
            await db.execute(
                "UPDATE widgets SET title=?, widget_type=?, data=?, updated_at=? WHERE id=?",
                (title, widget_type, json.dumps(data), now, wid),
            )
        else:
            wid = new_id()
            await db.execute(
                "INSERT INTO widgets (id, template_id, widget_key, title, widget_type, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (wid, template_id, widget_key, title, widget_type, json.dumps(data), now),
            )
        await db.commit()
    return wid


async def list_widgets() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT w.*, t.name as template_name FROM widgets w "
            "LEFT JOIN templates t ON w.template_id = t.id ORDER BY w.updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result


async def delete_widget(widget_id: str):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("DELETE FROM widgets WHERE id=?", (widget_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

async def create_schedule(template_id: str, goal: str, interval_minutes: int) -> str:
    sid = new_id()
    now = utcnow()
    next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "INSERT INTO schedules (id, template_id, goal, interval_minutes, enabled, next_run, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (sid, template_id, goal, interval_minutes, next_run, now, now),
        )
        await db.commit()
    return sid


async def list_schedules(template_id: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        if template_id:
            async with db.execute(
                "SELECT * FROM schedules WHERE template_id=? ORDER BY created_at DESC",
                (template_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT s.*, t.name as template_name FROM schedules s "
                "JOIN templates t ON s.template_id=t.id ORDER BY s.created_at DESC"
            ) as cur:
                rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def delete_schedule(schedule_id: str):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        await db.commit()


async def toggle_schedule(schedule_id: str, enabled: bool):
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE schedules SET enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now, schedule_id),
        )
        await db.commit()


async def get_due_schedules() -> list[dict]:
    now = utcnow()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT s.*, t.name as template_name FROM schedules s "
            "JOIN templates t ON s.template_id=t.id "
            "WHERE s.enabled=1 AND (s.next_run IS NULL OR s.next_run <= ?)",
            (now,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_schedule_ran(schedule_id: str, interval_minutes: int):
    now = utcnow()
    next_run = (datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        await db.execute(
            "UPDATE schedules SET last_run=?, next_run=?, updated_at=? WHERE id=?",
            (now, next_run, now, schedule_id),
        )
        await db.commit()
