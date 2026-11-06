#!/usr/bin/env python3
"""Render the golden invoice sources with headless Google Chrome and print each output's SHA-256.

Every invoice in evals/golden/expected.json has an HTML source at evals/golden/source/<invoice id>.html.
Its `file` label names the output: a .png is a 1240 x 1754 screenshot, a .pdf is a print to A4.
Each Chrome run gets a new, empty --user-data-dir, so it never touches the profile or the windows of a
Chrome you already have open.

    python3 scripts/render_invoices.py
    python3 scripts/render_invoices.py --chrome "/path/to/chrome"

A PNG repeats byte for byte with the same Chrome on the same machine. A PDF does not, because it carries a
creation time. After a re-render, copy the printed hashes into the source_sha256 field of each
transcripts/<invoice id>/extractor.json.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "evals" / "golden"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def render(chrome, source, output):
    """Run Chrome once for one invoice. Returns None on success, or a message."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="invoice-render-") as profile:
        cmd = [chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}"]
        if output.suffix == ".png":
            cmd += ["--hide-scrollbars", "--window-size=1240,1754", f"--screenshot={output}"]
        else:
            cmd += [f"--print-to-pdf={output}", "--no-pdf-header-footer"]
        cmd.append(source.as_uri())
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return "Chrome did not finish within 120 seconds"
    if not output.is_file() or output.stat().st_size == 0:
        return f"Chrome exited {done.returncode} and wrote no file: {done.stderr.strip()[-300:]}"
    # Chrome sometimes exits with 2 on teardown after the file is complete; it says so on stderr first.
    if done.returncode and "bytes written to file" not in done.stderr:
        return f"Chrome exited {done.returncode} before it reported the file as written: {done.stderr.strip()[-300:]}"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render golden invoice HTML sources to PNG and PDF.")
    ap.add_argument("--chrome", default=os.environ.get("CHROME", DEFAULT_CHROME), help="path to the Chrome binary")
    args = ap.parse_args(argv)
    if not Path(args.chrome).is_file():
        print(f"render_invoices.py: Chrome not found at {args.chrome}; pass --chrome", file=sys.stderr)
        return 2
    invoices = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))["invoices"]
    failed = 0
    for invoice_id, label in invoices.items():
        source, output = GOLDEN / "source" / f"{invoice_id}.html", GOLDEN / label["file"]
        problem = f"missing source {source.relative_to(ROOT)}" if not source.is_file() else render(args.chrome, source, output)
        if problem:
            failed += 1
            print(f"FAIL {invoice_id}: {problem}", file=sys.stderr)
            continue
        print(f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
