import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Callable, Awaitable

from openai import AsyncOpenAI

from .database import (
    update_run_status,
    save_run_messages,
    append_run_event,
    update_run_tokens,
    upsert_widget,
    list_templates,
    create_run,
)
from .tools import execute_tool, get_tool_schemas

_openai_client: AsyncOpenAI | None = None
_anthropic_client: AsyncOpenAI | None = None


def _get_client(model: str) -> AsyncOpenAI:
    global _openai_client, _anthropic_client
    if model.startswith("claude"):
        if _anthropic_client is None:
            _anthropic_client = AsyncOpenAI(
                base_url="https://api.anthropic.com/v1/",
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            )
        return _anthropic_client
    else:
        if _openai_client is None:
            _openai_client = AsyncOpenAI()
        return _openai_client


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, max_chars: int = 600) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"… [truncated, {len(text)} chars total]"


class AgentRun:
    """Manages the pause/resume state for a single agent run."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.cancelled = False
        self._pending: dict[str, tuple[asyncio.Event, list]] = {}

    async def wait_for_input(self, call_id: str, timeout_s: int = 1800) -> str:
        """Block until the user provides input for this call_id."""
        ev = asyncio.Event()
        holder: list[str] = []
        self._pending[call_id] = (ev, holder)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(call_id, None)
            raise TimeoutError(f"No response within {timeout_s // 60} minutes")
        self._pending.pop(call_id, None)
        return holder[0]

    def provide_input(self, call_id: str, value: str):
        """Called from the WebSocket handler to resume the agent."""
        if call_id in self._pending:
            ev, holder = self._pending[call_id]
            holder.append(value)
            ev.set()

    def cancel(self):
        """Cancel the run and unblock any pending waits."""
        self.cancelled = True
        for ev, holder in self._pending.values():
            holder.append("")
            ev.set()
        self._pending.clear()


async def run_agent(
    run_id: str,
    template: dict,
    secrets: dict[str, str],
    agent_run: AgentRun,
    broadcast: Callable[[dict], Awaitable[None]],
    save_secret: Callable[[str, str], Awaitable[None]],
    initial_messages: list[dict] | None = None,
):
    """
    Main agent execution loop. Runs as an asyncio task.

    broadcast(event_dict) — sends an event to all connected WebSocket clients for this run.
    save_secret(key, value) — saves a secret to the template vault on user request.
    """

    async def emit(event: dict):
        """Broadcast + persist an event."""
        event.setdefault("timestamp", _utcnow())
        await broadcast(event)
        await append_run_event(run_id, event)

    user_goal = template.get("_user_goal", "")
    allowed_tools: list[str] = json.loads(template.get("allowed_tools", "[]"))
    system_prompt: str = template.get("system_prompt", "You are a helpful assistant.")

    # Inject pre-stored secrets into the system prompt so GPT can use them silently
    if secrets:
        secrets_block = "\n\n<stored_credentials>\n"
        for k, v in secrets.items():
            secrets_block += f"  <{k}>{v}</{k}>\n"
        secrets_block += "</stored_credentials>\n"
        secrets_block += (
            "\nIMPORTANT: Never repeat or quote raw credential values in your visible messages. "
            "Use them silently when making tool calls."
        )
        system_prompt = system_prompt + secrets_block

    if initial_messages is not None:
        # Continuation: reuse existing history, refresh secrets in system message
        messages = list(initial_messages)
        messages[0] = {"role": "system", "content": system_prompt}
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        log_label = f"Continuing: {last_user}"
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_goal},
        ]
        log_label = f"Starting: {user_goal}"

    model = template.get("model", "claude-opus-4-6")
    template_id = template.get("id")
    tool_schemas = get_tool_schemas(allowed_tools)
    max_iterations = int(template.get("max_iterations") or 100)
    total_prompt_tokens = 0
    total_completion_tokens = 0

    try:
        await update_run_status(run_id, "running")
        await emit({"type": "status_change", "status": "running"})
        await emit({"type": "agent_log", "content": log_label})

        for iteration in range(max_iterations):
            if agent_run.cancelled:
                await update_run_status(run_id, "cancelled")
                await emit({"type": "status_change", "status": "cancelled"})
                return

            # Call OpenAI
            kwargs: dict = {
                "model": model,
                "messages": messages,
                "max_tokens": 4096,
            }
            if tool_schemas:
                kwargs["tools"] = tool_schemas
                kwargs["tool_choice"] = "auto"

            response = await _get_client(model).chat.completions.create(**kwargs)
            if response.usage:
                total_prompt_tokens += response.usage.prompt_tokens
                total_completion_tokens += response.usage.completion_tokens
                await update_run_tokens(run_id, total_prompt_tokens, total_completion_tokens)
            choice = response.choices[0]
            msg = choice.message

            # Convert assistant message to serializable dict
            if msg.tool_calls:
                assistant_dict = {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            else:
                assistant_dict = {"role": "assistant", "content": msg.content}

            messages.append(assistant_dict)

            # --- Done ---
            if choice.finish_reason == "stop":
                final = msg.content or ""
                await save_run_messages(run_id, messages)
                await update_run_status(run_id, "completed", result=final)
                await emit({"type": "run_complete", "content": final})
                await emit({"type": "status_change", "status": "completed"})
                return

            # --- Tool calls ---
            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                tool_results: list[dict] = []

                for tc in msg.tool_calls:
                    if agent_run.cancelled:
                        break

                    call_id = tc.id
                    tool_name = tc.function.name
                    try:
                        arguments = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        arguments = {}

                    await emit({
                        "type": "tool_call_start",
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    })

                    if tool_name == "ask_user":
                        question = arguments.get("question", "Please provide information:")
                        input_type = arguments.get("input_type", "text")

                        await update_run_status(run_id, "waiting_for_user")
                        await emit({
                            "type": "user_input_required",
                            "call_id": call_id,
                            "question": question,
                            "input_type": input_type,
                        })

                        try:
                            response_data_str = await agent_run.wait_for_input(call_id)
                        except TimeoutError as e:
                            await update_run_status(run_id, "failed", error=str(e))
                            await emit({"type": "error", "content": str(e)})
                            await emit({"type": "status_change", "status": "failed"})
                            return

                        if agent_run.cancelled:
                            break

                        # response_data_str is JSON: {value, save_to_template, save_key}
                        try:
                            resp_data = json.loads(response_data_str)
                        except Exception:
                            resp_data = {"value": response_data_str}

                        user_value = resp_data.get("value", "")
                        should_save = resp_data.get("save_to_template", False)
                        save_key = resp_data.get("save_key") or f"secret_{call_id[:8]}"

                        if should_save:
                            await save_secret(save_key, user_value)
                            await emit({"type": "agent_log", "content": f"Saved credential '{save_key}' for future runs."})

                        await update_run_status(run_id, "running")
                        await emit({"type": "status_change", "status": "running"})
                        await emit({
                            "type": "tool_call_result",
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "result": "[user provided]" if arguments.get("input_type") == "password" else _truncate(user_value),
                            "status": "success",
                        })

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": user_value,
                        })

                    elif tool_name == "set_dashboard_widget":
                        widget_key = arguments.get("widget_key", "widget")
                        title = arguments.get("title", "Widget")
                        widget_type = arguments.get("widget_type", "text")
                        data = arguments.get("data", {})
                        await upsert_widget(template_id, widget_key, title, widget_type, data)
                        result = f"Widget '{title}' updated on dashboard."
                        await emit({
                            "type": "widget_update",
                            "widget_key": widget_key,
                            "title": title,
                            "widget_type": widget_type,
                            "data": data,
                        })
                        await emit({
                            "type": "tool_call_result",
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "result": result,
                            "status": "success",
                        })
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result,
                        })

                    elif tool_name == "trigger_agent":
                        agent_name = arguments.get("agent_name", "")
                        goal = arguments.get("goal", "")
                        templates = await list_templates()
                        target = next(
                            (t for t in templates if t["name"].lower() == agent_name.lower()),
                            None,
                        )
                        if not target:
                            result = f"No agent named '{agent_name}' found."
                            status = "error"
                        else:
                            child_run_id = await create_run(target["id"], target["name"], goal, target.get("model", "claude-opus-4-6"))
                            child_template = dict(target)
                            child_template["_user_goal"] = goal

                            async def _noop_broadcast(e): pass
                            async def _noop_secret(k, v): pass

                            asyncio.create_task(run_agent(
                                run_id=child_run_id,
                                template=child_template,
                                secrets={},
                                agent_run=AgentRun(child_run_id),
                                broadcast=_noop_broadcast,
                                save_secret=_noop_secret,
                            ))
                            result = f"Started agent '{target['name']}' (run {child_run_id[:8]}…)"
                            status = "success"
                        await emit({
                            "type": "tool_call_result",
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "result": result,
                            "status": status,
                        })
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result,
                        })

                    else:
                        # Regular tool
                        try:
                            result = await execute_tool(tool_name, arguments, secrets)
                            status = "success"
                        except Exception as e:
                            result = f"Tool error: {e}"
                            status = "error"

                        await emit({
                            "type": "tool_call_result",
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "result": _truncate(result),
                            "status": status,
                        })

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": result,  # full result to OpenAI
                        })

                messages.extend(tool_results)
                await save_run_messages(run_id, messages)
                continue

        # Exceeded max iterations
        await update_run_status(run_id, "failed", error="Maximum iterations reached")
        await emit({"type": "error", "content": "Maximum iterations reached without completing."})
        await emit({"type": "status_change", "status": "failed"})

    except asyncio.CancelledError:
        await update_run_status(run_id, "cancelled")
        await emit({"type": "status_change", "status": "cancelled"})
    except Exception as e:
        err = str(e)
        await update_run_status(run_id, "failed", error=err)
        try:
            await emit({"type": "error", "content": f"Unexpected error: {err}"})
            await emit({"type": "status_change", "status": "failed"})
        except Exception:
            pass
