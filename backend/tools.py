import asyncio
import imaplib
import email
from email.header import decode_header

import httpx

# ---------------------------------------------------------------------------
# Tool registry — each entry has:
#   schema   : OpenAI function calling schema
#   executor : async callable(arguments: dict, secrets: dict) -> str
#              None for ask_user (handled specially by runner)
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "ask_user": {
        "schema": {
            "type": "function",
            "function": {
                "name": "ask_user",
                "description": (
                    "Pause and ask the human operator a question, then wait for their response. "
                    "Use this when you need credentials, missing information, or a decision. "
                    "Set input_type to 'password' for sensitive values like passwords/API keys."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question to display to the user.",
                        },
                        "input_type": {
                            "type": "string",
                            "enum": ["text", "password", "multiline"],
                            "description": "Input field type. Use 'password' to mask the input.",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
        "executor": None,
    },
    "read_email_imap": {
        "schema": {
            "type": "function",
            "function": {
                "name": "read_email_imap",
                "description": "Read recent emails from an IMAP mailbox and return their content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server": {
                            "type": "string",
                            "description": "IMAP server hostname, e.g. imap.gmail.com",
                        },
                        "port": {
                            "type": "integer",
                            "description": "IMAP port (default 993 for SSL).",
                        },
                        "username": {"type": "string", "description": "Email address / login."},
                        "password": {"type": "string", "description": "Email password or app password."},
                        "folder": {
                            "type": "string",
                            "description": "Mailbox folder to read (default: INBOX).",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of most-recent emails to fetch (default: 5, max: 20).",
                        },
                    },
                    "required": ["server", "username", "password"],
                },
            },
        },
        "executor": "_exec_read_email_imap",
    },
    "http_request": {
        "schema": {
            "type": "function",
            "function": {
                "name": "http_request",
                "description": "Make an HTTP request (GET, POST, PUT, DELETE, PATCH) to a URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                        },
                        "url": {"type": "string", "description": "Full URL to request."},
                        "headers": {
                            "type": "object",
                            "description": "Optional HTTP headers as key-value pairs.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Optional request body (for POST/PUT/PATCH).",
                        },
                        "json_body": {
                            "type": "object",
                            "description": "Optional JSON body — use instead of body for JSON APIs.",
                        },
                    },
                    "required": ["method", "url"],
                },
            },
        },
        "executor": "_exec_http_request",
    },
    "read_file": {
        "schema": {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the text contents of a local file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or ~ path to the file."},
                    },
                    "required": ["path"],
                },
            },
        },
        "executor": "_exec_read_file",
    },
    "write_file": {
        "schema": {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write text content to a local file (creates parent dirs as needed).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute or ~ path to write."},
                        "content": {"type": "string", "description": "Content to write."},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "executor": "_exec_write_file",
    },
    "list_directory": {
        "schema": {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files and directories at a given path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path to list."},
                    },
                    "required": ["path"],
                },
            },
        },
        "executor": "_exec_list_directory",
    },
    "set_dashboard_widget": {
        "schema": {
            "type": "function",
            "function": {
                "name": "set_dashboard_widget",
                "description": (
                    "Display structured data as a widget on the Goose dashboard. "
                    "Use this to present results beautifully instead of plain text. "
                    "For list: data={items:[str]}. For table: data={columns:[str], rows:[[str]]}. "
                    "For metric: data={value:str, label:str, sublabel?:str}. "
                    "For text: data={content:str}. For status: data={items:[{name:str, status:'up'|'down'|'degraded'}]}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "widget_key": {
                            "type": "string",
                            "description": "Unique snake_case identifier for this widget, e.g. 'inbox_summary'.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Title shown on the dashboard card.",
                        },
                        "widget_type": {
                            "type": "string",
                            "enum": ["list", "table", "metric", "text", "status"],
                        },
                        "data": {
                            "type": "object",
                            "description": "Widget content — shape depends on widget_type.",
                        },
                    },
                    "required": ["widget_key", "title", "widget_type", "data"],
                },
            },
        },
        "executor": None,  # handled specially in runner
    },
    "trigger_agent": {
        "schema": {
            "type": "function",
            "function": {
                "name": "trigger_agent",
                "description": (
                    "Start another Goose agent by name to handle a subtask asynchronously. "
                    "The triggered agent runs independently and its results appear in the Runs list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_name": {
                            "type": "string",
                            "description": "Exact name of the agent template to run.",
                        },
                        "goal": {
                            "type": "string",
                            "description": "What the triggered agent should do.",
                        },
                    },
                    "required": ["agent_name", "goal"],
                },
            },
        },
        "executor": None,  # handled specially in runner
    },
    "run_shell_command": {
        "schema": {
            "type": "function",
            "function": {
                "name": "run_shell_command",
                "description": (
                    "Run a shell command on the local machine and return stdout/stderr. "
                    "Use carefully — only when explicitly needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute."},
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default 60, max 300).",
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        "executor": "_exec_run_shell_command",
    },
}


