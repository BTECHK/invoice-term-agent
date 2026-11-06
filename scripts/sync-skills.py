#!/usr/bin/env python3
"""Mirror skills/ into .claude/skills and .agents/skills, byte for byte.

skills/ is the source. The two mirrors exist because different agent runtimes read skills
from different folders. Do not edit a mirror by hand.

    python3 scripts/sync-skills.py           # copy, and delete files in a mirror that skills/ lacks
    python3 scripts/sync-skills.py --check   # report drift, change nothing, exit 1 on drift

Standard library only.
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIRRORS = (".claude/skills", ".agents/skills")
IGNORED = {".DS_Store"}


def tree(base):
    """Map relative posix path to file path for every file under base."""
    if not base.is_dir():
        return {}
    return {p.relative_to(base).as_posix(): p for p in sorted(base.rglob("*"))
            if p.is_file() and p.name not in IGNORED and "__pycache__" not in p.parts}


def drift(root=ROOT):
    """Return (source_exists, [(kind, mirror, relative_path)]) with kind missing, changed or extra."""
    src = root / "skills"
    if not src.is_dir():
        return False, []
    want, found = tree(src), []
    for mirror in MIRRORS:
        have = tree(root / mirror)
        for rel in sorted(want):
            if rel not in have:
                found.append(("missing", mirror, rel))
            elif want[rel].read_bytes() != have[rel].read_bytes():
                found.append(("changed", mirror, rel))
        found += [("extra", mirror, rel) for rel in sorted(set(have) - set(want))]
    return True, found


def sync(root=ROOT):
    want = tree(root / "skills")
    for mirror in MIRRORS:
        dst = root / mirror
        for rel, path in want.items():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dst / rel)
        for rel in set(tree(dst)) - set(want):
            (dst / rel).unlink()
        for folder in sorted(dst.rglob("*"), reverse=True):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    return len(want)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Mirror skills/ into .claude/skills and .agents/skills.")
    ap.add_argument("--check", action="store_true", help="report drift and change nothing")
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: the folder above scripts/)")
    args = ap.parse_args(argv)
    root = Path(args.root)
    exists, found = drift(root)
    if not exists:
        print(f"skills/ not found under {root}: nothing to mirror yet")
        return 0
    if args.check:
        for kind, mirror, rel in found:
            print(f"{mirror}/{rel}: drift: {kind} compared with skills/")
        print("mirrors match skills/" if not found else f"{len(found)} drifted file(s): run python3 scripts/sync-skills.py")
        return 1 if found else 0
    count = sync(root)
    print(f"mirrored {count} file(s) from skills/ to {', '.join(MIRRORS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
