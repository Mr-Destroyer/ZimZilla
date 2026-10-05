"""`/team` — fan a task out across parallel agents.

The main brain reads the task, decides how many workers it needs and what each
one owns, runs them concurrently, and synthesises their results. The shape is a
star: workers report to the main brain and never to each other, so their briefs
are the only coordination between them.

This module holds the orchestration and nothing else — no Textual, no widgets,
no colours. It reports what happened as plain event dicts through an `on_event`
sink and lets the UI decide how to paint them. That keeps the whole thing
testable without a terminal, which is how tests/test_phase4.py exercises it.

Three things here are load-bearing and easy to get wrong:

*   **Each worker gets its own Config.** ``Agent.__init__`` does
    ``cfg._scope = self.scope``, and tools find the scope guard by reading
    ``cfg._scope`` (tools._scope_of). Sharing one Config between workers means
    the last one constructed silently owns the guard for all of them. Workers
    therefore run on ``dataclasses.replace(cfg)`` copies, each of which re-loads
    its own Scope.

*   **The mode is inherited, never changed.** ``/team`` is not a mode. A worker
    built from a ``danger`` session runs in ``danger``; the only Config the
    orchestrator overrides is the planner's, and only to force it read-only.

*   **Ownership is the real write control, not the lock.** The lock serialises
    mutating *tool calls*; it cannot see a file written by a bash command, which
    is one opaque call to it. See `_waves`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncContextManager, Awaitable, Callable

from .agent import Agent
from .config import Config
from . import tools as tools_mod

#: Hard ceiling on the roster, whatever the planner asks for.
TEAM_MAX_AGENTS = 5

#: How many workers may be in flight at once. Bounds the concurrent load on the
#: proxy without serialising the run — a five-worker roster still overlaps.
TEAM_CONCURRENCY = 4

#: Cap on a single worker's brief, so one over-eager planner entry cannot blow
#: out the prompt.
MAX_BRIEF_CHARS = 2000


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

PLANNER_PROMPT = """\
You are the orchestrator for a team of coding agents. Analyse the task below and
decide whether it should be split across parallel workers.

TASK
----
{task}

You have READ-ONLY tools. Use them to size the work — enough to know which files
and areas are involved — but do not attempt to do the task itself.

Rules:
- Split the task only if it has genuinely independent parts. If it does not, or
  if it is small, return an empty roster — a single agent will handle it.
- Return AT MOST {max_agents} workers. Fewer is better; do not pad the roster.
- Each worker gets a short lowercase `name` (e.g. "parser-tests") and a `brief`
  that is a complete, self-contained instruction — the worker sees the brief and
  nothing else, so it must say what to do and where.
- Each worker gets an `owns` list: the paths or globs it may WRITE. These must
  not overlap between workers, because workers run at the same time and two of
  them writing one file loses one set of changes. If two parts of the task must
  touch the same file, they belong in one worker.
- Workers cannot talk to each other. Each brief must stand alone.

Reply with ONE JSON object and nothing else:

{{"summary": "one line on how you read the task",
  "workers": [{{"name": "short-name",
               "brief": "what this worker should do, in full",
               "owns": ["path/or/glob.py"]}}]}}

For a task that needs no team: {{"summary": "...", "workers": []}}
"""


@dataclass
class WorkerSpec:
    """One worker: what it is called, what it does, what it may write."""

    name: str
    brief: str
    owns: list[str] = field(default_factory=list)


@dataclass
class Roster:
    """The planner's decision."""

    summary: str
    workers: list[WorkerSpec] = field(default_factory=list)


@dataclass
class WorkerResult:
    """What a worker produced, for the synthesis."""

    spec: WorkerSpec
    digest: str = ""
    ok: bool = True
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    touched: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Roster parsing
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _slug(name: str) -> str:
    """A short, safe worker name. Never empty."""
    s = _SLUG_RE.sub("-", str(name or "").strip().lower()).strip("-")
    return s[:24] or "worker"


def _first_json_object(text: str) -> str | None:
    """The first balanced ``{...}`` span, string-aware.

    The model often wraps its JSON in prose ("Here is the roster: {...} Hope
    that helps"), so brace-counting beats a greedy regex — it stops at the
    object's real end instead of swallowing trailing text. Braces inside string
    literals are skipped, or a brief containing ``{`` would unbalance the count.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_roster(text: str, max_agents: int = TEAM_MAX_AGENTS) -> Roster | None:
    """Parse the planner's reply into a Roster, or None if it cannot be read.

    None is a signal, not an error: the caller falls back to running the task
    as a single turn. A planner that rambles should cost the operator a team,
    never the task itself.
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = []
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1))
    obj = _first_json_object(text)
    if obj:
        candidates.append(obj)

    data = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            data = parsed
            break
    if data is None:
        return None

    summary = str(data.get("summary", "") or "").strip()

    raw = data.get("workers")
    if not isinstance(raw, list):
        # A roster with no `workers` key is still a valid "no team" answer.
        return Roster(summary=summary, workers=[])

    workers: list[WorkerSpec] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        brief = str(entry.get("brief", "") or "").strip()
        if not brief:
            continue  # a worker with no brief cannot be run
        name = _slug(entry.get("name", ""))
        # De-duplicate: two workers sharing a name would also share a colour
        # and be indistinguishable in the interleaved transcript.
        base = name
        n = 2
        while name in seen:
            name = f"{base}-{n}"
            n += 1
        seen.add(name)

        owns_raw = entry.get("owns")
        owns = []
        if isinstance(owns_raw, list):
            owns = [str(p).strip() for p in owns_raw if str(p).strip()]

        workers.append(WorkerSpec(name=name, brief=brief[:MAX_BRIEF_CHARS], owns=owns))
        if len(workers) >= max_agents:
            break

    return Roster(summary=summary, workers=workers)