def get_tool_schemas(allowed_tools: list[str]) -> list[dict]:
    """Return OpenAI tool schemas for the given tool names (ask_user always included)."""
    names = ["ask_user"] + [t for t in allowed_tools if t != "ask_user"]
    return [TOOLS[n]["schema"] for n in names if n in TOOLS]


async def execute_tool(tool_name: str, arguments: dict, secrets: dict) -> str:
    """Dispatch to the appropriate tool executor."""
    tool = TOOLS.get(tool_name)
    if not tool:
        return f"Error: unknown tool '{tool_name}'"
    executor_name = tool.get("executor")
    if not executor_name:
        return f"Error: tool '{tool_name}' has no executor (handled specially)"
    executor = globals().get(executor_name)
    if not executor:
        return f"Error: executor '{executor_name}' not found"
    return await executor(arguments, secrets)


# ---------------------------------------------------------------------------
# Executor implementations
# ---------------------------------------------------------------------------

def _decode_header_safe(h) -> str:
    if not h:
        return "(none)"
    parts = decode_header(h)
    result = []
    for raw, charset in parts:
        if isinstance(raw, bytes):
            result.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(raw))
    return "".join(result)


def _read_email_sync(server: str, port: int, username: str, password: str, folder: str, count: int) -> str:
    mail = imaplib.IMAP4_SSL(server, port)
    try:
        mail.login(username, password)
        mail.select(folder)
        _, data = mail.search(None, "ALL")
        ids = data[0].split()
        if not ids:
            return "Mailbox is empty."
        ids = ids[-min(count, 20):]
        emails = []
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header_safe(msg.get("Subject", ""))
            from_addr = msg.get("From", "")
            date = msg.get("Date", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")[:2000]
                        break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")[:2000]
            emails.append(f"From: {from_addr}\nDate: {date}\nSubject: {subject}\n\n{body}")
        sep = "\n\n" + "─" * 60 + "\n\n"
        return sep.join(emails) if emails else "No emails retrieved."
    finally:
        try:
            mail.logout()
        except Exception:
            pass


async def _exec_read_email_imap(arguments: dict, secrets: dict) -> str:
    server = arguments.get("server", "imap.gmail.com")
    port = int(arguments.get("port", 993))
    username = arguments.get("username", "")
    password = arguments.get("password", "")
    folder = arguments.get("folder", "INBOX")
    count = min(int(arguments.get("count", 5)), 20)
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _read_email_sync, server, port, username, password, folder, count
        )
    except Exception as e:
        return f"Error reading email: {e}"


async def _exec_http_request(arguments: dict, secrets: dict) -> str:
    method = arguments.get("method", "GET").upper()
    url = arguments["url"]
    headers = arguments.get("headers") or {}
    body = arguments.get("body")
    json_body = arguments.get("json_body")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            kwargs: dict = {"headers": headers}
            if json_body is not None:
                kwargs["json"] = json_body
            elif body is not None:
                kwargs["content"] = body.encode()
            resp = await client.request(method, url, **kwargs)
            text = resp.text[:8000]
            return f"Status: {resp.status_code}\n\n{text}"
    except Exception as e:
        return f"HTTP request error: {e}"


async def _exec_read_file(arguments: dict, secrets: dict) -> str:
    from pathlib import Path
    try:
        path = Path(arguments["path"]).expanduser()
        return path.read_text(encoding="utf-8", errors="replace")[:10000]
    except Exception as e:
        return f"Error reading file: {e}"


async def _exec_write_file(arguments: dict, secrets: dict) -> str:
    from pathlib import Path
    try:
        path = Path(arguments["path"]).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments["content"], encoding="utf-8")
        return f"Wrote {len(arguments['content'])} characters to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


async def _exec_list_directory(arguments: dict, secrets: dict) -> str:
    from pathlib import Path
    try:
        path = Path(arguments["path"]).expanduser()
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines = []
        for entry in entries:
            prefix = "[DIR]  " if entry.is_dir() else "[FILE] "
            lines.append(f"{prefix}{entry.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"Error listing directory: {e}"


async def _exec_run_shell_command(arguments: dict, secrets: dict) -> str:
    command = arguments["command"]
    timeout = min(int(arguments.get("timeout", 60)), 300)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Command timed out after {timeout}s"
        result = f"Exit code: {proc.returncode}"
        if stdout:
            result += f"\n\nSTDOUT:\n{stdout.decode('utf-8', errors='replace')[:5000]}"
        if stderr:
            result += f"\n\nSTDERR:\n{stderr.decode('utf-8', errors='replace')[:2000]}"
        return result
    except Exception as e:
        return f"Error running command: {e}"
