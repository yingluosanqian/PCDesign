"""Project directory layout and metadata persistence."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DESIGN_FILENAME = "design.md"
META_DIR = ".pcd"
META_FILENAME = "meta.json"
INITIAL_PROMPT_FILENAME = "initial_prompt.txt"


@dataclass
class ProjectMeta:
    p_thread_id: str
    proposer_model: Optional[str]
    critic_model: Optional[str]
    created_at: str
    iterations_done: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "ProjectMeta":
        data = json.loads(s)
        return cls(**data)


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.design_path = self.root / DESIGN_FILENAME
        self.meta_dir = self.root / META_DIR
        self.meta_path = self.meta_dir / META_FILENAME
        self.initial_prompt_path = self.meta_dir / INITIAL_PROMPT_FILENAME

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

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
