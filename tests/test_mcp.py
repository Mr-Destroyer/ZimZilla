"""Regression suite for the MCP client.

Run:  .venv/bin/python tests/test_mcp.py

MCP is the one part of the tool layer whose tool set is not known at build
time: it is discovered from a server at runtime. That is what these tests
protect, and it is why they run against a real server process rather than a
mock — the protocol is the thing under test, and a mock would only prove the
mock agrees with itself.

``tests/fixtures/mcp_test_server.py`` is a minimal stdio server speaking the
spec's newline-delimited JSON-RPC. It offers three tools: ``echo``, ``add`` and
``fail`` (which returns ``isError: true``).

What is covered:

* **The handshake.** ``initialize`` -> ``notifications/initialized`` ->
  ``tools/list``, in that order, with the protocol version negotiated.
* **Namespacing.** Two servers may both offer ``search``; the exposed name is
  ``mcp_<server>_<tool>`` so they cannot collide with each other or with the
  harness's own tools.
* **The tool layer sees them.** ``execute`` forwards to the registry,
  ``needs_permission`` gates them, ``build_preview`` shows the call, and
  ``summarise_call`` labels it.
* **Plan mode denies them.** Their effect is decided by the server, so they
  cannot be called read-only — denied by prefix, because the names are not
  known until a connection exists.
* **A broken server does not take the others down.** One entry that will not
  start is recorded and the rest still connect.
* **The config round-trips** and is written mode 600, because an entry may
  carry a token in ``env``.

No test here touches the network: the only server is a local child process.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import mcp  # noqa: E402
from zimzilla import tools  # noqa: E402
from zimzilla.config import Config, MODES  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

SERVER = str(ROOT / "tests" / "fixtures" / "mcp_test_server.py")


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _server(name: str = "test") -> mcp.ServerConfig:
    return mcp.ServerConfig(name=name, command=sys.executable, args=[SERVER])


# ---------------------------------------------------------------------------
# the protocol
# ---------------------------------------------------------------------------


def test_handshake_and_listing() -> None:
    """initialize -> initialized -> tools/list, and the tools come back."""
    client = mcp.Client(_server())
    try:
        client.connect()
        check("handshake: the server identified itself",
              client.server_info.get("name") == "test-server",
              str(client.server_info))
        listed = client.list_tools()
        names = sorted(t.name for t in listed)
        check("handshake: every tool is listed", names == ["add", "echo", "fail"],
              str(names))
        echo = next(t for t in listed if t.name == "echo")
        check("handshake: the description is carried",
              "Echo the given text" in echo.description, echo.description)
        check("handshake: the input schema is carried",
              echo.input_schema.get("properties", {}).get("text", {}).get("type")
              == "string", str(echo.input_schema))
        check("handshake: the required list is carried",
              echo.input_schema.get("required") == ["text"],
              str(echo.input_schema.get("required")))
    finally:
        client.close()


def test_calling_tools() -> None:
    """A call returns the server's text, and isError is honoured."""
    client = mcp.Client(_server())
    try:
        client.connect()
        client.list_tools()
        text, is_error = client.call_tool("echo", {"text": "hello world"})
        check("call: the text comes back", text == "hello world", repr(text))
        check("call: it is not an error", is_error is False, str(is_error))

        text, is_error = client.call_tool("add", {"a": 2, "b": 40})
        check("call: arguments are passed through", text == "42.0", repr(text))

        text, is_error = client.call_tool("fail", {})
        check("call: isError is honoured", is_error is True, str(is_error))
        check("call: the error text is kept",
              "always fails" in text, repr(text))
    finally:
        client.close()


def test_a_bad_server_is_reported_not_raised() -> None:
    """A server that will not start gives a message, not a traceback."""
    cfg = mcp.ServerConfig(name="nope", command="definitely-not-a-real-command")
    problem = cfg.validate()
    check("validate: a missing command is caught before connecting",
          problem is not None and "not found" in problem, str(problem))

    client = mcp.Client(cfg)
    raised = None
    try:
        client.connect()
    except mcp.MCPError as e:
        raised = e
    check("validate: connect raises MCPError, not OSError",
          isinstance(raised, mcp.MCPError), type(raised).__name__)


