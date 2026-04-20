"""Project directory layout, metadata persistence, and append-only logs."""
from __future__ import annotations

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
    ) -> None:
        record = {
            "iteration": iteration,
            "timestamp": self.now_iso(),
            "critics_output": critics_output,
            "judgment": judgment,
        }
        self._append_jsonl(self.judgments_log_path, record)

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

    @staticmethod
    def _append_jsonl(path: Path, record: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False))
            f.write("\n")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
