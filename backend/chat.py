import asyncio
import json
import os

from openai import AsyncOpenAI

from .database import create_template, list_templates
from .tools import TOOLS

_openai_client: AsyncOpenAI | None = None
_anthropic_client: AsyncOpenAI | None = None

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# ---------------------------------------------------------------------------
# Dev tool implementations
# ---------------------------------------------------------------------------

async def _run_command(command: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=PROJECT_DIR,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return "Command timed out after 120 seconds."
        output = stdout.decode("utf-8", errors="replace")
        return f"Exit code: {proc.returncode}\n{output}"[:5000]
    except Exception as e:
        return f"Error: {e}"


def _read_file(path: str) -> str:
    full = path if os.path.isabs(path) else os.path.join(PROJECT_DIR, path)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 8000:
            return content[:8000] + f"\n...[truncated — {len(content)} total chars]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def _write_file(path: str, content: str) -> str:
    full = path if os.path.isabs(path) else os.path.join(PROJECT_DIR, path)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def _list_files(path: str = ".") -> str:
    full = path if os.path.isabs(path) else os.path.join(PROJECT_DIR, path)
    try:
        items = []
        for entry in sorted(os.scandir(full), key=lambda e: (not e.is_dir(), e.name)):
            if entry.name.startswith(".") and entry.name not in (".env.example",):
                continue
            items.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(items) or "(empty)"
    except Exception as e:
        return f"Error listing files: {e}"


async def _web_search(query: str, max_results: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(max_results, 10)):
                results.append(f"**{r['title']}**\n{r['href']}\n{r['body']}")
        return "\n\n---\n\n".join(results) if results else "No results found."
    except ImportError:
        return "Web search unavailable. Run: pip install duckduckgo-search"
    except Exception as e:
        return f"Search error: {e}"


# ---------------------------------------------------------------------------
# Tool definitions for OpenAI
# ---------------------------------------------------------------------------

def _build_tools(agent_tool_names: str, agent_tool_lines: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_agent_template",
                "description": (
                    "Create a new AI agent template in the Goose dashboard. "
                    "Call once you have: name, purpose, tools needed, and credentials required."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "system_prompt": {"type": "string"},
                        "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "system_prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_agent_templates",
                "description": "List all existing agent templates in the Goose dashboard.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a shell command in the project directory. "
                    "Use for: installing packages (pip install), running tests, "
                    "restarting the server (pkill -f uvicorn), git operations, "
                    "reading command output, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the project. Path relative to project root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "e.g. 'backend/chat.py'"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite a file. Creates parent directories as needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string", "description": "Full file content"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files and directories at a path in the project.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path, default '.'"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web. Useful for docs, packages, error solutions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "description": "Default 5"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    available_tools = [
        {"name": name, "description": info["schema"]["function"]["description"]}
        for name, info in TOOLS.items()
        if name != "ask_user"
    ]
    tool_lines = "\n".join(f"  - {t['name']}: {t['description']}" for t in available_tools)
    tool_names = ", ".join(t["name"] for t in available_tools)

    return f"""You are Goose, an autonomous AI assistant and developer. You manage the Goose dashboard and can also modify the project itself.

You have two modes:

## 1. General assistant / agent builder
Answer questions, have conversations, and create new AI agent templates when asked.

When building agents, gather: name, purpose, tools needed (from: {tool_names}), and credentials. Then call `create_agent_template` with a detailed system prompt.

## 2. Developer mode
You can directly modify, extend, and operate the Goose project using your dev tools:
- **run_command** — run any shell command (pip install, git, tests, restart server, etc.)
- **read_file** — read any project file before editing it
- **write_file** — write or overwrite files
- **list_files** — explore the project structure
- **web_search** — look up docs, packages, or error messages

### How to approach dev tasks:
1. Always read a file before writing it — never write blindly
2. Make surgical changes — read the file, change only what's needed, write back the complete file preserving everything else
3. NEVER truncate or summarise file content when writing — always write the full file
4. Install Python packages with `source .venv/bin/activate && pip install -q <pkg>` AND add to requirements.txt
5. Install system packages with `sudo apt-get install -y <pkg>` (passwordless sudo is configured)
6. The server uses `uvicorn --reload`, so file changes auto-restart it
7. To explicitly restart: `pkill -f 'uvicorn backend.main:app'` — the process manager will bring it back
8. Always tell the user what you changed and why

Project root: {PROJECT_DIR}

Keep responses concise. For long tasks, narrate what you're doing step by step."""


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def _dispatch(fn_name: str, args: dict) -> tuple[str, dict | None]:
    """Returns (tool_result_str, optional_action)."""
    action = None

    if fn_name == "create_agent_template":
        tid = await create_template(
            name=args["name"],
            description=args.get("description", ""),
            system_prompt=args["system_prompt"],
            allowed_tools=args.get("allowed_tools", []),
        )
        action = {"type": "agent_created", "id": tid, "name": args["name"]}
        result = json.dumps({"success": True, "id": tid, "name": args["name"]})

    elif fn_name == "list_agent_templates":
        templates = await list_templates()
        result = json.dumps([
            {"id": t["id"], "name": t["name"], "description": t.get("description", "")}
            for t in templates
        ])

    elif fn_name == "run_command":
        result = await _run_command(args["command"])

    elif fn_name == "read_file":
        result = _read_file(args["path"])

    elif fn_name == "write_file":
        result = _write_file(args["path"], args["content"])

    elif fn_name == "list_files":
        result = _list_files(args.get("path", "."))

    elif fn_name == "web_search":
        result = await _web_search(args["query"], args.get("max_results", 5))

    else:
        result = json.dumps({"error": f"Unknown function: {fn_name}"})

    return result, action


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_chat(messages: list[dict]) -> dict:
    """
    Run one or more turns of the Goose meta-agent (agentic loop).
    messages: list of {role, content} dicts (no system message).
    Returns: {reply: str, action: dict | None}
    """
    system_prompt = _build_system_prompt()
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    available_tools = [
        {"name": name, "description": info["schema"]["function"]["description"]}
        for name, info in TOOLS.items()
        if name != "ask_user"
    ]
    tool_names = ", ".join(t["name"] for t in available_tools)
    tool_lines = "\n".join(f"  - {t['name']}: {t['description']}" for t in available_tools)
    chat_tools = _build_tools(tool_names, tool_lines)

    action = None
    chat_model = os.environ.get("CHAT_MODEL", "gpt-4o")

    # Agentic loop — keep going until the model stops calling tools
    for _ in range(15):
        response = await _get_client(chat_model).chat.completions.create(
            model=chat_model,
            messages=full_messages,
            tools=chat_tools,
            tool_choice="auto",
            max_tokens=4096,
        )

        choice = response.choices[0]
        msg = choice.message

        if choice.finish_reason != "tool_calls" or not msg.tool_calls:
            return {"reply": msg.content or "", "action": action}

        # Append the assistant's tool-call message
        full_messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute all tool calls and append results
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            result, tc_action = await _dispatch(tc.function.name, args)
            if tc_action:
                action = tc_action
            full_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    return {"reply": "Reached maximum tool-call iterations.", "action": action}
