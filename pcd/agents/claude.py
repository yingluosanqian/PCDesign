"""Claude Code CLI driver — a mirror of CodexClient for Anthropic's `claude`.

Unlike codex's persistent app-server (one long-lived subprocess speaking
JSON-RPC), Claude Code is one-shot per invocation: every `run_turn`
spawns a fresh `claude -p --output-format stream-json` subprocess and
the subprocess exits when the assistant finishes its turn.

Sessions are identified by a UUID:
- `start_thread` pre-allocates a UUID; the first `run_turn` of that
  thread passes it via `--session-id <uuid>`, which makes Claude create
  a new session with that ID.
- `resume_thread` stores the UUID; the next `run_turn` passes
  `--resume <uuid>` to reattach to the existing session.

The user's project convention requires Claude to run with
`IS_SANDBOX=1` in the environment and `--dangerously-skip-permissions`
on the command line, both of which this client always applies so the
agent has full read/write access to the project directory.

`reasoning_effort` maps to Claude's own `--effort <level>` flag (which
accepts `low | medium | high | xhigh | max`). The `sandbox` parameter
from the CodexClient API is accepted for signature parity but ignored —
Claude's permission surface is controlled by the skip-permissions flag.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import uuid
from threading import Thread
from time import monotonic
from typing import Callable, IO, Optional

from pcd.agents.codex import TurnResult


_EOF = object()  # sentinel put on the queue when a reader thread hits EOF


def _format_tool_use(name: str, inp: dict) -> str:
    """Render a compact `[tool] name(k=v, ...)` marker for the progress stream.

    Each value is truncated to 80 chars so a Write with a 4 KB `content` arg
    doesn't flood the terminal. Non-string values are repr'd."""
    pieces: list[str] = []
    for k, v in inp.items():
        sv = v if isinstance(v, str) else repr(v)
        if len(sv) > 80:
            sv = sv[:77] + "..."
        sv = sv.replace("\n", "\\n")
        pieces.append(f"{k}={sv}")
        if len(pieces) >= 3:
            break
    args = ", ".join(pieces)
    return f"\n[tool] {name}({args})\n"


class ClaudeClient:
    def __init__(
        self,
        *,
        command: str = "claude",
        cwd: str,
        reasoning_effort: str = "medium",
        timeout_seconds: int = 1800,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = timeout_seconds
        self._resume_session_id: Optional[str] = None

    def __enter__(self) -> "ClaudeClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if shutil.which(self._command) is None:
            raise RuntimeError(f"claude CLI not found in PATH: {self._command}")

    def close(self) -> None:
        # Each run_turn owns its subprocess and tears it down before
        # returning, so there is nothing persistent to clean up here.
        self._resume_session_id = None

    def start_thread(
        self, *, cwd: str, model: Optional[str], sandbox: str
    ) -> str:
        self._resume_session_id = None
        return str(uuid.uuid4())

    def resume_thread(
        self,
        *,
        thread_id: str,
        cwd: str,
        model: Optional[str],
        sandbox: str,
    ) -> str:
        self._resume_session_id = thread_id
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
        args: list[str] = [
            self._command,
            "--dangerously-skip-permissions",
            "-p",
            "--output-format",
            "stream-json",
            "--input-format",
            "text",
            "--verbose",
        ]
        if self._resume_session_id:
            args += ["--resume", self._resume_session_id]
        else:
            args += ["--session-id", thread_id]
        if model:
            args += ["--model", model]
        if self._reasoning_effort:
            args += ["--effort", self._reasoning_effort]

        env = os.environ.copy()
        env["IS_SANDBOX"] = "1"

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
        )
        assert (
            proc.stdin is not None
            and proc.stdout is not None
            and proc.stderr is not None
        )

        # Ship the prompt on stdin so we don't run into argv length or
        # shell-quoting issues for multi-kilobyte role prompts.
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass

        stdout_q: "queue.Queue[object]" = queue.Queue()
        stderr_q: "queue.Queue[object]" = queue.Queue()
        stdout_thread = Thread(
            target=self._reader_loop,
            args=(proc.stdout, stdout_q),
            daemon=True,
            name="claude-stdout",
        )
        stderr_thread = Thread(
            target=self._reader_loop,
            args=(proc.stderr, stderr_q),
            daemon=True,
            name="claude-stderr",
        )
        stdout_thread.start()
        stderr_thread.start()

        final_text = ""
        streamed_text = ""
        observed_session_id: Optional[str] = None
        stderr_buf: list[str] = []
        stdout_eof = False
        deadline = monotonic() + self._timeout_seconds

        try:
            while not stdout_eof:
                if monotonic() >= deadline:
                    raise RuntimeError(
                        f"claude turn timed out after {self._timeout_seconds}s"
                    )
                try:
                    item = stdout_q.get(timeout=0.25)
                except queue.Empty:
                    self._drain(stderr_q, stderr_buf)
                    if proc.poll() is not None:
                        try:
                            item = stdout_q.get_nowait()
                        except queue.Empty:
                            break
                    else:
                        continue
                if item is _EOF:
                    stdout_eof = True
                    break
                line = str(item).strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "system" and msg.get("subtype") == "init":
                    sid = msg.get("session_id")
                    if isinstance(sid, str):
                        observed_session_id = sid
                    continue
                if mtype == "assistant":
                    m = msg.get("message") or {}
                    content = m.get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type")
                            if btype == "text":
                                t = block.get("text")
                                if isinstance(t, str) and t:
                                    streamed_text += t
                                    if on_progress is not None:
                                        on_progress(t)
                            elif btype == "tool_use" and on_progress is not None:
                                on_progress(
                                    _format_tool_use(
                                        block.get("name", "?"),
                                        block.get("input") or {},
                                    )
                                )
                    continue
                if mtype == "user":
                    m = msg.get("message") or {}
                    content = m.get("content") or []
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") != "tool_result":
                                continue
                            if on_progress is None:
                                continue
                            is_err = bool(block.get("is_error"))
                            on_progress(
                                "\n[tool ✗]\n" if is_err else "\n[tool ✓]\n"
                            )
                    continue
                if mtype == "result":
                    if msg.get("is_error"):
                        err = (
                            msg.get("result")
                            or msg.get("error")
                            or "claude turn failed"
                        )
                        raise RuntimeError(f"claude turn error: {err}")
                    res = msg.get("result")
                    if isinstance(res, str):
                        final_text = res
                    sid = msg.get("session_id")
                    if isinstance(sid, str):
                        observed_session_id = sid
                    continue
        finally:
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
            for t in (stdout_thread, stderr_thread):
                t.join(timeout=2)
            self._drain(stderr_q, stderr_buf)
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

        if proc.returncode not in (0, None):
            stderr = "".join(stderr_buf).strip()
            raise RuntimeError(
                f"claude exited with code {proc.returncode}: "
                f"{stderr or '(no stderr captured)'}"
            )

        resolved_id = observed_session_id or thread_id
        # Subsequent turns on this client should resume the same session
        # rather than create a new one.
        self._resume_session_id = resolved_id
        return TurnResult(
            final_text=final_text or streamed_text,
            thread_id=resolved_id,
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

    @staticmethod
    def _drain(q: "queue.Queue[object]", buf: list[str]) -> None:
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                return
            if item is _EOF:
                return
            buf.append(str(item))
