import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

load_dotenv()

from .database import (
    append_run_event,
    create_run,
    create_schedule,
    create_template,
    delete_schedule,
    delete_secret_entry,
    delete_template,
    delete_widget,
    get_due_schedules,
    get_run,
    get_secret_keys,
    get_template,
    get_usage_stats,
    init_db,
    list_runs,
    list_schedules,
    list_templates,
    list_widgets,
    mark_schedule_ran,
    set_template_pinned,
    toggle_schedule,
    update_run_tokens,
    update_template,
    upsert_widget,
)
from .runner import AgentRun, run_agent
from .vault import Vault
from .chat import run_chat
from .tools import TOOLS

try:
    from . import telegram_bot as _tg
    _TG_AVAILABLE = True
except ImportError:
    _tg = None
    _TG_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_ws_connections: dict[str, list] = {}
_active_runs: dict[str, AgentRun] = {}
_active_tasks: dict[str, asyncio.Task] = {}
_tg_app = None
vault: Vault = None


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------

async def _run_due_schedules():
    schedules = await get_due_schedules()
    for sched in schedules:
        template = await get_template(sched["template_id"])
        if not template:
            continue
        run_id = await create_run(sched["template_id"], template["name"], sched["goal"], template.get("model", "gpt-4o"))
        await mark_schedule_ran(sched["id"])
        secrets = await vault.get_secrets(sched["template_id"])
        tmpl = dict(template)
        tmpl["_user_goal"] = sched["goal"]
        agent_run = AgentRun(run_id)
        _active_runs[run_id] = agent_run

        async def _bcast(event: dict, _rid=run_id):
            await _broadcast(_rid, event)

        async def _ssecret(key: str, value: str, _tid=sched["template_id"]):
            await vault.set_secret(_tid, key, value)

        task = asyncio.create_task(
            run_agent(run_id=run_id, template=tmpl, secrets=secrets,
                      agent_run=agent_run, broadcast=_bcast, save_secret=_ssecret)
        )
        task.add_done_callback(lambda t, r=run_id: _cleanup_run(r))
        _active_tasks[run_id] = task


async def _scheduler_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await _run_due_schedules()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def _tg_start_run(template_id: str, template_name: str, goal: str) -> str:
    template = await get_template(template_id)
    if not template:
        raise ValueError(f"Template {template_id} not found")
    run_id = await create_run(template_id, template_name, goal, template.get("model", "gpt-4o"))
    secrets = await vault.get_secrets(template_id)
    tmpl = dict(template)
    tmpl["_user_goal"] = goal
    tmpl["id"] = template_id
    agent_run = AgentRun(run_id)
    _active_runs[run_id] = agent_run

    async def _bcast(event: dict, _rid=run_id):
        await _broadcast(_rid, event)

    async def _ssecret(key: str, value: str, _tid=template_id):
        await vault.set_secret(_tid, key, value)

    task = asyncio.create_task(
        run_agent(run_id=run_id, template=tmpl, secrets=secrets,
                  agent_run=agent_run, broadcast=_bcast, save_secret=_ssecret)
    )
    task.add_done_callback(lambda t: _cleanup_run(run_id))
    _active_tasks[run_id] = task
    return run_id


def _tg_provide_input(run_id: str, call_id: str, value: str) -> bool:
    agent_run = _active_runs.get(run_id)
    if agent_run:
        payload = json.dumps({"value": value, "save_to_template": False, "save_key": ""})
        agent_run.provide_input(call_id, payload)
        return True
    return False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global vault, _tg_app
    await init_db()
    vault = Vault.load_or_create(".vault.key")
    sched_task = asyncio.create_task(_scheduler_loop())

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tg_token and _TG_AVAILABLE:
        tg_allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
        _tg.setup(_tg_start_run, _tg_provide_input, tg_allowed)
        try:
            _tg_app = await _tg.start_bot(tg_token)
        except Exception as e:
            logger.warning(f"Telegram bot failed to start: {e}")

    yield

    if _tg_app is not None:
        await _tg.stop_bot(_tg_app)
    sched_task.cancel()
    for task in _active_tasks.values():
        task.cancel()
    if _active_tasks:
        await asyncio.gather(*_active_tasks.values(), return_exceptions=True)


app = FastAPI(lifespan=lifespan, title="Goose AI Dashboard")


