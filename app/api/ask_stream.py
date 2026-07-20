"""Streaming ask (ROADMAP Phase 2): POST /api/ask/stream — NDJSON.

Same body as ``POST /api/ask`` (it *is* ``tasks.AskBody``: prompt / analyst_id
/ hand / model / timeout_s — one request contract, imported not copied), and
literally the same preprocessing (``tasks.prepare_ask``, shared not mirrored):
an ``analyst_id`` gets roster validation (unknown ⇒ 404 before the stream
starts), the persona-sandwich prompt wrap with the standing-memory block, and
the interactive idle-hand preference (a busy unpinned hand reroutes to the
first idle+available hand in its fallback chain). The response is a
newline-delimited JSON stream bridging the executor's existing ``on_chunk``
plumbing straight to the client:

    {"type": "stdout"|"stderr"|"status", "text": "..."}     ... per chunk
    {"type": "status", "text": "…N chunks dropped"}         ... only if the
                                                                consumer lagged
    {"type": "done", "task": {id, status, hand, exit_code, error, output}}

The bridge: ``executor.submit(..., on_chunk=...)`` runs as an independent
asyncio task; ``on_chunk`` (called synchronously on this loop by the hand's
stdout/stderr pumps) enqueues frames onto a **bounded** queue
(``QUEUE_MAX_CHUNKS``) that the response generator drains. On overflow the
oldest chunk is dropped (ring-buffer semantics: a live tail wants the newest
output; the complete record is in the ``tasks`` row anyway) and a counter
feeds one ``status`` frame before ``done``. The final ``done`` frame carries
the task row (output truncated to 8KB — ``GET /api/tasks/{id}`` has the
DB-capped record).

**Disconnect semantics — deliberately different from /api/ask.** The
synchronous ``/api/ask`` ties the request to the task: the caller waits.
Here the submit task is *not* tied to the response: a client that hangs up
mid-stream does NOT cancel the run — the generator's exit flips the bridge
to closed (``on_chunk`` short-circuits, the buffer is drained and stays
empty) while the executor task keeps going, finishes, and lands in the
``tasks`` table as usual (fire-and-forget, like the rest of the institute's
loops). A disconnected client recovers the result later via
``GET /api/tasks?source=api`` (the task id is only surfaced in the ``done``
frame, by design: ``executor.submit`` allocates it internally).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..router import executor
from .tasks import AskBody, prepare_ask

router = APIRouter(prefix="/api", tags=["tasks"])

QUEUE_MAX_CHUNKS = 1000       # bound on buffered live chunks (frames, not bytes)
DONE_OUTPUT_CAP_BYTES = 8192
_CHUNK_TYPES = ("stdout", "stderr", "status")
_DONE = object()  # queue sentinel: the submit task is finished


class _ChunkBridge:
    """on_chunk → bounded queue, with drop-oldest overflow and a closed state.

    Single-producer, single-consumer, one event loop — ``offer`` is only ever
    called synchronously (hand pumps / the submit done-callback), so the
    make-room-then-put below cannot interleave with a concurrent put.

    Ordering invariant that keeps the sentinel safe: every chunk ``offer``
    happens while the hand is executing, and the ``_DONE`` sentinel is offered
    by the submit task's done-callback strictly after that — so dropping the
    oldest item can never drop the sentinel, and the done signal survives a
    full queue.
    """

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def offer(self, item: Any) -> None:
        if self.closed:  # consumer is gone: stop buffering entirely
            return
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()  # drop the OLDEST, keep the newest
                    self.dropped += 1
                except asyncio.QueueEmpty:  # unreachable single-threaded; never spin
                    pass

    def close(self) -> None:
        """Consumer exited (client disconnect or normal end): short-circuit
        future offers and release everything already buffered."""
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()


def _task_payload(task: executor.Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "status": task.status,
        "hand": task.hand,
        "exit_code": task.exit_code,
        "error": task.error,
        "output": executor.truncate_output(task.output or "", DONE_OUTPUT_CAP_BYTES),
    }


@router.post("/ask/stream")
async def ask_stream(body: AskBody):
    """One-shot prompt with live NDJSON output; see module docstring for semantics."""
    hand, prompt = await prepare_ask(body)  # 404 on unknown analyst happens before the stream
    bridge = _ChunkBridge(QUEUE_MAX_CHUNKS)

    def on_chunk(chunk: dict) -> None:
        kind = chunk.get("type")
        bridge.offer({
            "type": kind if kind in _CHUNK_TYPES else "status",
            "text": str(chunk.get("text") or ""),
        })

    # Independent task, NOT awaited by the request handler: if the client
    # disconnects, the response generator dies but this task runs to
    # completion and persists its row (fire-and-forget semantics).
    submit_task = asyncio.ensure_future(
        executor.submit(
            hand, prompt, source="api",
            model=body.model, timeout_s=body.timeout_s, on_chunk=on_chunk,
        )
    )

    def _on_submit_done(t: asyncio.Task) -> None:
        # done-callbacks run via call_soon after completion, so every chunk
        # offer already happened — the sentinel is strictly last (see bridge).
        bridge.offer(_DONE)
        # a disconnected client never reaches .result(); retrieve a possible
        # exception here so the loop doesn't log "exception was never retrieved"
        if not t.cancelled():
            t.exception()

    submit_task.add_done_callback(_on_submit_done)

    async def gen():
        try:
            while True:
                item = await bridge.queue.get()  # client disconnect cancels HERE, not the submit task
                if item is _DONE:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
            if bridge.dropped:
                note = {
                    "type": "status",
                    "text": f"…{bridge.dropped} chunks dropped (slow consumer; "
                            "the full output is on the task record)",
                }
                yield json.dumps(note, ensure_ascii=False) + "\n"
            try:
                payload = _task_payload(submit_task.result())
            except BaseException as exc:  # noqa: BLE001 - report the failure as a frame, never a broken stream
                payload = {
                    "id": None, "status": "failed", "hand": None, "exit_code": None,
                    "error": f"{type(exc).__name__}: {exc}", "output": "",
                }
            yield json.dumps({"type": "done", "task": payload}, ensure_ascii=False) + "\n"
        finally:
            bridge.close()  # disconnect or normal end: stop buffering, free the queue

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
