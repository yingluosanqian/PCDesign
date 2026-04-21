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
GUIDANCE_LOG = "guidance.jsonl"
ROUNDS_DIR = "rounds"

# Round lifecycle — one of these strings is written into
# rounds/iter_NNN/STATUS at each milestone. On restart, the orchestrator
# reads STATUS to skip already-completed expensive steps.
STATUS_STARTED = "started"            # pre_critics snapshot written
STATUS_CRITICS_DONE = "critics_done"  # all section critics + (maybe) reframer/exploration done
STATUS_JUDGE_DONE = "judge_done"      # judgment.json written
STATUS_JUDGED = "judged"              # manual-judge (if any) applied; judgment frozen
STATUS_PROPOSER_DONE = "proposer_done"  # design.md rewrite complete
STATUS_COMMITTED = "committed"        # jsonl appended, summary.md written
STATUS_SEQUENCE = (
    STATUS_STARTED,
    STATUS_CRITICS_DONE,
    STATUS_JUDGE_DONE,
    STATUS_JUDGED,
    STATUS_PROPOSER_DONE,
    STATUS_COMMITTED,
)


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
    # reframe_tested: Reframer has fired at least once in this run, its
    #   issues flowed through Judge, and the Proposer's revise consumed
    #   the resulting package. Blocks convergence until True.
    # reframe_at_round: scheduled trigger — Reframer fires once at or
    #   after this iteration. Default 2 (early, before Proposer has
    #   deeply committed to the current shape's refinement direction).
    # reframe_attempts: number of times Reframer was invoked (success
    #   or failure). Capped at 2; after that the Reframer stops trying
    #   and the gate moves to "degraded" status. Prevents budget burn
    #   when Reframer repeatedly fails.
    # reframe_degraded_confirmed: human has acknowledged via
    #   `pcd check --result confirm_reframe_degraded` that this run
    #   will not receive structural-alternative coverage. Unlocks the
    #   convergence gate under gate (ii). Only settable when
    #   reframe_attempts >= 2 AND reframe_tested is still False.
    reframe_tested: bool = False
    reframe_at_round: int = 2
    reframe_attempts: int = 0
    reframe_degraded_confirmed: bool = False
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
        self.guidance_log_path = self.meta_dir / GUIDANCE_LOG
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
        hard_constraints: list[str] | None = None,
        trigger: str = "scheduled",
    ) -> Path:
        """Log a Reframer-produced alternatives package.

        `trigger` names the reason we ran Reframer this round (currently
        always "scheduled"). Also writes the rounds/iter_NNN/alternatives.md
        sibling so humans can skim the package without spelunking jsonl.
        Returns the markdown path.
        """
        # Local import avoids project ← issues import cycle.
        from pcd.issues import format_alternatives_summary

        hc = list(hard_constraints or [])
        record = {
            "iteration": iteration,
            "timestamp": self.now_iso(),
            "trigger": trigger,
            "hard_constraints": hc,
            "alternatives": alternatives,
        }
        self._append_jsonl(self.alternatives_log_path, record)

        round_dir = self.rounds_dir / f"iter_{iteration:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        md_path = round_dir / "alternatives.md"
        md_body = format_alternatives_summary(alternatives, hc)
        md_path.write_text(
            f"<!-- trigger: {trigger} -->\n\n{md_body}", encoding="utf-8"
        )
        return md_path

    def append_human_check(self, record: dict) -> None:
        self._append_jsonl(self.human_checks_log_path, record)

    # ------------------------------------------------------------ guidance

    def append_guidance(self, note: str) -> int:
        """Record a user's one-liner guidance. Returns the new id.

        The next Proposer revise will see all guidance records with
        consumed_at_iteration == None, inject them into the revise
        prompt, and mark them consumed after the revise succeeds.
        """
        entries = list(self._iter_guidance())
        gid = len(entries) + 1
        record = {
            "id": gid,
            "note": note,
            "created_at": self.now_iso(),
            "consumed_at_iteration": None,
        }
        self._append_jsonl(self.guidance_log_path, record)
        return gid

    def _iter_guidance(self) -> Iterator[dict]:
        if not self.guidance_log_path.exists():
            return
        with self.guidance_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def pending_guidance(self) -> list[dict]:
        """Guidance records the Proposer hasn't consumed yet."""
        return [
            g
            for g in self._iter_guidance()
            if g.get("consumed_at_iteration") is None
        ]

    def mark_guidance_consumed(self, iteration: int, ids: list[int]) -> None:
        """Mark the given guidance ids as consumed in round `iteration`.

        Rewrites the whole guidance.jsonl in-place. OK because the
        guidance log stays small (one record per user hint; there
        won't be thousands).
        """
        if not ids:
            return
        id_set = set(ids)
        entries = list(self._iter_guidance())
        for e in entries:
            if (
                e.get("id") in id_set
                and e.get("consumed_at_iteration") is None
            ):
                e["consumed_at_iteration"] = iteration
        self.guidance_log_path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
            + ("\n" if entries else ""),
            encoding="utf-8",
        )

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

    # ------------------------------------------------------------ round STATUS

    def _round_dir(self, iteration: int) -> Path:
        path = self.rounds_dir / f"iter_{iteration:03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_round_status(self, iteration: int, status: str) -> None:
        """Write (overwrite) the STATUS file for an iteration."""
        (self._round_dir(iteration) / "STATUS").write_text(
            status + "\n", encoding="utf-8"
        )

    def read_round_status(self, iteration: int) -> Optional[str]:
        """Read the STATUS of an iteration's round dir, or None if no file."""
        path = self.rounds_dir / f"iter_{iteration:03d}" / "STATUS"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def persist_critic_output(
        self, iteration: int, role: str, issues: list[dict]
    ) -> None:
        """Persist one critic's issues immediately after it returns.

        `role` is the key the issues will occupy in critics_output
        (e.g. "requirement", "design", "rationale", "reframer",
        "exploration"). Crash-recovery reads these back.
        """
        (self._round_dir(iteration) / f"critic_{role}.json").write_text(
            json.dumps(issues, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_critics_output_from_disk(
        self, iteration: int, roles: Iterator[str]
    ) -> dict[str, list[dict]]:
        """Rebuild critics_output dict from per-role JSON files on disk.

        Used when resuming from STATUS >= critics_done. Missing files
        (e.g. a critic role that didn't run this iteration) are
        represented as empty lists in the returned dict ONLY when the
        caller's role list includes them; unknown extras are ignored.
        """
        out: dict[str, list[dict]] = {}
        round_dir = self.rounds_dir / f"iter_{iteration:03d}"
        for role in roles:
            path = round_dir / f"critic_{role}.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        out[role] = data
                        continue
                except json.JSONDecodeError:
                    pass
            out[role] = []
        return out

    def persist_reframer_package(self, iteration: int, package: dict) -> None:
        """Persist the raw reframer package (hard_constraints + alternatives)."""
        (self._round_dir(iteration) / "reframer_package.json").write_text(
            json.dumps(package, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_reframer_package(self, iteration: int) -> Optional[dict]:
        path = self.rounds_dir / f"iter_{iteration:03d}" / "reframer_package.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def persist_judgment(self, iteration: int, judgment: dict) -> None:
        """Persist Judge's raw output immediately after Judge returns."""
        (self._round_dir(iteration) / "judgment.json").write_text(
            json.dumps(judgment, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_judgment_artifact(self, iteration: int) -> Optional[dict]:
        path = self.rounds_dir / f"iter_{iteration:03d}" / "judgment.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def persist_round_flags(self, iteration: int, flags: dict) -> None:
        """Persist round-level flags (critic_failures, contaminated lists,
        reframer_fired, manually_edited) so recovery can restore them."""
        (self._round_dir(iteration) / "round_flags.json").write_text(
            json.dumps(flags, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_round_flags(self, iteration: int) -> Optional[dict]:
        path = self.rounds_dir / f"iter_{iteration:03d}" / "round_flags.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def snapshot_design_pre_critics(self, iteration: int) -> Optional[Path]:
        """Save the state of design.md at the START of a round.

        Drops it at `.pcd/rounds/iter_NNN/design.pre_critics.md`. Used
        as the round-level audit snapshot — if a private-staging
        contamination triggers or if you want to diff "what critics
        saw" vs "what Proposer produced", this is the canonical input.

        Returns the snapshot path, or None if design.md doesn't exist.
        """
        if not self.design_path.exists():
            return None
        round_dir = self.rounds_dir / f"iter_{iteration:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        snapshot = round_dir / "design.pre_critics.md"
        snapshot.write_bytes(self.design_path.read_bytes())
        return snapshot

    @staticmethod
    def _append_jsonl(path: Path, record: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