def test_a_server_that_writes_junk_is_named() -> None:
    """A banner on stdout is the commonest way a stdio server is broken."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "junk.py"
        script.write_text(
            "import sys\n"
            "print('this is not JSON')\n"
            "sys.stdout.flush()\n"
            "sys.stdin.read()\n"
        )
        cfg = mcp.ServerConfig(name="junk", command=sys.executable,
                               args=[str(script)])
        client = mcp.Client(cfg)
        raised = None
        try:
            client.connect()
        except mcp.MCPError as e:
            raised = e
        check("junk: the failure names the non-JSON output",
              isinstance(raised, mcp.MCPError) and "non-JSON" in str(raised),
              str(raised)[:120])


# ---------------------------------------------------------------------------
# namespacing
# ---------------------------------------------------------------------------


def test_names_are_namespaced() -> None:
    """Two servers offering the same tool name must not collide."""
    a = mcp.MCPTool(server="alpha", name="search")
    b = mcp.MCPTool(server="beta", name="search")
    check("names: the server is in the name",
          a.exposed_name == "mcp_alpha_search", a.exposed_name)
    check("names: two servers do not collide",
          a.exposed_name != b.exposed_name, f"{a.exposed_name} / {b.exposed_name}")
    check("names: the harness's own tools are not shadowed",
          not a.exposed_name.startswith("search"), a.exposed_name)

    odd = mcp.MCPTool(server="my server", name="do/thing")
    check("names: unsafe characters are folded",
          odd.exposed_name == "mcp_my_server_do_thing", odd.exposed_name)

    schema = a.schema()
    check("names: the schema carries the server as a prefix",
          schema["description"].startswith("[alpha]"), schema["description"])
    check("names: the schema is a valid tool schema",
          schema["input_schema"].get("type") == "object", str(schema["input_schema"]))


# ---------------------------------------------------------------------------
# the tool layer
# ---------------------------------------------------------------------------


def test_the_tool_layer_forwards() -> None:
    """execute, needs_permission, build_preview and summarise_call all know."""
    reg = mcp.registry()
    reg.close()
    reg.connect_all([_server()])
    cfg = Config()
    try:
        check("layer: the schemas are advertised",
              sorted(s["name"] for s in reg.schemas())
              == ["mcp_test_add", "mcp_test_echo", "mcp_test_fail"],
              str([s["name"] for s in reg.schemas()]))

        result = tools.execute("mcp_test_echo", {"text": "via dispatch"}, cfg)
        check("layer: execute forwards to the server",
              result.output == "via dispatch" and not result.is_error,
              f"{result.output!r} / {result.is_error}")
        check("layer: the result is marked as MCP",
              result.meta.get("mcp") is True, str(result.meta))

        result = tools.execute("mcp_test_fail", {}, cfg)
        check("layer: a server-side error is an error result",
              result.is_error is True, str(result.is_error))

        result = tools.execute("mcp_test_nonexistent", {}, cfg)
        check("layer: an unknown MCP tool says so",
              result.is_error and "no MCP tool" in result.output, result.output)

        check("layer: MCP tools are gated",
              tools.needs_permission("mcp_test_echo") is True)
        check("layer: the harness's own reads are not",
              tools.needs_permission("read_file") is False)

        preview = tools.build_preview("mcp_test_echo", {"text": "hi"}, cfg)
        check("layer: the preview names the server and tool",
              "test" in preview.title and "echo" in preview.title, preview.title)
        check("layer: the preview shows the arguments",
              '"text"' in preview.body, preview.body)

        label = tools.summarise_call("mcp_test_echo", {"text": "hi"}, cfg)
        check("layer: the call is labelled for the transcript",
              "echo" in label and "hi" in label, label)
    finally:
        reg.close()


def test_plan_mode_denies_mcp() -> None:
    """An MCP tool's effect is the server's, so it cannot be called read-only."""
    check("plan: the mode denies the mcp family",
          "mcp" in MODES["plan"]["deny"], str(MODES["plan"]["deny"]))
    check("plan: other modes do not",
          "mcp" not in MODES["auto"]["deny"], str(MODES["auto"]["deny"]))

    from zimzilla.agent import Agent

    agent = Agent(Config(mode="plan"))
    reason = agent._denied("mcp_test_echo")
    check("plan: the agent refuses an MCP call",
          reason is not None and "MCP" in reason, str(reason))
    check("plan: and says why",
          reason is not None and "read-only" in reason, str(reason))

    # The schema filter must honour the prefix, not just exact names.
    check("plan: the schema filter withholds the family",
          Agent._schema_denied("mcp_test_echo", {"mcp"}) is True)
    check("plan: and leaves the harness's own tools alone",
          Agent._schema_denied("read_file", {"mcp"}) is False)


def test_one_broken_server_does_not_stop_the_others() -> None:
    """A bad entry in mcp.json must not cost the operator every working one."""
    reg = mcp.Registry()
    good = _server("good")
    bad = mcp.ServerConfig(name="bad", command="definitely-not-a-real-command")
    reg.connect_all([bad, good])
    try:
        check("resilience: the good server connected",
              "good" in reg.clients, str(list(reg.clients)))
        check("resilience: the bad one is recorded",
              "bad" in reg.errors, str(reg.errors))
        check("resilience: the good server's tools are available",
              len(reg.tools()) == 3, str(len(reg.tools())))
    finally:
        reg.close()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_config_round_trips() -> None:
    """The server list survives a write and a read, and is mode 600."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mcp.json"
        servers = [
            mcp.ServerConfig(name="fs", command="npx",
                             args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                             env={"TOKEN": "secret"}),
            mcp.ServerConfig(name="remote", url="https://example.com/mcp",
                             headers={"Authorization": "Bearer x"}),
        ]
        written = mcp.save_config(servers, path)
        check("config: the file is written", written.exists(), str(written))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check("config: it is mode 600", mode == 0o600, oct(mode))

        back = mcp.load_config(path)
        check("config: both servers come back", len(back) == 2, str(len(back)))
        fs = next(s for s in back if s.name == "fs")
        check("config: the command survives", fs.command == "npx", fs.command)
        check("config: the args survive",
              fs.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
              str(fs.args))
        check("config: the env survives", fs.env.get("TOKEN") == "secret", str(fs.env))
        check("config: the transport is derived", fs.transport == "stdio", fs.transport)

        remote = next(s for s in back if s.name == "remote")
        check("config: a url entry is http", remote.transport == "http", remote.transport)
        check("config: the headers survive",
              remote.headers.get("Authorization") == "Bearer x", str(remote.headers))

        # A missing file is an empty list, not an error.
        check("config: a missing file is empty",
              mcp.load_config(Path(tmp) / "nope.json") == [])

        # A malformed file is an error, and says so.
        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json")
        raised = None
        try:
            mcp.load_config(bad)
        except mcp.MCPError as e:
            raised = e
        check("config: malformed JSON is reported",
              isinstance(raised, mcp.MCPError), type(raised).__name__)


