"""MCP — the Model Context Protocol, as a source of tools.

An MCP server exposes tools over JSON-RPC. This module connects to one, lists
what it offers, and calls them — so a ZimZilla session can use any MCP server
the operator points it at, without the harness knowing anything about what that
server does.

Why a module of its own
-----------------------
The tool layer in :mod:`zimzilla.tools` is a fixed set: a schema list, a
dispatch table, and a handler per tool. MCP is the opposite — the tool set is
discovered at runtime and belongs to somebody else. Threading that through
``tools.py`` would put a network client inside the module that exists to be the
static tool set, so the connection lives here and ``tools.py`` gains one
dispatch entry that forwards to it.

Transports
----------
Two, and they are the two the spec defines for a client:

* **stdio** — the server is a child process speaking JSON-RPC over its own
  stdin/stdout, one JSON object per line. This is what ``npx``-style servers
  use, and it is the common case.
* **HTTP** — the server is a URL. Streamable HTTP (``POST`` with an optional
  ``Mcp-Session-Id``) is what the current spec defines; the older HTTP+SSE
  transport is not implemented, because a server that only speaks it is rare
  and the failure is a clear one rather than a silent one.

What is deliberately not implemented
------------------------------------
Resources, prompts, sampling, elicitation and roots. This is a *tool* client:
the model gets tools, which is what an agent harness needs. A server that also
offers resources still works — its tools are listed and called, and the rest is
ignored, which the spec allows.

Security posture
----------------
An MCP server is code the operator chose to run, and its tools are as powerful
as the server makes them. Two consequences, both enforced here rather than
documented and hoped for:

* **A server's tools are gated like ``bash``.** They are not in
  ``GATED_TOOLS`` — that set is static and these names are not known until a
  connection exists — so :func:`zimzilla.tools.needs_permission` consults this
  module for the dynamic answer. A tool the operator has not seen before does
  not run unattended.
* **Plan mode denies them.** A tool whose effect is unknown cannot be called
  read-only, so ``config.MODES["plan"]`` denies the whole family by prefix.

Nothing here echoes a credential. A server's ``env`` is passed to the child
process and never logged; an HTTP server's headers are the same.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: Where the operator's server list lives. JSON, because it is a list of
#: objects with optional nested fields and an operator may well hand-edit it.
CONFIG_PATH = ZIMZILLA_HOME / "mcp.json"

#: The protocol version this client speaks. Sent on ``initialize``; a server
#: that answers with a different one is accepted as long as it is a version
#: this client understands, which for the tool surface is any of these — the
#: ``tools/list`` and ``tools/call`` shapes have not changed across them.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: How long a single request may take. A tool call is the slow one — a server
#: may be doing real work — so the ceiling is generous, but finite: a hung
#: server must not hang the turn.
DEFAULT_TIMEOUT = 60.0

#: How long a stdio server gets to start and answer ``initialize``. Shorter
#: than a tool call: a server that cannot initialise in this long is broken,
#: and the operator is waiting on a command that should be instant.
STARTUP_TIMEOUT = 30.0

#: The prefix every MCP tool is advertised under. Namespacing is not cosmetic:
#: two servers may both offer ``search``, and a bare name would collide with
#: each other and with the harness's own tools. ``mcp_<server>_<tool>`` is
#: unique by construction and tells the operator where a call went.
TOOL_PREFIX = "mcp_"

#: Characters a tool name may contain once namespaced. The model-facing name
#: has to survive being a JSON key and a function name, so anything else is
#: folded to an underscore rather than passed through.
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class MCPError(Exception):
    """A connection or protocol failure, with a message worth showing."""


def _safe(text: str) -> str:
    """Fold *text* to the character set a tool name may use."""
    return "".join(c if c in _SAFE else "_" for c in str(text))


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """One entry from ``mcp.json``.

    ``command`` is a stdio server (a child process); ``url`` is an HTTP one.
    Exactly one of them is required, and :meth:`validate` is what says so
    rather than letting a half-configured entry fail at connect time with a
    message about a missing key.
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def validate(self) -> str | None:
        """Why this entry cannot be used, or ``None`` when it is usable."""
        if not self.name:
            return "an entry has no name"
        if self.command and self.url:
            return f"{self.name}: give either command or url, not both"
        if not self.command and not self.url:
            return f"{self.name}: needs a command (stdio) or a url (http)"
        if self.url and not self.url.startswith(("http://", "https://")):
            return f"{self.name}: url must start with http:// or https://"
        if self.command and not shutil.which(self.command):
            return f"{self.name}: command not found on PATH: {self.command}"
        return None


