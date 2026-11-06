"""Run the public CLI and isolate copies of source-grounded fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# macOS /var is an OS symlink. Test outputs use its canonical real path.
tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / 'evals/synthetic'


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value) + '\n')


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def command(*args, cwd=ROOT):
    env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
    env.update(PYTHONPATH=str(ROOT / 'src'), AP_AGENT_ROOT=str(ROOT))
    return subprocess.run([sys.executable, *map(str, args)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60)


def run_cli(output, *args):
    return command('-m', 'invoice_agent.cli', '--output', output, *args)


def single_invoice(root, invoice_id='SYN-EQUAL-001'):
    transcripts = root / 'transcripts'
    shutil.copytree(SYNTHETIC / 'transcripts' / invoice_id, transcripts / invoice_id)
    fixtures = root / 'fixtures'
    shutil.copytree(SYNTHETIC / 'fixtures', fixtures)
    args = ('--inputs', SYNTHETIC / 'inputs', '--transcripts', transcripts,
            '--fixtures', fixtures)
    return args, transcripts / invoice_id / 'extractor.json', fixtures