def test_validation() -> None:
    """A half-configured entry is refused before it is connected to."""
    check("validate: no name is refused",
          mcp.ServerConfig(name="", command="x").validate() is not None)
    check("validate: neither command nor url is refused",
          mcp.ServerConfig(name="a").validate() is not None)
    check("validate: both command and url is refused",
          mcp.ServerConfig(name="a", command="x", url="https://e.com").validate()
          is not None)
    check("validate: a non-http url is refused",
          mcp.ServerConfig(name="a", url="ftp://e.com").validate() is not None)
    check("validate: a good stdio entry passes",
          mcp.ServerConfig(name="a", command=sys.executable).validate() is None)
    check("validate: a good http entry passes",
          mcp.ServerConfig(name="a", url="https://e.com/mcp").validate() is None)


def main() -> int:
    test_handshake_and_listing()
    test_calling_tools()
    test_a_bad_server_is_reported_not_raised()
    test_a_server_that_writes_junk_is_named()
    test_names_are_namespaced()
    test_the_tool_layer_forwards()
    test_plan_mode_denies_mcp()
    test_one_broken_server_does_not_stop_the_others()
    test_config_round_trips()
    test_validation()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, ok, extra in RESULTS:
        if not ok:
            print(f"FAIL  {name}" + (f"  {extra}" if extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
