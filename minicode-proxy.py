#!/usr/bin/env python3
"""Local coding shim in front of the small model.

The 7B model is not trusted to pick tools. This process reads the user turn,
does obvious filesystem work itself, and only then forwards harder writing
to the model. Dumped files still become write plus bash.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MINICODE_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")
UPSTREAM_FILE = os.path.expanduser("~/.config/minicode/upstream")


def current_upstream() -> str:
    try:
        chosen = open(UPSTREAM_FILE, encoding="utf-8").read().strip()
    except OSError:
        chosen = ""
    return (chosen or UPSTREAM).rstrip("/")
_listen = os.environ.get("MINICODE_LISTEN", "127.0.0.1:11436")
_host, _port = _listen.rsplit(":", 1)
LISTEN = (_host, int(_port))
TOOL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
TOOL_NAMES = "write|shell|edit|read|glob|grep|bash"
NAMED_CALL = re.compile(rf"\b(?P<name>{TOOL_NAMES})\s*\(?\s*\{{", re.IGNORECASE)


def argument_text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def as_call(obj) -> dict | None:
    if not isinstance(obj, dict) or not obj.get("name"):
        return None
    if "arguments" not in obj and "parameters" not in obj:
        return None
    if "arguments" not in obj:
        obj = {"name": obj.get("name"), "arguments": obj.get("parameters")}
    return obj


def parse_calls(text: str) -> list[dict]:
    if is_folder_request(last_user()):
        return correct_calls([])
    found = []
    for raw in TOOL_BLOCK.findall(text or ""):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        call = as_call(obj)
        if call:
            found.append(call)
    if found:
        return found
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body).strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        call = as_call(obj)
        return [call] if call else []
    if isinstance(obj, list):
        calls = [call for item in obj if (call := as_call(item))]
        return calls
    decoder = json.JSONDecoder()
    index = 0
    source = text or ""
    while True:
        start = source.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(source, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        call = as_call(obj)
        if call:
            found.append(call)
    if found:
        return found
    found = named_calls(source)
    if found:
        return found
    found = fence_calls(source)
    if found:
        return found
    return file_calls(source)


def named_calls(source: str) -> list[dict]:
    """coder often writes write{"path":"..."} instead of a name/arguments object."""
    decoder = json.JSONDecoder()
    found = []
    seen = set()
    for match in NAMED_CALL.finditer(source or ""):
        start = match.end() - 1
        try:
            obj, end = decoder.raw_decode(source, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = match.group("name").lower()
        call = as_call(obj) or {"name": name, "arguments": obj}
        if call.get("name") != name and "name" not in obj:
            call = {"name": name, "arguments": obj}
        key = (call.get("name"), argument_text(call.get("arguments", {})))
        if key in seen:
            continue
        seen.add(key)
        found.append(call)
    return found


HOME = os.path.expanduser("~")
CWD_FILE = os.path.expanduser("~/.config/minicode/cwd")
FAKE_HOME = re.compile(
    r"(?:/Users|/home)/(?:your_username|username|your_user|user_name|<username>|<user>)(?=/|[\"'\s]|$)",
    re.IGNORECASE,
)


def work_dir() -> str:
    try:
        path = open(CWD_FILE, encoding="utf-8").read().strip()
    except OSError:
        path = ""
    if path and os.path.isdir(path):
        return path
    desktop = os.path.join(HOME, "Desktop")
    return desktop if os.path.isdir(desktop) else HOME


def ground_text(value: str) -> str:
    return FAKE_HOME.sub(HOME, value or "")


def ground_bytes(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    grounded = ground_text(text)
    if grounded == text:
        return raw
    return grounded.encode("utf-8")


def ground_arguments(value):
    if isinstance(value, str):
        return ground_text(value)
    if isinstance(value, dict):
        return {key: ground_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [ground_arguments(item) for item in value]
    return value


def ground_tool_calls(calls: list) -> None:
    for call in calls or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or "arguments" not in function:
            continue
        function["arguments"] = ground_arguments(function.get("arguments"))


def openai_tool_calls(calls: list[dict]) -> list[dict]:
    out = []
    for index, call in enumerate(calls):
        out.append(
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": call.get("name") or "",
                    "arguments": ground_text(argument_text(call.get("arguments", {}))),
                },
            }
        )
    return out


def ollama_tool_calls(calls: list[dict]) -> list[dict]:
    out = []
    for index, call in enumerate(calls):
        arguments = call.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"input": arguments}
        out.append({"function": {"index": index, "name": call.get("name") or "", "arguments": ground_arguments(arguments)}})
    return out


def rewrite_openai(payload: dict) -> dict:
    choices = payload.get("choices") or []
    if not choices:
        return payload
    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
        ground_tool_calls(message["tool_calls"])
        calls = correct_calls(calls_from_openai(message["tool_calls"]))
        if calls:
            message["tool_calls"] = openai_tool_calls(calls)
            message["content"] = ""
            choices[0]["message"] = message
            choices[0]["finish_reason"] = "tool_calls"
        return payload
    calls = correct_calls(parse_calls(message.get("content") or ""))
    if not calls:
        return payload
    message["tool_calls"] = openai_tool_calls(calls)
    message["content"] = ""
    choices[0]["message"] = message
    choices[0]["finish_reason"] = "tool_calls"
    return payload


def rewrite_ollama(payload: dict) -> dict:
    message = payload.get("message") or {}
    if message.get("tool_calls"):
        ground_tool_calls(message["tool_calls"])
        calls = correct_calls(calls_from_ollama(message["tool_calls"]))
        if calls:
            message["tool_calls"] = ollama_tool_calls(calls)
            message["content"] = ""
            payload["message"] = message
        return payload
    calls = correct_calls(parse_calls(message.get("content") or ""))
    if not calls:
        return payload
    message["tool_calls"] = ollama_tool_calls(calls)
    message["content"] = ""
    payload["message"] = message
    return payload


def collect_stream_text(raw: bytes) -> str:
    parts = []
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices") or []
        if not choices:
            message = event.get("message") or {}
            if message.get("content"):
                parts.append(message["content"])
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            parts.append(delta["content"])
        message = choices[0].get("message") or {}
        if message.get("content"):
            parts.append(message["content"])
    return "".join(parts)


def tool_stream(model: str, calls: list[dict]) -> bytes:
    created = 0
    chunks = []
    for index, call in enumerate(openai_tool_calls(calls)):
        delta = {
            "tool_calls": [
                {
                    "index": index,
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    },
                }
            ]
        }
        if index == 0:
            delta["role"] = "assistant"
        chunks.append(
            {
                "id": "chatcmpl-minicode",
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
        )
    chunks.append(
        {
            "id": "chatcmpl-minicode",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
    )
    lines = [("data: " + json.dumps(chunk, ensure_ascii=False)).encode() + b"\n\n" for chunk in chunks]
    lines.append(b"data: [DONE]\n\n")
    return b"".join(lines)


def rewrite_ollama_stream(raw: bytes) -> bytes:
    parts: list[str] = []
    last = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        last = event
        message = event.get("message") or {}
        if message.get("tool_calls"):
            calls = correct_calls(calls_from_ollama(message["tool_calls"]))
            if calls and last is not None:
                last["done"] = True
                last["message"] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": ollama_tool_calls(calls),
                }
                return json.dumps(last, ensure_ascii=False).encode() + b"\n"
            return ground_bytes(raw)
        if message.get("content"):
            parts.append(message["content"])
    calls = correct_calls(parse_calls("".join(parts)))
    if not calls or last is None:
        return raw
    last["done"] = True
    last["message"] = {
        "role": "assistant",
        "content": "",
        "tool_calls": ollama_tool_calls(calls),
    }
    return json.dumps(last, ensure_ascii=False).encode() + b"\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._forward(b"")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self._forward(self.rfile.read(length))

    def _forward(self, body: bytes) -> None:
        path = self.path
        if self.command == "POST" and (path.startswith("/v1/chat/completions") or path.startswith("/api/chat")):
            body = prepare(body)
        incoming = {}
        try:
            incoming = json.loads(body.decode() or "{}") if body else {}
        except json.JSONDecodeError:
            incoming = {}
        if self.command == "POST" and path.startswith(("/v1/chat/completions", "/api/chat")):
            forced = short_circuit_calls()
            if forced:
                model = incoming.get("model") or "coder"
                if incoming.get("stream") and path.startswith("/v1/chat/completions"):
                    self._send(200, "text/event-stream", tool_stream(model, forced))
                elif path.startswith("/v1/chat/completions"):
                    self._send(200, "application/json", tool_json(model, forced))
                else:
                    self._send(200, "application/json", ollama_tool_json(forced))
                return
        request = urllib.request.Request(
            current_upstream() + path,
            data=body if self.command == "POST" else None,
            method=self.command,
            headers={
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "User-Agent": "minicode",
            },
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self._send(exc.code, exc.headers.get("Content-Type", "application/json"), payload)
            return
        except Exception as exc:
            self._send(502, "text/plain", str(exc).encode())
            return
        wants_stream = bool(incoming.get("stream")) and path.startswith("/v1/chat/completions")
        if wants_stream:
            self._proxy_stream(upstream, incoming)
            return
        raw = upstream.read()
        content_type = upstream.headers.get("Content-Type", "application/json")
        if path.startswith("/v1/chat/completions"):
            try:
                raw = json.dumps(rewrite_openai(json.loads(raw)), ensure_ascii=False).encode()
                content_type = "application/json"
            except json.JSONDecodeError:
                pass
        elif path.startswith("/api/chat"):
            stripped = raw.lstrip()
            if stripped.startswith(b"{") and b"\n{" in raw:
                raw = rewrite_ollama_stream(raw)
                content_type = "application/x-ndjson"
            else:
                try:
                    raw = json.dumps(rewrite_ollama(json.loads(raw)), ensure_ascii=False).encode()
                    content_type = "application/json"
                except json.JSONDecodeError:
                    pass
        self._send(upstream.status, content_type, raw)

    def _proxy_stream(self, upstream, incoming: dict) -> None:
        # Hold the reply until it finishes. A command often sits under a Chinese
        # explanation, and flushing early turns that command into text.
        model = incoming.get("model") or "coder"
        held = bytearray()
        parts: list[str] = []
        native = False
        try:
            while True:
                line = upstream.readline()
                if not line:
                    break
                held.extend(line)
                text, is_tool_delta, _done = sse_delta(line)
                if is_tool_delta:
                    native = True
                if text:
                    parts.append(text)
            if native:
                calls = correct_calls(native_calls_from_sse(bytes(held)))
                if calls:
                    self._send(200, "text/event-stream", tool_stream(model, calls))
                    return
                self._send(200, "text/event-stream", ground_bytes(bytes(held)))
                return
            calls = correct_calls(parse_calls("".join(parts)))
            if calls:
                self._send(200, "text/event-stream", tool_stream(model, calls))
                return
            self._send(200, "text/event-stream", bytes(held))
        finally:
            upstream.close()

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def system_prompt() -> str:
    desktop = os.path.join(HOME, "Desktop")
    return (
        "The shell and the files are already on the user's Mac. "
        "Do not say the command ran on another computer, and do not tell the user to copy files with scp. "
        "You only write and edit files. Folders are created for you. "
        "When the user asks you to create a file, run a command, or open a page, call the tool. "
        "Never use write to make a folder. write always creates a file and needs a suffix like .html or .py. "
        "Never paste the full file in the chat. Never tell the user to save or copy the code. "
        "Use write with path and content, then bash to run or open the file. "
        f"New files go in the current project {work_dir()}. HTML games can also go on {desktop}. "
        "Python is already installed as python3. Never install Python. "
        f"The home directory is {HOME}. The Desktop is {desktop}. "
        "Use these absolute paths in write and bash. Never invent your_username or /path/to."
    )
FENCE = re.compile(r"```(?:sh|bash|shell|zsh|console)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
FILE_FENCE = re.compile(
    r"```(?P<lang>html|htm|css|js|javascript|ts|tsx|jsx|python|py|swift)\s*(?P<name>[^\n`]*)\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)
HTML_START = re.compile(r"(?i)(?:<!DOCTYPE\s+html|<html[\s>])")
PLACEHOLDER = re.compile(r"username|macbook_ip|/path/to|/path/on|<[^>\n]+>|your_", re.IGNORECASE)
KEEP_TOOLS = {"write", "shell", "read", "edit", "bash", "glob", "grep"}
MAX_HISTORY = 6
MAX_CHARS = 1500
_ctx = threading.local()
LANG_EXT = {
    "html": ".html",
    "htm": ".html",
    "css": ".css",
    "js": ".js",
    "javascript": ".js",
    "ts": ".ts",
    "tsx": ".tsx",
    "jsx": ".jsx",
    "python": ".py",
    "py": ".py",
    "swift": ".swift",
}


def fence_calls(source: str) -> list[dict]:
    found = []
    seen = set()
    for block in FENCE.findall(source or ""):
        lines = []
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("$"):
                line = line[1:].strip()
            lines.append(line)
        if len(lines) != 1:
            continue
        command = ground_text(lines[0])
        if not command or command in seen or PLACEHOLDER.search(command):
            continue
        if not re.match(r"^[A-Za-z0-9_./~-]", command):
            continue
        seen.add(command)
        found.append({"name": "shell", "arguments": {"command": command}})
    return found


def user_text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def last_user() -> str:
    return user_text_of(getattr(_ctx, "user", "") or "")


FOLDER_ASK = re.compile(
    r"文件夹|目录|(?<![A-Za-z])folders?(?![A-Za-z])|(?<![A-Za-z])directory(?![A-Za-z])|mkdir",
    re.I,
)
FILE_ASK = re.compile(
    r"文件(?!夹)|html|页面|游戏|代码|脚本|\.html|\.py|\.js|tetris|俄罗斯方块|网页",
    re.I,
)
FOLDER_SKIP = {
    "html",
    "file",
    "文件夹",
    "目录",
    "folder",
    "directory",
    "mkdir",
    "一个",
    "这个",
    "那个",
    "新的",
    "新建",
    "创建",
    "然后",
    "并且",
    "再",
}
NAME_NOISE = re.compile(r"写|页面|html|代码|脚本|游戏|然后|并且|帮我|请|一下|桌面|建立|创建|新建|名为|叫做|的|の")


def is_folder_request(text: str) -> bool:
    return bool(FOLDER_ASK.search(user_text_of(text)))


def is_folder_only(text: str) -> bool:
    text = user_text_of(text)
    return is_folder_request(text) and not FILE_ASK.search(text)


def folder_root(user_text: str) -> str:
    if re.search(r"桌面|Desktop", user_text or "", re.I):
        desktop = os.path.join(HOME, "Desktop")
        if os.path.isdir(desktop):
            return desktop
    return work_dir()


def looks_like_folder_name(name: str) -> bool:
    name = (name or "").strip()
    if not name or name.lower() in FOLDER_SKIP:
        return False
    if NAME_NOISE.search(name) or len(name) > 40:
        return False
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,40}", name):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{1,8}", name):
        return True
    return False


def folder_path(user_text: str) -> str:
    text = user_text_of(user_text)
    root = folder_root(text)
    candidates: list[str] = []
    for match in re.finditer(r"(?:名为|叫做|名叫|叫|name(?:d)?)\s*[「『\"'`]?([A-Za-z][A-Za-z0-9._-]{0,40}|[\u4e00-\u9fff]{1,8})", text, re.I):
        candidates.append(match.group(1))
    for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9._-]{0,40})\s*(?:文件夹|目录|folder)", text, re.I):
        candidates.append(match.group(1))
    for match in re.finditer(r"[「『\"'`]([^「『\"'`]{1,40})[」』\"'`]", text):
        candidates.append(match.group(1))
    for match in re.finditer(r"mkdir(?:\s+-p)?\s+([~\w./-]+)", text, re.I):
        raw = match.group(1)
        if raw.startswith("~") or raw.startswith("/"):
            return ground_text(os.path.expanduser(raw))
        candidates.append(os.path.basename(raw))
    # last latin token before 文件夹, e.g. 建立一个test文件夹
    for match in re.finditer(r"([A-Za-z][A-Za-z0-9._-]{0,40})(?=\s*(?:的)?(?:文件夹|目录|folder)\b)", text, re.I):
        candidates.append(match.group(1))
    if re.search(r"(?:文件夹|目录|folder)", text, re.I):
        for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9._-]{0,40})(?![A-Za-z0-9])", text):
            candidates.append(match.group(1))
    for name in candidates:
        if looks_like_folder_name(name):
            return os.path.join(root, name)
    return ""


def mkdir_call(path: str) -> dict:
    return {"name": "bash", "arguments": {"command": f"mkdir -p {shlex.quote(path)}"}}


def short_circuit_calls() -> list[dict]:
    user = last_user()
    if not is_folder_only(user):
        return []
    dest = folder_path(user)
    if not dest:
        return []
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        pass
    return [mkdir_call(dest)]


def tool_json(model: str, calls: list[dict]) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-minicode",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": openai_tool_calls(calls),
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        ensure_ascii=False,
    ).encode()


def ollama_tool_json(calls: list[dict]) -> bytes:
    return json.dumps(
        {
            "model": "coder",
            "done": True,
            "message": {"role": "assistant", "content": "", "tool_calls": ollama_tool_calls(calls)},
        },
        ensure_ascii=False,
    ).encode()


def decode_args(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"path": value} if value else {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def calls_from_openai(tool_calls: list) -> list[dict]:
    out = []
    for item in tool_calls or []:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            continue
        out.append({"name": function.get("name") or "", "arguments": decode_args(function.get("arguments"))})
    return out


def calls_from_ollama(tool_calls: list) -> list[dict]:
    out = []
    for item in tool_calls or []:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            continue
        out.append({"name": function.get("name") or "", "arguments": decode_args(function.get("arguments"))})
    return out


def native_calls_from_sse(raw: bytes) -> list[dict]:
    acc: dict[int, dict[str, str]] = {}
    for line in raw.splitlines():
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = event.get("choices") or []
        items = []
        if choices:
            delta = choices[0].get("delta") or {}
            message = choices[0].get("message") or {}
            items.extend(delta.get("tool_calls") or [])
            items.extend(message.get("tool_calls") or [])
        else:
            items.extend((event.get("message") or {}).get("tool_calls") or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            index = int(item.get("index") or 0)
            cur = acc.setdefault(index, {"name": "", "arguments": ""})
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            if function.get("name"):
                cur["name"] = str(function["name"])
            if function.get("arguments"):
                cur["arguments"] += function["arguments"] if isinstance(function["arguments"], str) else json.dumps(function["arguments"])
    out = []
    for index in sorted(acc):
        item = acc[index]
        out.append({"name": item.get("name") or "", "arguments": decode_args(item.get("arguments"))})
    return out


def correct_calls(calls: list[dict]) -> list[dict]:
    user = last_user()
    folder = is_folder_request(user)
    out: list[dict] = []
    for call in calls or []:
        name = str(call.get("name") or "").lower()
        args = decode_args(call.get("arguments"))
        path = str(args.get("path") or args.get("filePath") or "")
        content = args.get("content") if args.get("content") is not None else args.get("contents")
        content_s = content if isinstance(content, str) else ""
        if folder and name == "write":
            dest = folder_path(user)
            if dest:
                try:
                    os.makedirs(dest, exist_ok=True)
                except OSError:
                    pass
                out.append(mkdir_call(dest))
            continue
        if name == "write" and not content_s.strip() and path and not os.path.splitext(os.path.basename(path))[1]:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass
            out.append(mkdir_call(path))
            continue
        if name == "write" and path:
            parent = os.path.dirname(path)
            if parent and parent not in {HOME, "/", work_dir()}:
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass
                out.append(mkdir_call(parent))
        out.append(call)
    if folder and not any(str(item.get("name") or "") == "bash" and "mkdir" in str((item.get("arguments") or {}).get("command") or "") for item in out):
        dest = folder_path(user)
        if dest:
            try:
                os.makedirs(dest, exist_ok=True)
            except OSError:
                pass
            out.insert(0, mkdir_call(dest))
    return out


def file_path(name: str, lang: str, body: str, user_text: str) -> str:
    root = work_dir()
    ext = LANG_EXT.get(lang.lower(), ".txt")
    raw = (name or "").strip().strip('"').strip("'")
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    if raw and os.path.isabs(raw):
        return raw
    if raw and re.search(r"\.[A-Za-z0-9]{1,8}$", raw) and "/" not in raw and "\\" not in raw:
        return os.path.join(root, raw)
    hinted = re.search(r"((?:/Users/[^/\s]+/|~/|Desktop/)[\w./-]+\.[A-Za-z0-9]{1,8})", user_text or "")
    if hinted:
        return ground_text(os.path.expanduser(hinted.group(1)))
    named = re.search(r"([\w.-]+\.(html|htm|css|js|py|swift))\b", user_text or "", re.I)
    if named:
        return os.path.join(root, named.group(1))
    if re.search(r"俄罗斯方块|tetris", user_text or "", re.I) and ext == ".html":
        return os.path.join(root, "tetris.html")
    title = re.search(r"<title>\s*([^<]+?)\s*</title>", body or "", re.I | re.S)
    if title and ext == ".html":
        slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title.group(1).strip()).strip("-")
        if slug:
            return os.path.join(root, slug[:40] + ".html")
    return os.path.join(root, "index" + ext)


def wants_run(user_text: str) -> bool:
    return bool(re.search(r"运行|跑起来|执行|浏览器|打开页面|open|run|launch|play", user_text or "", re.I))


def wants_page(user_text: str) -> bool:
    return bool(FILE_ASK.search(user_text or ""))


def run_calls(path: str, lang: str, user_text: str) -> list[dict]:
    lang = (lang or "").lower()
    quoted = shlex.quote(path)
    if lang in ("html", "htm") or path.endswith((".html", ".htm")):
        if wants_run(user_text) or wants_page(user_text):
            return [{"name": "bash", "arguments": {"command": f"open {quoted}"}}]
        return []
    if lang in ("python", "py") or path.endswith(".py"):
        if wants_run(user_text):
            return [{"name": "bash", "arguments": {"command": f"python3 {quoted}"}}]
    return []


def file_calls(source: str) -> list[dict]:
    user_text = last_user()
    if is_folder_only(user_text) or not wants_page(user_text):
        return []
    found = []
    seen = set()
    for match in FILE_FENCE.finditer(source or ""):
        lang = (match.group("lang") or "txt").lower()
        body = match.group("body") or ""
        if len(body.strip()) < 30:
            continue
        path = file_path(match.group("name") or "", lang, body, user_text)
        if path in seen:
            continue
        seen.add(path)
        found.append({"name": "write", "arguments": {"path": path, "content": body}})
        found.extend(run_calls(path, lang, user_text))
    if found:
        return found
    text = source or ""
    hit = HTML_START.search(text)
    if not hit:
        return []
    html = text[hit.start() :]
    end = html.lower().rfind("</html>")
    if end >= 0:
        html = html[: end + 7]
    if len(html) < 80:
        return []
    path = file_path("", "html", html, user_text)
    return [
        {"name": "write", "arguments": {"path": path, "content": html}},
        *run_calls(path, "html", user_text),
    ]


def classify_reply(text: str, done: bool) -> str | None:
    source = (text or "").lstrip()
    if not source:
        return "chat" if done else None
    if source.startswith(("{", "<tool_call>", "```", "<!DOCTYPE", "<!doctype", "<html", "<HTML")):
        return "tool"
    if HTML_START.match(source):
        return "tool"
    if NAMED_CALL.match(source):
        return "tool"
    if len(source) >= 8 and not any(mark in source[:8] for mark in "{<`"):
        return "chat"
    if done or len(source) >= 64:
        return "tool" if (NAMED_CALL.search(source) or '"arguments"' in source or HTML_START.search(source)) else "chat"
    return None


def sse_delta(line: bytes) -> tuple[str, bool, bool]:
    if not line.startswith(b"data:"):
        return "", False, False
    data = line[5:].strip()
    if data == b"[DONE]":
        return "", False, True
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return "", False, False
    choices = event.get("choices") or []
    if not choices:
        message = event.get("message") or {}
        return message.get("content") or "", bool(message.get("tool_calls")), bool(event.get("done"))
    choice = choices[0]
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}
    text = delta.get("content") or message.get("content") or ""
    is_tool = bool(delta.get("tool_calls") or message.get("tool_calls"))
    return text, is_tool, choice.get("finish_reason") is not None


def compact_tool(tool: dict) -> dict | None:
    function = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(function, dict):
        return None
    name = function.get("name") or ""
    if name not in KEEP_TOOLS:
        return None
    parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
    properties = parameters.get("properties") if isinstance(parameters.get("properties"), dict) else {}
    compact = {}
    for key, spec in properties.items():
        if isinstance(spec, dict) and spec.get("type"):
            compact[key] = {"type": spec.get("type")}
        else:
            compact[key] = {"type": "string"}
    required = parameters.get("required") if isinstance(parameters.get("required"), list) else list(compact)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": compact, "required": required},
        },
    }


def trim_message(message: dict, limit: int) -> dict:
    content = message.get("content")
    if isinstance(content, str) and len(content) > limit:
        message = dict(message)
        message["content"] = content[:limit]
    return message


def prepare(body: bytes) -> bytes:
    if not body:
        return body
    try:
        incoming = json.loads(body)
    except json.JSONDecodeError:
        return body
    messages = incoming.get("messages")
    if not isinstance(messages, list):
        return body
    kept = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") in ("system", "developer"):
            continue
        kept.append(message)
    if len(kept) > MAX_HISTORY:
        kept = kept[-MAX_HISTORY:]
    trimmed = []
    last = len(kept) - 1
    for index, message in enumerate(kept):
        limit = 8000 if index == last and message.get("role") == "user" else MAX_CHARS
        trimmed.append(trim_message(message, limit))
    incoming["messages"] = [{"role": "system", "content": system_prompt()}, *trimmed]
    user_text = ""
    for message in reversed(trimmed):
        if message.get("role") == "user":
            user_text = user_text_of(message.get("content"))
            break
    _ctx.user = user_text
    tools = incoming.get("tools")
    if isinstance(tools, list):
        compact = [tool for item in tools if (tool := compact_tool(item))]
        if is_folder_only(user_text):
            compact = [tool for tool in compact if (tool.get("function") or {}).get("name") in {"bash", "shell"}]
        if compact:
            incoming["tools"] = compact
        else:
            incoming.pop("tools", None)
    return json.dumps(incoming, ensure_ascii=False).encode()


def main() -> None:
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
