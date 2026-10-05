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
_listen = os.environ.get("MINICODE_LISTEN", "127.0.0.1:11436")
_host, _port = _listen.rsplit(":", 1)
LISTEN = (_host, int(_port))
TOOL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


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
    return found


def openai_tool_calls(calls: list[dict]) -> list[dict]:
    out = []
    for index, call in enumerate(calls):
        out.append(
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": call.get("name") or "",
                    "arguments": argument_text(call.get("arguments", {})),
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
        out.append({"function": {"index": index, "name": call.get("name") or "", "arguments": arguments}})
    return out


def rewrite_openai(payload: dict) -> dict:
    choices = payload.get("choices") or []
    if not choices:
        return payload
    message = choices[0].get("message") or {}
    if message.get("tool_calls"):
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
            return raw
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
        request = urllib.request.Request(
            UPSTREAM + path,
            data=body if self.command == "POST" else None,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
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
        raw = upstream.read()
        content_type = upstream.headers.get("Content-Type", "application/json")
        wants_stream = False
        try:
            incoming = json.loads(body.decode() or "{}") if body else {}
            wants_stream = bool(incoming.get("stream"))
        except json.JSONDecodeError:
            incoming = {}
        if path.startswith("/v1/chat/completions") and wants_stream:
            text = collect_stream_text(raw)
            calls = parse_calls(text)
            if calls:
                model = incoming.get("model") or "coder"
                self._send(200, "text/event-stream", tool_stream(model, calls))
                return
        elif path.startswith("/v1/chat/completions"):
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

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
