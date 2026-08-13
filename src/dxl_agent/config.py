from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    module_path = Path(__file__).resolve()
    source_checkout = module_path.parents[2]
    if (source_checkout / "demo" / "fixtures").is_dir():
        return source_checkout
    installed_wheel = module_path.parents[1]
    if (installed_wheel / "demo" / "fixtures").is_dir():
        return installed_wheel
    return source_checkout


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    fixture_dir: Path
    policy_path: Path
    max_history_turns: int = 8
    demo_mode: bool = True
    planner_mode: str = "rules"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    operator_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        root = _project_root()
        return cls(
            database_path=Path(
                os.getenv("DXL_DATABASE_PATH", str(Path.cwd() / "data" / "demo.db"))
            ),
            fixture_dir=Path(os.getenv("DXL_FIXTURE_DIR", root / "demo/fixtures")),
            policy_path=Path(os.getenv("DXL_POLICY_PATH", root / "policies/default.toml")),
            max_history_turns=int(os.getenv("DXL_MAX_HISTORY_TURNS", "8")),
            demo_mode=os.getenv("DXL_DEMO_MODE", "true").lower() in {"1", "true", "yes"},
            planner_mode=os.getenv("DXL_PLANNER_MODE", "rules").lower(),
            llm_base_url=os.getenv("DXL_LLM_BASE_URL") or None,
            llm_model=os.getenv("DXL_LLM_MODEL") or None,
            llm_api_key=os.getenv("DXL_LLM_API_KEY") or None,
            operator_key=os.getenv("DXL_OPERATOR_KEY") or None,
        )