# ---------------------------------------------------------------------------
# Connection manager helpers
# ---------------------------------------------------------------------------

async def _broadcast(run_id: str, event: dict):
    dead = []
    for ws in _ws_connections.get(run_id, []):
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            _ws_connections[run_id].remove(ws)
        except ValueError:
            pass
    if _tg_app is not None and _TG_AVAILABLE:
        try:
            await _tg.on_run_event(run_id, event, _tg_app.bot)
        except Exception:
            pass


def _cleanup_run(run_id: str):
    _active_runs.pop(run_id, None)
    _active_tasks.pop(run_id, None)


# ---------------------------------------------------------------------------
# REST — Tools
# ---------------------------------------------------------------------------

@app.get("/api/tools")
async def api_list_tools():
    return [
        {"name": name, "description": info["schema"]["function"]["description"]}
        for name, info in TOOLS.items()
    ]


# ---------------------------------------------------------------------------
# REST — Chat (Goose meta-agent)
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(data: dict):
    messages = data.get("messages", [])
    if not messages:
        raise HTTPException(400, "messages is required")
    result = await run_chat(messages)
    return result


# ---------------------------------------------------------------------------
# REST — Templates
# ---------------------------------------------------------------------------

@app.get("/api/templates")
async def api_list_templates():
    templates = await list_templates()
    for t in templates:
        t["secret_keys"] = await get_secret_keys(t["id"])
        t["allowed_tools"] = json.loads(t["allowed_tools"])
    return templates


@app.post("/api/templates", status_code=201)
async def api_create_template(data: dict):
    tid = await create_template(
        name=data["name"],
        description=data.get("description", ""),
        system_prompt=data["system_prompt"],
        allowed_tools=data.get("allowed_tools", []),
        model=data.get("model", "gpt-4o"),
    )
    for key, value in (data.get("secrets") or {}).items():
        if key and value:
            await vault.set_secret(tid, key, value)
    return {"id": tid}


