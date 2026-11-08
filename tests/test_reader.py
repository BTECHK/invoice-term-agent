"""The local reader: offline, exact on the labelled set, reproducible, and the source of the committed transcripts."""
import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import ROOT, read_json, run_cli, write_json

EXPECTED = read_json(ROOT / 'evals/golden/expected.json')['invoices']
FIELDS = ('invoice_id', 'vendor_name', 'vendor_address', 'po_reference', 'company_code', 'invoice_value', 'payment_terms')


def _grader():
    spec = importlib.util.spec_from_file_location('grade', ROOT / 'evals/grade.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blocked(*args, **kwargs):
    raise OSError('network access is blocked in this test')


def _fold(text):
    return ' '.join(text.split())


class ReaderTests(unittest.TestCase):
    """One offline reader run over the six golden invoices feeds every check here."""

    @classmethod
    def setUpClass(cls):
        from invoice_agent import cli
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name) / 'transcripts'
        with patch('socket.socket', _blocked), patch('socket.create_connection', _blocked):
            code = cli.read_main(['--inputs', str(ROOT / 'evals/golden/invoices'),
                                  str(ROOT / 'evals/golden/injection'), '--out', str(cls.out)])
        if code:
            raise AssertionError(f'invoice-agent read exited {code}')
        cls.fresh = {path.parent.name: read_json(path) for path in cls.out.glob('*/extractor.json')}
        cls.grade = _grader()

    def test_r1_the_reader_runs_offline_and_writes_a_transcript_per_invoice(self):
        self.assertEqual(set(self.fresh), set(EXPECTED))
        for iid, envelope in self.fresh.items():
            mode = 'pdf_text_layer' if EXPECTED[iid]['file'].endswith('.pdf') else 'local_ocr'
            self.assertEqual(envelope['metadata']['mode'], mode, iid)
            self.assertEqual(envelope['metadata']['reader_version'], '1.0.0')
            self.assertEqual(set(envelope), {'metadata', 'response', 'reading'})
            self.assertTrue(envelope['reading']['lines'], iid)

    def test_r2_every_field_equals_its_label_after_replay_verification(self):
        from invoice_agent.extract import load_replay, normalized_fields
        table, misses, nulls = [], [], []
        for iid in EXPECTED:
            replay = load_replay(iid, self.out, root=ROOT)
            fields = normalized_fields(replay.extraction)
            cells = []
            for name in FIELDS:
                want = EXPECTED[iid]['fields'][name]
                same = self.grade.same_field(name, fields[name], want)
                cells.append('ok' if same else 'MISS')
                if fields[name] is None:
                    nulls.append((iid, name))
                if not same:
                    misses.append((iid, name, fields[name], want))
            table.append(f'{iid:18}' + ''.join(f'{cell:>9}' for cell in cells))
        header = f'{"invoice":18}' + ''.join(f'{name[:8]:>9}' for name in FIELDS)
        print('\n' + header + '\n' + '\n'.join(table))
        self.assertEqual(nulls, [])
        self.assertEqual(misses, [])

    def test_r3_a_hand_edited_quote_is_quarantined(self):
        from invoice_agent.extract import ReplayError, load_replay
        edits = {
            'NBF-0051376': ('vendor_address', lambda field: field.update(quote=field['quote'] + ' Suite 4')),
            'KBP-2025-0417': ('payment_terms', lambda field: field.update(quote=field['quote'].swapcase())),
            'PCS-25-03917': ('po_reference', lambda field: field.update(page=2)),
        }
        with tempfile.TemporaryDirectory() as tmp:
            transcripts = Path(tmp) / 'transcripts'
            shutil.copytree(ROOT / 'transcripts', transcripts)
            for iid, (name, change) in edits.items():
                path = transcripts / iid / 'extractor.json'
                data = read_json(path)
                change(data['response'][name])
                write_json(path, data)
            out = Path(tmp) / 'out'
            proc = run_cli(out, '--transcripts', transcripts)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            for iid, (name, _) in edits.items():
                with self.subTest(invoice=iid):
                    self.assertEqual(read_json(out / 'quarantine' / (iid + '.json'))['error_class'], 'ReplayError')
                    self.assertFalse((out / 'proposals' / (iid + '.json')).exists())
                    with self.assertRaisesRegex(ReplayError, name):
                        load_replay(iid, transcripts, root=ROOT)
            written = sorted(path.stem for path in (out / 'proposals').glob('*.json'))
            self.assertEqual(written, sorted(set(EXPECTED) - set(edits)))

    def test_r4_confidence_is_the_lowest_score_of_the_lines_quoted(self):
        for iid, envelope in self.fresh.items():
            lines = envelope['reading']['lines']
            for name in FIELDS:
                field = envelope['response'][name]
                low = high = 1.0
                for part in field['quote'].split('\n'):
                    scores = [line['score'] for line in lines
                              if line['page'] == field['page'] and _fold(line['text']) == _fold(part)]
                    self.assertTrue(scores, (iid, name, part))
                    low, high = min(low, min(scores)), min(high, max(scores))
                self.assertGreaterEqual(field['confidence'], round(low, 2) - 0.005, (iid, name))
                self.assertLessEqual(field['confidence'], round(high, 2) + 0.005, (iid, name))
                if envelope['metadata']['mode'] == 'pdf_text_layer':
                    self.assertEqual(field['confidence'], 1.0, (iid, name))

    def test_r5_image_only_copies_of_the_pdfs_read_through_the_ocr_path(self):
        import pymupdf
        from invoice_agent.extract import normalized_fields
        from invoice_agent.reader import extract, read_document
        misses, checked = [], 0
        with tempfile.TemporaryDirectory() as tmp:
            for iid, label in EXPECTED.items():
                if not label['file'].endswith('.pdf'):
                    continue
                scan = pymupdf.open()
                with pymupdf.open(ROOT / 'evals/golden' / label['file']) as source:
                    for page in source:
                        copy = scan.new_page(width=page.rect.width, height=page.rect.height)
                        copy.insert_image(copy.rect, stream=page.get_pixmap(dpi=200).tobytes('png'))
                path = Path(tmp) / f'{iid}.pdf'
                scan.save(path)
                scan.close()
                reading = read_document(path)
                self.assertEqual(reading.mode, 'local_ocr', iid)
                fields = normalized_fields(extract(reading))
                for name in FIELDS:
                    checked += 1
                    if not self.grade.same_field(name, fields[name], label['fields'][name]):
                        misses.append((iid, name, fields[name], label['fields'][name]))
        self.assertEqual(checked, 21)
        self.assertEqual(misses, [])

    def test_r6_injection_flags_are_read_from_the_pixels_with_the_account_masked(self):
        envelope = self.fresh['DLP-2025-0907']
        self.assertEqual(envelope['metadata']['mode'], 'local_ocr')
        flags = envelope['response']['flags']
        self.assertTrue({'bank_or_remit_to_change', 'instruction_in_document'} <= {flag['type'] for flag in flags}, flags)
        quotes = ' '.join(flag['quote'] for flag in flags)
        self.assertIn('****', quotes)
        self.assertNotIn('NL00EXMP0000000000', re.sub('[oO]', '0', quotes).replace(' ', ''))
        self.assertTrue(any('EXMP' in line['text'] for line in envelope['reading']['lines']))

    def test_r7_the_committed_transcripts_are_the_reader_output(self):
        for iid, fresh in self.fresh.items():
            committed = read_json(ROOT / 'transcripts' / iid / 'extractor.json')
            self.assertEqual(committed['metadata']['mode'], fresh['metadata']['mode'], iid)
            self.assertEqual(committed['metadata']['source_sha256'], fresh['metadata']['source_sha256'], iid)
            for name in FIELDS:
                saved, now = committed['response'][name], fresh['response'][name]
                self.assertEqual((saved['value'], saved['quote'], saved['page']), (now['value'], now['quote'], now['page']), (iid, name))
                self.assertLessEqual(abs(saved['confidence'] - now['confidence']), 0.02, (iid, name))
            self.assertEqual({flag['type'] for flag in committed['response']['flags']},
                             {flag['type'] for flag in fresh['response']['flags']}, iid)
