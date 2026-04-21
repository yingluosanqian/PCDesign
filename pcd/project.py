"""Project directory layout, metadata persistence, and append-only logs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


DESIGN_FILENAME = "design.md"
META_DIR = ".pcd"
META_FILENAME = "meta.json"
INITIAL_PROMPT_FILENAME = "initial_prompt.txt"
JUDGMENTS_LOG = "judgments.jsonl"
REVISIONS_LOG = "revisions.jsonl"
HUMAN_CHECKS_LOG = "human_checks.jsonl"
ALTERNATIVES_LOG = "alternatives.jsonl"
ROUNDS_DIR = "rounds"


@dataclass
class ProjectMeta:
    p_thread_id: str
    proposer_model: Optional[str]
    critic_model: Optional[str]
    created_at: str
    iterations_done: int = 0
    judge_model: Optional[str] = None
    converged: bool = False
    convergence_note: str = ""
    # Per-role agent selection (codex | claude). Existing projects without
    # these fields in meta.json fall through to the default "codex".
    proposer_agent: str = "codex"
    critic_agent: str = "codex"
    judge_agent: str = "codex"
    # Reframer (alternative-generator) state. See pcd/roles/reframer.py.
    # reframe_tested: at least one Proposer revise has processed an
    #   alternatives package. Blocks convergence until True.
    # reframe_pending: alternatives have been logged but not yet consumed
    #   by a Proposer revise. Forces the next Proposer call regardless of
    #   the normal quality_ok/stable branching.
    # reframe_at_round: scheduled trigger — Reframer runs once at the end
    #   of this iteration if still untested. Default 2 (after Proposer's
    #   first real revision, when Proposer hasn't yet deeply committed).
    reframe_tested: bool = False
    reframe_pending: bool = False
    reframe_at_round: int = 2
    reframer_agent: str = "codex"
    reframer_model: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "ProjectMeta":
        data = json.loads(s)
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.design_path = self.root / DESIGN_FILENAME
        self.meta_dir = self.root / META_DIR
        self.meta_path = self.meta_dir / META_FILENAME
        self.initial_prompt_path = self.meta_dir / INITIAL_PROMPT_FILENAME
        self.judgments_log_path = self.meta_dir / JUDGMENTS_LOG
        self.revisions_log_path = self.meta_dir / REVISIONS_LOG
        self.human_checks_log_path = self.meta_dir / HUMAN_CHECKS_LOG
        self.alternatives_log_path = self.meta_dir / ALTERNATIVES_LOG
        self.rounds_dir = self.meta_dir / ROUNDS_DIR

    def exists(self) -> bool:
        return self.meta_path.exists()

    def create_layout(self, *, initial_prompt: str) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        self.meta_dir.mkdir()
        self.initial_prompt_path.write_text(initial_prompt, encoding="utf-8")

    def load_meta(self) -> ProjectMeta:
        return ProjectMeta.from_json(self.meta_path.read_text(encoding="utf-8"))

    def save_meta(self, meta: ProjectMeta) -> None:
        self.meta_path.write_text(meta.to_json(), encoding="utf-8")

    def append_judgment(
        self,
        *,
        iteration: int,
        judgment: dict,
        critics_output: dict,
        manually_edited: bool = False,
        degraded: bool = False,
        degraded_reasons: Optional[list[str]] = None,
    ) -> None:
        record = {
            "iteration": iteration,
            "timestamp": self.now_iso(),
            "critics_output": critics_output,
            "judgment": judgment,
            "manually_edited": manually_edited,
            "degraded": degraded,
            "degraded_reasons": list(degraded_reasons or []),
        }
        self._append_jsonl(self.judgments_log_path, record)

    def dump_round(
        self,
        *,
        iteration: int,
        critics_output: dict,
        judgment: dict,
        manually_edited: bool = False,
        degraded: bool = False,
        degraded_reasons: Optional[list[str]] = None,
    ) -> Path:
        """Explode one iteration into human-friendly files on disk.

        Sibling to `append_judgment` (which owns the append-only jsonl log).
        Lives under `.pcd/rounds/iter_NNN/` and is safe to re-write: calling
        this twice for the same iteration just overwrites the files.
        """
        # Local import to avoid a project ← issues import cycle at module load.
        from pcd.issues import format_round_summary

        round_dir = self.rounds_dir / f"iter_{iteration:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        for role, issues in critics_output.items():
            (round_dir / f"critic_{role}.json").write_text(
                json.dumps(issues, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        (round_dir / "judgment.json").write_text(
            json.dumps(judgment, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (round_dir / "summary.md").write_text(
            format_round_summary(
                iteration=iteration,
                critics_output=critics_output,
                judgment=judgment,
                manually_edited=manually_edited,
                degraded=degraded,
                degraded_reasons=list(degraded_reasons or []),
            ),
            encoding="utf-8",
        )
        return round_dir

    def append_alternatives(
        self,
        *,
        iteration: int,
        alternatives: list[dict],
        trigger: str,
    ) -> Path:
        """Log a Reframer-produced alternatives package.

        `trigger` names the reason we ran Reframer this round: "scheduled"
        (iteration == reframe_at_round) or "converge_gate" (we'd have
        declared converged but reframe_tested was still False). Kept on
        the record so you can eyeball "why this alt package exists" later.

        Also writes the rounds/iter_NNN/alternatives.md sibling so
        humans can skim the package without spelunking the jsonl.
        Returns the markdown path.
        """
        # Local import avoids project ← issues import cycle.
        from pcd.issues import format_alternatives_summary

        record = {
            "iteration": iteration,
            "timestamp": self.now_iso(),
            "trigger": trigger,
            "alternatives": alternatives,
        }
        self._append_jsonl(self.alternatives_log_path, record)

        round_dir = self.rounds_dir / f"iter_{iteration:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        md_path = round_dir / "alternatives.md"
        md_body = format_alternatives_summary(alternatives)
        md_path.write_text(
            f"<!-- trigger: {trigger} -->\n\n{md_body}", encoding="utf-8"
        )
        return md_path

    def last_pending_alternatives(self) -> Optional[dict]:
        """The most recent alternatives record — interpreted as 'pending'
        by the orchestrator based on meta.reframe_pending, not by the file
        itself. Returns the record dict or None if nothing has been logged."""
        last: Optional[dict] = None
        if not self.alternatives_log_path.exists():
            return None
        with self.alternatives_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
        return last

    def append_human_check(self, record: dict) -> None:
        self._append_jsonl(self.human_checks_log_path, record)

    def next_human_check_id(self) -> int:
        if not self.human_checks_log_path.exists():
            return 1
        count = 0
        with self.human_checks_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count + 1

    def must_fix_history(self, *, skip_degraded: bool = True) -> list[int]:
        """must_fix counts across logged iterations, in order.

        Degraded rounds (critic failure or Proposer no-op) are skipped by
        default — they carry no trustworthy quality signal, and the
        no-progress sliding window should only look at real rounds.
        """
        out: list[int] = []
        for rec in self.iter_judgments():
            if skip_degraded and rec.get("degraded"):
                continue
            j = rec.get("judgment") or {}
            s = j.get("summary") or {}
            mf = s.get("must_fix_count")
            if isinstance(mf, int):
                out.append(mf)
        return out

    def append_revision(self, *, iteration: int, note: str = "") -> None:
        record = {
            "iteration": iteration,
            "timestamp": self.now_iso(),
            "note": note,
        }
        self._append_jsonl(self.revisions_log_path, record)

    def iter_judgments(self) -> Iterator[dict]:
        if not self.judgments_log_path.exists():
            return
        with self.judgments_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def last_judgment(self) -> Optional[dict]:
        last: Optional[dict] = None
        for record in self.iter_judgments():
            last = record
        return last

    def last_non_degraded_judgment(self) -> Optional[dict]:
        """Most recent round whose judgment is trusted as stability evidence.

        Degraded rounds (critic failure, Proposer no-op, or — once
        implemented — claude-side contamination rollback) are skipped:
        they either ran on incomplete input or didn't move the document.
        """
        last: Optional[dict] = None
        for record in self.iter_judgments():
            if record.get("degraded"):
                continue
            last = record
        return last

    def design_hash(self) -> Optional[str]:
        """SHA-256 of design.md as a hex string, or None if the file is missing."""
        if not self.design_path.exists():
            return None
        return hashlib.sha256(self.design_path.read_bytes()).hexdigest()

    @staticmethod
    def _append_jsonl(path: Path, record: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