def load_config(path: Path | None = None) -> list[ServerConfig]:
    """Read the server list. A missing file is an empty list, not an error.

    A malformed file *is* an error, and it is raised rather than swallowed: an
    operator who has just edited the JSON wants to be told it does not parse,
    not shown an empty list that looks like their servers vanished.
    """
    path = path or CONFIG_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise MCPError(f"cannot read {path}: {e}") from e
    entries = raw.get("servers") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise MCPError(f"{path}: expected a list of servers, or {{\"servers\": [...]}}")
    servers: list[ServerConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        servers.append(ServerConfig(
            name=str(entry.get("name") or "").strip(),
            command=str(entry.get("command") or "").strip(),
            args=[str(a) for a in (entry.get("args") or [])],
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            url=str(entry.get("url") or "").strip(),
            headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
            enabled=bool(entry.get("enabled", True)),
            timeout=float(entry.get("timeout") or DEFAULT_TIMEOUT),
        ))
    return servers


def save_config(servers: list[ServerConfig], path: Path | None = None) -> Path:
    """Write the server list, mode 600 — it may hold tokens in ``env``."""
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"servers": [
        {k: v for k, v in {
            "name": s.name,
            "command": s.command or None,
            "args": s.args or None,
            "env": s.env or None,
            "url": s.url or None,
            "headers": s.headers or None,
            "enabled": s.enabled,
            "timeout": s.timeout if s.timeout != DEFAULT_TIMEOUT else None,
        }.items() if v is not None}
        for s in servers
    ]}
    path.write_text(json.dumps(body, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class _StdioTransport:
    """A child process speaking JSON-RPC, one object per line.

    The spec's stdio framing is newline-delimited JSON on the server's stdin
    and stdout. Nothing else goes on those streams — a server that prints a
    banner to stdout breaks the protocol, which is why the failure is reported
    as "not JSON" rather than silently skipped.
    """

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        env = dict(os.environ)
        env.update(self.cfg.env)
        try:
            self.proc = subprocess.Popen(
                [self.cfg.command, *self.cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            raise MCPError(f"{self.cfg.name}: cannot start {self.cfg.command}: {e}") from e

    def send(self, message: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError(f"{self.cfg.name}: not started")
        line = json.dumps(message) + "\n"
        with self._lock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise MCPError(f"{self.cfg.name}: the server closed its input: {e}") from e

    def read(self, timeout: float) -> dict:
        """Read one JSON object, skipping notifications.

        A server may interleave notifications (``notifications/tools/
        list_changed``, logging) with responses. Those have no ``id`` and are
        not what the caller is waiting for, so they are read and dropped rather
        than returned as a malformed response.
        """
        if self.proc is None or self.proc.stdout is None:
            raise MCPError(f"{self.cfg.name}: not started")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(f"{self.cfg.name}: timed out after {timeout:.0f}s")
            line = self._readline(remaining)
            if line is None:
                raise MCPError(f"{self.cfg.name}: the server exited")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # A banner or a log line on stdout. Named rather than skipped:
                # it is the single most common way a stdio server is broken,
                # and "not JSON" with the text is what makes it fixable.
                raise MCPError(
                    f"{self.cfg.name}: the server wrote non-JSON to stdout: "
                    f"{line[:200]!r}") from None
            if isinstance(obj, dict) and "id" in obj:
                return obj
            # A notification or a server-initiated request. Neither is a reply
            # to anything this client sent, so it is dropped.

    def _readline(self, timeout: float) -> str | None:
        """One line from stdout, or ``None`` at EOF.

        ``readline`` on a pipe blocks with no timeout, so the read happens on a
        worker thread and the wait is bounded here. A thread left blocked on a
        dead server is not a leak worth avoiding — the process is killed in
        :meth:`close`, which unblocks it.
        """
        result: list[str | None] = [None]

        def _read() -> None:
            try:
                result[0] = self.proc.stdout.readline()  # type: ignore[union-attr]
            except (ValueError, OSError):
                result[0] = None

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            return None
        return result[0]

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class _HTTPTransport:
    """A server reached over Streamable HTTP.

    Every request is a ``POST`` carrying one JSON-RPC object. The response is
    either a JSON object or an SSE stream; both are handled, because the spec
    allows either and servers differ. A ``Mcp-Session-Id`` returned on
    ``initialize`` is echoed on every later request, which is how the server
    ties them together.
    """

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.session_id = ""
        self.protocol_version = ""

    def start(self) -> None:
        # Nothing to start: the URL is the server. A bad URL is caught by
        # ``validate`` before this is ever reached.
        return

    def send(self, message: dict) -> None:
        # Requests are sent and their replies read in one call, because HTTP
        # has no separate write and read. ``request`` is the real entry point.
        raise MCPError("internal: use request() for the HTTP transport")

    def request(self, message: dict, timeout: float) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "zimzilla",
        }
        headers.update(self.cfg.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        body = json.dumps(message).encode()
        req = urllib.request.Request(self.cfg.url, data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                sid = response.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = response.read().decode("utf-8", "replace")
                ctype = (response.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except OSError:
                pass
            raise MCPError(
                f"{self.cfg.name}: HTTP {e.code}"
                + (f": {detail}" if detail else "")) from e
        except (urllib.error.URLError, OSError) as e:
            raise MCPError(f"{self.cfg.name}: cannot reach {self.cfg.url}: {e}") from e

        if "text/event-stream" in ctype:
            return self._from_sse(raw)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise MCPError(
                f"{self.cfg.name}: the server answered with something that is "
                f"not JSON: {raw[:200]!r}") from None
        if not isinstance(obj, dict):
            raise MCPError(f"{self.cfg.name}: expected a JSON object, got {type(obj).__name__}")
        return obj

    @staticmethod
    def _from_sse(raw: str) -> dict:
        """The first JSON-RPC response in an SSE body."""
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "id" in obj:
                return obj
        raise MCPError("the server's event stream carried no response")

    def close(self) -> None:
        return


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


@dataclass
class MCPTool:
    """One tool as the server described it."""

    server: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    @property
    def exposed_name(self) -> str:
        """The name the model sees: ``mcp_<server>_<tool>``."""
        return f"{TOOL_PREFIX}{_safe(self.server)}_{_safe(self.name)}"

    def schema(self) -> dict:
        """The tool in the shape ``tools.TOOL_SCHEMAS`` uses."""
        params = self.input_schema
        if not isinstance(params, dict) or params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        where = f"[{self.server}] " if self.server else ""
        return {
            "name": self.exposed_name,
            "description": (where + (self.description or self.name)).strip(),
            "input_schema": params,
        }


class Client:
    """One connection to one MCP server."""

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.transport: _StdioTransport | _HTTPTransport
        if cfg.transport == "http":
            self.transport = _HTTPTransport(cfg)
        else:
            self.transport = _StdioTransport(cfg)
        self.tools: list[MCPTool] = []
        self.server_info: dict = {}
        self._next_id = 0
        self._connected = False

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        """Start the transport and run the ``initialize`` handshake."""
        problem = self.cfg.validate()
        if problem:
            raise MCPError(problem)
        self.transport.start()
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "zimzilla", "version": "1.0.0"},
        }, timeout=STARTUP_TIMEOUT)
        version = str(result.get("protocolVersion") or "")
        if version and version not in SUPPORTED_VERSIONS:
            raise MCPError(
                f"{self.cfg.name}: the server speaks protocol {version}, which "
                f"this client does not ({', '.join(SUPPORTED_VERSIONS)})")
        if isinstance(self.transport, _HTTPTransport):
            self.transport.protocol_version = version or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo") or {}
        # The spec requires this notification before any other request. A
        # server that does not get it may refuse everything that follows.
        self._notify("notifications/initialized")
        self._connected = True

    def list_tools(self) -> list[MCPTool]:
        """Every tool the server offers, following pagination."""
        tools: list[MCPTool] = []
        cursor = ""
        for _ in range(50):  # a page cap, so a broken server cannot loop us
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            for entry in result.get("tools") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                tools.append(MCPTool(
                    server=self.cfg.name,
                    name=str(entry["name"]),
                    description=str(entry.get("description") or ""),
                    input_schema=entry.get("inputSchema") or {},
                ))
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                break
        self.tools = tools
        return tools

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """Call one tool. Returns ``(text, is_error)``.

        The result's ``content`` is a list of typed blocks. Text is joined;
        anything else is described rather than dropped, so a model that gets an
        image back is told it got one instead of being handed an empty string.
        """
        result = self._request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        }, timeout=self.cfg.timeout)
        is_error = bool(result.get("isError"))
        parts: list[str] = []
        for block in result.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "image":
                parts.append(f"[image: {block.get('mimeType') or 'unknown type'}, "
                             f"{len(str(block.get('data') or ''))} base64 chars]")
            elif kind == "audio":
                parts.append(f"[audio: {block.get('mimeType') or 'unknown type'}]")
            elif kind == "resource":
                res = block.get("resource") or {}
                uri = res.get("uri") or ""
                text = res.get("text")
                parts.append(f"[resource {uri}]\n{text}" if text else f"[resource {uri}]")
            elif kind == "resource_link":
                parts.append(f"[resource link: {block.get('uri') or ''}]")
            else:
                parts.append(f"[{kind}]")
        # Structured content is the machine-readable twin of the text blocks.
        # Appended when there is no text, so a tool that returns only
        # ``structuredContent`` is not reported as having returned nothing.
        if not parts and isinstance(result.get("structuredContent"), (dict, list)):
            parts.append(json.dumps(result["structuredContent"], indent=2))
        return "\n".join(p for p in parts if p), is_error

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self._connected = False

    # -- JSON-RPC --------------------------------------------------------
    def _request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id,
                   "method": method, "params": params}
        wait = timeout if timeout is not None else self.cfg.timeout
        if isinstance(self.transport, _HTTPTransport):
            reply = self.transport.request(message, wait)
        else:
            self.transport.send(message)
            reply = self.transport.read(wait)
        if reply.get("error"):
            error = reply["error"]
            if isinstance(error, dict):
                raise MCPError(
                    f"{self.cfg.name}: {error.get('message') or error.get('code')}")
            raise MCPError(f"{self.cfg.name}: {error}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        if isinstance(self.transport, _HTTPTransport):
            # A notification has no reply. Sending it and ignoring the body is
            # correct; a server that answers 202 with nothing is the norm.
            try:
                self.transport.request(message, STARTUP_TIMEOUT)
            except MCPError:
                pass
            return
        self.transport.send(message)


# ---------------------------------------------------------------------------
# the registry — one connection per server, shared by the session
# ---------------------------------------------------------------------------


class Registry:
    """The session's live MCP connections.

    One connection per server, opened lazily and kept for the session. A tool
    call is a round trip, so reconnecting per call would pay the handshake on
    every one — and for a stdio server that means starting a process each time.
    """

    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def connect_all(self, servers: list[ServerConfig] | None = None) -> None:
        """Connect every enabled server, recording per-server failures.

        A server that will not start does not stop the others: one broken entry
        in ``mcp.json`` must not cost the operator every working one. The
        failure is kept in :attr:`errors` and shown by ``/mcp``.
        """
        servers = servers if servers is not None else load_config()
        for cfg in servers:
            if not cfg.enabled:
                continue
            try:
                client = Client(cfg)
                client.connect()
                client.list_tools()
                with self._lock:
                    self.clients[cfg.name] = client
                    self.errors.pop(cfg.name, None)
            except MCPError as e:
                with self._lock:
                    self.errors[cfg.name] = str(e)

    def tools(self) -> list[MCPTool]:
        with self._lock:
            return [t for c in self.clients.values() for t in c.tools]

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools()]

    def find(self, exposed_name: str) -> tuple[Client, MCPTool] | None:
        for client in self.clients.values():
            for tool in client.tools:
                if tool.exposed_name == exposed_name:
                    return client, tool
        return None

    def call(self, exposed_name: str, arguments: dict) -> tuple[str, bool]:
        found = self.find(exposed_name)
        if found is None:
            return f"no MCP tool named {exposed_name}", True
        client, tool = found
        try:
            return client.call_tool(tool.name, arguments)
        except MCPError as e:
            return str(e), True

    def close(self) -> None:
        with self._lock:
            for client in self.clients.values():
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - a close must not raise
                    pass
            self.clients.clear()


#: The session's registry. Module-level because the tool layer is a set of
#: plain functions with no object to hang it on, and because there is exactly
#: one session per process.
_REGISTRY = Registry()


def registry() -> Registry:
    return _REGISTRY


def is_mcp_tool(name: str) -> bool:
    """Whether *name* is an MCP tool rather than one of the harness's own."""
    return str(name).startswith(TOOL_PREFIX)


def has_servers() -> bool:
    """Whether any server is configured. Cheap — a file existence check."""
    try:
        return bool(load_config())
    except MCPError:
        return False


def available() -> bool:
    """Whether any MCP tool is currently callable."""
    return bool(_REGISTRY.tools())
