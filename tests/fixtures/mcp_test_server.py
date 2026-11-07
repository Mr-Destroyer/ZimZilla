#!/usr/bin/env python3
"""A minimal MCP server over stdio, for testing the client.

Speaks the spec's newline-delimited JSON-RPC: initialize, tools/list, tools/call.
"""
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails, to test isError.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "test-server", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(args.get("text", ""))}],
                    "isError": False,
                }})
            elif name == "add":
                total = float(args.get("a", 0)) + float(args.get("b", 0))
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(total)}],
                    "isError": False,
                }})
            elif name == "fail":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "this tool always fails"}],
                    "isError": True,
                }})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": f"unknown tool: {name}"}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": f"unknown method: {method}"}})


if __name__ == "__main__":
    main()
