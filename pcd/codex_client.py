"""Minimal JSON-RPC client for `codex app-server --listen stdio://`.

Design notes
------------
We run codex as a subprocess and speak newline-delimited JSON-RPC over its
stdio. Two background reader threads drain stdout/stderr line-by-line into
queues, and the main thread pulls from the stdout queue with a timeout.

Why threads + queues and not select(): codex closes stderr (or simply stops
writing to it) after its startup warnings, and when that happens select()
reports stderr as permanently ready (EOF is "ready"), while readline()
returns immediately with "". That spins the CPU. Separately, TextIOWrapper
has an internal buffer that select() doesn't see, so select can falsely
report "not ready" while a full JSON line is already buffered in Python.
Per-stream reader threads using blocking readline() avoid both pitfalls.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
from dataclasses import dataclass
from threading import Thread
from time import monotonic
from typing import Callable, IO, Optional


_EOF = object()  # sentinel put on the queue when a reader thread hits EOF


@dataclass
class TurnResult:
    final_text: str
    thread_id: str


class CodexClient:
    def __init__(
        self,
        *,
        command: str = "codex",
        cwd: str,
        reasoning_effort: str = "medium",
        timeout_seconds: int = 1800,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = timeout_seconds
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: "queue.Queue[object]" = queue.Queue()
        self._stderr_queue: "queue.Queue[object]" = queue.Queue()
        self._stdout_thread: Optional[Thread] = None
        self._stderr_thread: Optional[Thread] = None
        self._stdout_eof = False
        self._stderr_eof = False
        self._next_id = 1
        self._pending_notifications: list[dict] = []
        self._stderr_buffer: list[str] = []

    def __enter__(self) -> "CodexClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if shutil.which(self._command) is None:
            raise RuntimeError(f"codex CLI not found in PATH: {self._command}")
        self._process = subprocess.Popen(
            [
                self._command,
                "app-server",
                "-c",
                f'model_reasoning_effort="{self._reasoning_effort}"',
                "--listen",
                "stdio://",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self._cwd,
            env=os.environ.copy(),
        )
        assert self._process.stdout is not None and self._process.stderr is not None
        self._stdout_thread = Thread(
            target=self._reader_loop,
            args=(self._process.stdout, self._stdout_queue),
            daemon=True,
            name="codex-stdout",
        )
        self._stderr_thread = Thread(
            target=self._reader_loop,
            args=(self._process.stderr, self._stderr_queue),
            daemon=True,
            name="codex-stderr",
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "pcdesign", "version": "0.1"},
                "capabilities": {},
            },
        )

    def close(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # Reader threads will hit EOF once streams close and exit naturally.
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None:
                t.join(timeout=2)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def start_thread(
        self, *, cwd: str, model: Optional[str], sandbox: str
    ) -> str:
        result = self._request(
            "thread/start",
            {
                "cwd": cwd,
                "model": model,
                "sandbox": sandbox,
                "approvalPolicy": "never",
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError(f"thread/start did not return a thread: {result!r}")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise RuntimeError(f"thread/start missing thread id: {result!r}")
        return thread_id

    def resume_thread(
        self,
        *,
        thread_id: str,
        cwd: str,
        model: Optional[str],
        sandbox: str,
    ) -> str:
        result = self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "model": model,
                "sandbox": sandbox,
                "approvalPolicy": "never",
            },
        )
        thread = result.get("thread")
        if isinstance(thread, dict):
            maybe_id = thread.get("id")
            if isinstance(maybe_id, str):
                return maybe_id
        return thread_id

    def run_turn(
        self,
        *,
        thread_id: str,
        cwd: str,
        model: Optional[str],
        prompt: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> TurnResult:
        self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "model": model,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        final_text = ""
        streamed_text = ""
        deadline = monotonic() + self._timeout_seconds
        while True:
            if monotonic() >= deadline:
                raise RuntimeError(
                    f"codex turn timed out after {self._timeout_seconds}s"
                )
            msg = self._read_message(deadline=deadline)
            if msg is None:
                raise RuntimeError(
                    self._failure_detail(
                        "codex app-server exited before turn/completed"
                    )
                )
            if "method" not in msg:
                continue
            method = str(msg["method"])
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                continue
            if method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str) and delta:
                    streamed_text += delta
                    if on_progress is not None:
                        on_progress(delta)
                continue
            if method == "item/completed":
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    if item.get("phase") == "final_answer":
                        t = item.get("text")
                        if isinstance(t, str):
                            final_text = t
                continue
            if method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict) and turn.get("error"):
                    err = turn["error"]
                    msg_text = (
                        err.get("message") if isinstance(err, dict) else None
                    ) or "turn failed"
                    raise RuntimeError(f"codex turn error: {msg_text}")
                break
            if method == "error":
                raise RuntimeError(
                    f"codex error: {params.get('message') or params}"
                )
        return TurnResult(
            final_text=final_text or streamed_text,
            thread_id=thread_id,
        )

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _reader_loop(stream: IO[str], q: "queue.Queue[object]") -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                q.put(line)
        finally:
            q.put(_EOF)

    def _request(self, method: str, params: dict) -> dict:
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        payload = {"id": request_id, "method": method, "params": params}
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        deadline = monotonic() + self._timeout_seconds
        while True:
            # Always read fresh here: reading from the pending-notifications
            # buffer would create a self-feeding loop (pop, re-enqueue, pop…).
            msg = self._read_fresh_message(deadline=deadline)
            if msg is None:
                raise RuntimeError(
                    self._failure_detail(
                        f"timed out waiting for {method} response"
                    )
                )
            if msg.get("id") == request_id:
                if isinstance(msg.get("error"), dict):
                    err = msg["error"]
                    raise RuntimeError(
                        f"{method} failed: {err.get('message') or err}"
                    )
                result = msg.get("result")
                return result if isinstance(result, dict) else {}
            if "method" in msg:
                self._pending_notifications.append(msg)

    def _read_message(self, *, deadline: float) -> Optional[dict]:
        if self._pending_notifications:
            return self._pending_notifications.pop(0)
        return self._read_fresh_message(deadline=deadline)

    def _read_fresh_message(self, *, deadline: float) -> Optional[dict]:
        self._drain_stderr()
        while True:
            remaining = max(0.0, deadline - monotonic())
            if remaining == 0.0:
                return None
            try:
                item = self._stdout_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                self._drain_stderr()
                if self._stdout_eof:
                    return None
                proc = self._process
                if proc is not None and proc.poll() is not None:
                    # Process exited and we've already drained what we could.
                    try:
                        item = self._stdout_queue.get_nowait()
                    except queue.Empty:
                        return None
                else:
                    continue
            if item is _EOF:
                self._stdout_eof = True
                return None
            line = str(item).strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    def _drain_stderr(self) -> None:
        while True:
            try:
                item = self._stderr_queue.get_nowait()
            except queue.Empty:
                return
            if item is _EOF:
                self._stderr_eof = True
                return
            self._stderr_buffer.append(str(item))

    def _failure_detail(self, default: str) -> str:
        self._drain_stderr()
        stderr = "".join(self._stderr_buffer).strip()
        return stderr or default
