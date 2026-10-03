"""Prove the event loop keeps ticking while a slow bash tool runs."""
import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, "/home/zim/Projects/ZimZilla")
from zimzilla.agent import Agent
from zimzilla.config import Config

class Blk:
    def __init__(self, **k): self.__dict__.update(k)
class Msg:
    def __init__(self, c): self.content = c; self.usage = None

async def main():
    cfg = Config.from_env(workdir=Path("/tmp"), api_key="x",
                          base_url="http://localhost:4001", mode="auto")
    agent = Agent(cfg)
    state = {"n": 0}
    async def fake_stream():
        state["n"] += 1
        if state["n"] == 1:
            yield (None, Msg([Blk(type="tool_use", id="t1", name="bash",
                                  input={"command": "sleep 3; echo DONE"})]))
            return
        yield (None, Msg([Blk(type="text", text="ok")]))
    agent._stream_once = fake_stream

    # Heartbeat: how many times can we tick during the 3s command?
    ticks = 0
    stop = False
    async def beat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.05)
            ticks += 1

    hb = asyncio.create_task(beat())
    t0 = time.time()
    events = [ev async for ev in agent.run_turn("run it")]
    elapsed = time.time() - t0
    stop = True
    await hb

    out = [e for e in events if e["type"] == "tool_result"][0]["output"]
    print(f"command took {elapsed:.2f}s, heartbeat ticks during it: {ticks}")
    print(f"tool output: {out.strip()!r}")
    # A frozen loop would tick ~0 times; a healthy one ticks ~60.
    ok = ticks > 30 and "DONE" in out
    print("VERDICT:", "PASS — loop stayed responsive" if ok else "FAIL — loop was blocked")
    return 0 if ok else 1

raise SystemExit(asyncio.run(main()))

# Run: ./.venv/bin/python tests/check_no_freeze.py
# A blocking tool call would report ~0 ticks here; a threaded one reports ~60.