# ---------------------------------------------------------------------------
# Ownership waves
# ---------------------------------------------------------------------------

def _owns_overlap(a: list[str], b: list[str]) -> bool:
    """Do two ownership lists claim the same path?

    Literal equality, or a glob on either side matching the other's literal.
    This cannot see a path neither side declared — see the module docstring.
    """
    for x in a:
        for y in b:
            if x == y:
                return True
            if fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
                return True
    return False


def _waves(workers: list[WorkerSpec]) -> list[list[WorkerSpec]]:
    """Group workers so that no two in one wave claim the same path.

    The planner is told to give non-overlapping `owns` lists, but a model
    instruction is not a guarantee. Rather than trust it, workers whose claims
    collide are placed in a later wave and run after the first finishes. This
    is the actual protection for writes the lock cannot see — a file written by
    a bash command, which is a single opaque call from the lock's point of view.

    Order is preserved: a worker only ever moves later, never earlier.
    """
    waves: list[list[WorkerSpec]] = []
    for spec in workers:
        for wave in waves:
            if not any(_owns_overlap(spec.owns, w.owns) for w in wave):
                wave.append(spec)
                break
        else:
            waves.append([spec])
    return waves


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _write_lock_hook(lock: asyncio.Lock) -> Callable[[str, dict], AsyncContextManager[None]]:
    """A tool_hook that serialises mutating tool calls across all workers.

    Only the tools that need permission mutate anything (bash, write_file,
    edit_file — tools.GATED_TOOLS); anything else returns a no-op guard so a
    read never queues behind a writer.

    What this does NOT cover: a file written by a bash command. bash is one
    call to this lock, and the shell inside it is invisible. Ownership waves
    are the control for that; this only stops two workers being *inside* a
    mutating call at the same instant.
    """

    @asynccontextmanager
    async def _guard(name: str, args: dict):
        if not tools_mod.needs_permission(name):
            yield
            return
        async with lock:
            yield

    return _guard


def _touched_paths(name: str, args: dict) -> list[str]:
    if name in {"write_file", "edit_file"}:
        p = str(args.get("path", "") or "").strip()
        return [p] if p else []
    return []


async def _run_worker(
    spec: WorkerSpec,
    index: int,
    cfg: Config,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    agent_factory: Callable[..., Agent],
    tool_hook,
) -> WorkerResult:
    """Run one worker to completion. Never raises except on cancellation."""
    result = WorkerResult(spec=spec)

    # Its own Config copy: its own Scope, its own cfg._scope. The mode, model,
    # workdir and scope paths are inherited unchanged — /team is not a mode.
    worker_cfg = dataclasses.replace(cfg)
    worker = agent_factory(worker_cfg, tool_hook=tool_hook)

    await on_event({"type": "team_start", "name": spec.name, "index": index,
                    "brief": spec.brief})

    buf: list[str] = []

    async def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            await on_event({"type": "team_text", "name": spec.name, "text": text})

    try:
        async for ev in worker.run_turn(spec.brief):
            etype = ev.get("type")
            if etype == "text_delta":
                buf.append(ev.get("text", ""))
            elif etype == "tool_call":
                # Flush before the call so the worker's reasoning lands above
                # the call it explains, not after it.
                await flush()
                await on_event({"type": "team_tool", "name": spec.name,
                                "tool": ev.get("name", ""), "args": ev.get("args") or {}})
            elif etype == "tool_result":
                result.touched.extend(
                    _touched_paths(ev.get("name", ""), ev.get("args") or {})
                )
                await on_event({
                    "type": "team_result", "name": spec.name,
                    "tool": ev.get("name", ""), "ok": not ev.get("is_error"),
                    "output": ev.get("output", ""),
                })
            elif etype == "blocked":
                await on_event({"type": "team_result", "name": spec.name,
                                "tool": ev.get("name", ""), "ok": False,
                                "output": ev.get("targets", "")})
            elif etype == "usage":
                result.input_tokens += ev.get("input", 0) or 0
                result.output_tokens += ev.get("output", 0) or 0
                result.cost += ev.get("cost", 0.0) or 0.0
            elif etype == "error":
                result.ok = False
                result.error = ev.get("message", "") or "error"
                await flush()
            # "thinking", "retry" and "turn_end" carry nothing a worker-level
            # transcript needs; the worker's own accounting is taken from the
            # usage events above.
        await flush()
    except asyncio.CancelledError:
        # Never swallow cancellation — it must unwind the whole team.
        raise
    except Exception as e:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(e).__name__}: {e}"
        await flush()

    result.digest = _digest(worker, result)
    return result


