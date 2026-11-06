"""Payment-term agent for accounts payable: rules in code, the agent only for exceptions."""
import os
from pathlib import Path

__version__ = "0.1.0"


def _repo_root() -> Path:
    configured = os.environ.get("AP_AGENT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[2])
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "policy" / "term-rules.yaml").is_file() and (root / "integration" / "agent-tools.yaml").is_file():
            return root
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