@app.put("/api/templates/{template_id}")
async def api_update_template(template_id: str, data: dict):
    template = await get_template(template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    await update_template(
        template_id,
        name=data.get("name", template["name"]),
        description=data.get("description", template.get("description", "")),
        system_prompt=data.get("system_prompt", template["system_prompt"]),
        allowed_tools=data.get("allowed_tools", json.loads(template["allowed_tools"])),
        model=data.get("model", template.get("model", "gpt-4o")),
    )
    for key, value in (data.get("secrets") or {}).items():
        if key and value:
            await vault.set_secret(template_id, key, value)
    for key in (data.get("delete_secrets") or []):
        await delete_secret_entry(template_id, key)
    return {"id": template_id}


@app.delete("/api/templates/{template_id}")
async def api_delete_template(template_id: str):
    template = await get_template(template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    await delete_template(template_id)
    return {"ok": True}


@app.post("/api/templates/{template_id}/pin")
async def api_pin_template(template_id: str, data: dict):
    template = await get_template(template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    await set_template_pinned(template_id, data.get("pinned", True))
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST — Schedules
# ---------------------------------------------------------------------------

@app.get("/api/templates/{template_id}/schedules")
async def api_list_schedules(template_id: str):
    return await list_schedules(template_id)


@app.post("/api/templates/{template_id}/schedules", status_code=201)
async def api_create_schedule(template_id: str, data: dict):
    sid = await create_schedule(
        template_id=template_id,
        goal=data["goal"],
        interval_minutes=data.get("interval_minutes", 1440),
    )
    return {"id": sid}


@app.delete("/api/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: str):
    await delete_schedule(schedule_id)
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/toggle")
async def api_toggle_schedule(schedule_id: str, data: dict):
    await toggle_schedule(schedule_id, data.get("enabled", True))
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST — Runs
# ---------------------------------------------------------------------------

@app.get("/api/runs")
async def api_list_runs():
    return await list_runs()


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    run = await get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.post("/api/runs", status_code=201)
async def api_start_run(data: dict):
    template_id = data.get("template_id")
    goal = data.get("goal", "")

    if template_id:
        template = await get_template(template_id)
        if not template:
            raise HTTPException(404, "Template not found")
        template_name = template["name"]
    else:
        template = {
            "id": None, "name": "Ad-hoc",
            "system_prompt": data.get("system_prompt", "You are a helpful assistant."),
            "allowed_tools": json.dumps(list(TOOLS.keys())),
            "model": data.get("model", "gpt-4o"),
        }
        template_name = "Ad-hoc"

    run_id = await create_run(template_id, template_name, goal, template.get("model", "gpt-4o"))
    secrets = await vault.get_secrets(template_id) if template_id else {}
    tmpl = dict(template)
    tmpl["_user_goal"] = goal
    if template_id:
        tmpl["id"] = template_id

    agent_run = AgentRun(run_id)
    _active_runs[run_id] = agent_run

    async def _bcast(event: dict, _rid=run_id):
        await _broadcast(_rid, event)

    async def _ssecret(key: str, value: str, _tid=template_id):
        if _tid:
            await vault.set_secret(_tid, key, value)

    task = asyncio.create_task(
        run_agent(run_id=run_id, template=tmpl, secrets=secrets,
                  agent_run=agent_run, broadcast=_bcast, save_secret=_ssecret)
    )
    task.add_done_callback(lambda t: _cleanup_run(run_id))
    _active_tasks[run_id] = task

    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/continue")
async def api_continue_run(run_id: str, data: dict):
    run = await get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    user_message = data.get("message", "")
    template_id = run.get("template_id")
    template = await get_template(template_id) if template_id else None
    if not template:
        raise HTTPException(404, "Template not found")

    events = json.loads(run.get("events") or "[]")
    prior_messages = []
    for ev in events:
        if ev.get("type") == "message":
            prior_messages.append({"role": ev["role"], "content": ev["content"]})
    prior_messages.append({"role": "user", "content": user_message})

    new_run_id = await create_run(template_id, template["name"], user_message, template.get("model", "gpt-4o"))
    secrets = await vault.get_secrets(template_id)
    tmpl = dict(template)
    tmpl["_user_goal"] = user_message
    tmpl["id"] = template_id

    agent_run = AgentRun(new_run_id)
    _active_runs[new_run_id] = agent_run

    async def _bcast(event: dict, _rid=new_run_id):
        await _broadcast(_rid, event)

    async def _ssecret(key: str, value: str, _tid=template_id):
        await vault.set_secret(_tid, key, value)

    task = asyncio.create_task(
        run_agent(run_id=new_run_id, template=tmpl, secrets=secrets,
                  agent_run=agent_run, broadcast=_bcast, save_secret=_ssecret,
                  initial_messages=prior_messages)
    )
    task.add_done_callback(lambda t: _cleanup_run(new_run_id))
    _active_tasks[new_run_id] = task

    return {"run_id": new_run_id}


@app.post("/api/runs/{run_id}/cancel")
async def api_cancel_run(run_id: str):
    agent_run = _active_runs.get(run_id)
    if agent_run:
        agent_run.cancel()
    task = _active_tasks.get(run_id)
    if task:
        task.cancel()
    return {"ok": True}


@app.post("/api/runs/{run_id}/input")
async def api_provide_input(run_id: str, data: dict):
    agent_run = _active_runs.get(run_id)
    if not agent_run:
        raise HTTPException(404, "Run not active")
    call_id = data.get("call_id", "")
    payload = json.dumps({
        "value": data.get("value", ""),
        "save_to_template": data.get("save_to_template", False),
        "save_key": data.get("save_key", ""),
    })
    agent_run.provide_input(call_id, payload)

    if data.get("save_to_template") and data.get("save_key") and data.get("value"):
        run = await get_run(run_id)
        if run and run.get("template_id"):
            await vault.set_secret(run["template_id"], data["save_key"], data["value"])

    return {"ok": True}


# ---------------------------------------------------------------------------
# REST — Widgets
# ---------------------------------------------------------------------------

@app.get("/api/widgets")
async def api_list_widgets():
    return await list_widgets()


@app.delete("/api/widgets/{widget_id}")
async def api_delete_widget(widget_id: str):
    await delete_widget(widget_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST — Usage & Costs
# ---------------------------------------------------------------------------

@app.get("/api/usage")
async def api_usage():
    return await get_usage_stats()


# ---------------------------------------------------------------------------
# WebSocket — live run events
# ---------------------------------------------------------------------------

@app.websocket("/ws/{run_id}")
async def ws_run(websocket: WebSocket, run_id: str):
    await websocket.accept()
    _ws_connections.setdefault(run_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        try:
            _ws_connections[run_id].remove(websocket)
        except (KeyError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Static files (frontend)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
