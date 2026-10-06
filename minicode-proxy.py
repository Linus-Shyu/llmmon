#!/usr/bin/env python3
"""Turn coder's plain JSON tool calls into the tool_calls field clients expect."""

from __future__ import annotations

import json
import os
import re
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
    return fence_calls(source)


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
FAKE_HOME = re.compile(
    r"(?:/Users|/home)/(?:your_username|username|your_user|user_name|<username>|<user>)(?=/|[\"'\s]|$)",
    re.IGNORECASE,
)


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
        return payload
    calls = parse_calls(message.get("content") or "")
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
        return payload
    calls = parse_calls(message.get("content") or "")
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
            return ground_bytes(raw)
        if message.get("content"):
            parts.append(message["content"])
    calls = parse_calls("".join(parts))
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
                self._send(200, "text/event-stream", ground_bytes(bytes(held)))
                return
            calls = parse_calls("".join(parts))
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
        "When the user asks you to create a file, run a command, or open a page, call the tool and do not print a command for them to copy. "
        "Use write, then shell. Open a page with shell command open and the url. "
        "Python is already installed as python3. Never install Python. "
        f"The home directory is {HOME}. The Desktop is {desktop}. "
        "Use these absolute paths in write and shell. Never invent your_username or /path/to."
    )
FENCE = re.compile(r"```(?:sh|bash|shell|zsh|console)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
PLACEHOLDER = re.compile(r"username|macbook_ip|/path/to|/path/on|<[^>\n]+>|your_", re.IGNORECASE)
KEEP_TOOLS = {"write", "shell", "read", "edit", "bash"}
MAX_HISTORY = 6
MAX_CHARS = 1500


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


def classify_reply(text: str, done: bool) -> str | None:
    source = (text or "").lstrip()
    if not source:
        return "chat" if done else None
    if source.startswith(("{", "<tool_call>", "```")):
        return "tool"
    if NAMED_CALL.match(source):
        return "tool"
    if len(source) >= 8 and not any(mark in source[:8] for mark in "{<`"):
        return "chat"
    if done or len(source) >= 64:
        return "tool" if (NAMED_CALL.search(source) or '"arguments"' in source) else "chat"
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
    tools = incoming.get("tools")
    if isinstance(tools, list):
        compact = [tool for item in tools if (tool := compact_tool(item))]
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
