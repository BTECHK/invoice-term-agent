#!/usr/bin/env python3
"""Build a timestamped allowlist release ZIP without local state or secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    ".env.example",
    ".gitleaks.toml",
    ".mcp.json",
    ".pre-commit-config.yaml",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "GEMINI.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "SECURITY.md",
    "pyproject.toml",
    "requirements.in",
    "requirements.lock",
}
DIRECTORIES = {
    ".agents",
    ".claude",
    ".github",
    "integration",
    "docs",
    "evals",
    "mock_erp",
    "policy",
    "schemas",
    "scripts",
    "skills",
    "src",
    "tests",
    "transcripts",
}
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "build",
    "dist",
    "audit",
    "worklist",
    "proposals",
}
EXCLUDED_NAMES = {
    "results.jsonl",
    "sample-results.jsonl",
    ".env",
    ".env.local",
    ".env.production",
}


def _allowed(path: Path, excluded: Collection[Path] = ()) -> bool:
    relative = path.relative_to(ROOT)
    if path.resolve() in excluded or path.suffix.lower() == ".zip":
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.name in EXCLUDED_NAMES or path.is_symlink():
        return False
    return path.is_file()


def _sources(excluded: Collection[Path] = ()) -> list[Path]:
    resolved_excluded = {path.resolve() for path in excluded}
    sources = [
        ROOT / name
        for name in FILES
        if (ROOT / name).is_file() and (ROOT / name).resolve() not in resolved_excluded
    ]
    for name in DIRECTORIES:
        directory = ROOT / name
        if directory.is_dir():
            sources.extend(
                path for path in directory.rglob("*") if _allowed(path, resolved_excluded)
            )
    return sorted(set(sources), key=lambda path: path.relative_to(ROOT).as_posix())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output: Path, stage: Path | None = None) -> tuple[Path, Path]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    persistent_manifest: Path | None = None
    if stage is None:
        temporary = tempfile.TemporaryDirectory(prefix="invoice-agent-release-")
        stage = Path(temporary.name) / "invoice-term-agent"
        persistent_manifest = output.with_suffix(output.suffix + ".manifest.json")
    stage = stage.resolve()
    if stage == ROOT or ROOT in stage.parents or stage in ROOT.parents:
        raise ValueError("release stage must not contain or overwrite the source tree")
    if stage == output or stage in output.parents:
        raise ValueError("release ZIP must be outside the release stage")
    if stage.exists() and (not stage.is_dir() or any(stage.iterdir())):
        raise ValueError("release stage must be a new or empty directory")
    stage.mkdir(parents=True, exist_ok=True)
    manifest_files: list[dict[str, str]] = []
    excluded_sources = {output}
    if persistent_manifest is not None:
        excluded_sources.add(persistent_manifest)
    for source in _sources(excluded_sources):
        relative = source.relative_to(ROOT)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest_files.append({"path": relative.as_posix(), "sha256": _sha(target)})
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unavailable"
    built_at = datetime.now(timezone.utc).replace(microsecond=0)
    manifest = {
        "built_at_utc": built_at.isoformat(),
        "format_version": 1,
        "source_commit": commit,
        "allowlist": True,
        "excluded": sorted(EXCLUDED_PARTS | EXCLUDED_NAMES),
        "files": manifest_files,
    }
    manifest_path = stage / "RELEASE-MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(stage.name) / path.relative_to(stage)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=built_at.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    if temporary is not None:
        assert persistent_manifest is not None
        persistent_manifest.write_bytes(manifest_path.read_bytes())
        temporary.cleanup()
        manifest_path = persistent_manifest
    return output, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    output, _ = build(args.output, args.stage)
    print(f"release ZIP: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