def _digest(worker: Agent, result: WorkerResult) -> str:
    """The worker's final text, for the synthesis prompt.

    Read back off the worker's own history rather than accumulated here: the
    history is what the worker actually said, and it is already in the right
    order. Falls back to a marker when the worker produced no text at all.
    """
    for msg in reversed(worker.messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "\n".join(p for p in parts if p).strip()
        if text:
            return text
    if not result.ok:
        return f"(worker failed: {result.error})"
    return "(worker produced no text)"


async def run_team(
    cfg: Config,
    task: str,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    agent_factory: Callable[..., Agent] = Agent,
    max_agents: int = TEAM_MAX_AGENTS,
    concurrency: int = TEAM_CONCURRENCY,
) -> list[WorkerResult]:
    """Plan, launch, and collect. Returns the worker results.

    Emits every step through ``on_event`` as it happens. The caller owns the
    synthesis — this returns the results it needs for it.

    ``agent_factory`` is the test seam: it is the only place Agents are built,
    so a test can hand back stubbed ones. Patching a class attribute would not
    work, because each worker has its own instance.
    """
    # ---- plan. Read-only, so planning cannot start doing the work itself when
    # the session is in zim or danger mode.
    plan_cfg = dataclasses.replace(cfg, mode="plan")
    planner = agent_factory(plan_cfg)
    plan_text = await _collect_text(
        planner, PLANNER_PROMPT.format(task=task, max_agents=max_agents)
    )

    roster = parse_roster(plan_text, max_agents=max_agents)

    await on_event({
        "type": "team_plan",
        "summary": roster.summary if roster else "",
        "workers": ([{"name": w.name, "brief": w.brief, "owns": w.owns}
                     for w in roster.workers] if roster else []),
        "parsed": roster is not None,
    })

    if roster is None or not roster.workers:
        return []

    # ---- launch, in waves, so no two concurrent workers claim one path.
    lock = asyncio.Lock()
    hook = _write_lock_hook(lock)
    sem = asyncio.Semaphore(max(1, concurrency))

    results: list[WorkerResult] = []
    total = len(roster.workers)
    index = 0

    async def _one(spec: WorkerSpec, i: int) -> WorkerResult:
        async with sem:
            return await _run_worker(
                spec, i, cfg,
                on_event=on_event, agent_factory=agent_factory, tool_hook=hook,
            )

    for wave in _waves(roster.workers):
        wave_results = await asyncio.gather(
            *(_one(spec, index + offset) for offset, spec in enumerate(wave))
        )
        for r in wave_results:
            results.append(r)
            await on_event({
                "type": "team_done", "name": r.spec.name, "ok": r.ok,
                "cost": r.cost, "error": r.error,
                "input": r.input_tokens, "output": r.output_tokens,
            })
        index += len(wave)

    await on_event({
        "type": "team_end", "workers": total,
        "ok": sum(1 for r in results if r.ok),
        "cost": sum(r.cost for r in results),
        "input": sum(r.input_tokens for r in results),
        "output": sum(r.output_tokens for r in results),
    })
    return results


async def _collect_text(agent: Agent, prompt: str) -> str:
    """Run one turn and return only its text, discarding the tool chatter."""
    parts: list[str] = []
    async for ev in agent.run_turn(prompt):
        if ev.get("type") == "text_delta":
            parts.append(ev.get("text", ""))
    return "".join(parts)


def synthesis_prompt(task: str, results: list[WorkerResult]) -> str:
    """The main brain's closing brief: fold the workers' work into one answer."""
    blocks = []
    for r in results:
        status = "OK" if r.ok else f"FAILED ({r.error})"
        owns = ", ".join(r.spec.owns) if r.spec.owns else "(no declared files)"
        blocks.append(
            f"### {r.spec.name} — {status}\n"
            f"brief: {r.spec.brief}\n"
            f"declared files: {owns}\n\n"
            f"{r.digest}"
        )
    body = "\n\n".join(blocks) if blocks else "(no worker produced anything)"

    return (
        f"A team of {len(results)} agents worked on this task in parallel:\n\n"
        f"TASK\n{task}\n\n"
        f"THEIR REPORTS\n{body}\n\n"
        "Write the single answer the operator asked for. Combine what the "
        "workers found into one coherent response — do not simply concatenate "
        "their reports, and do not list them by name unless a name is "
        "load-bearing. If workers disagree, say so and say which you believe. "
        "If a worker failed, note it only if it leaves a gap in the answer."
    )
