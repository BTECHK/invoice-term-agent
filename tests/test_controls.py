"""Small regression checks for control boundaries not observable on the happy path."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import ROOT, SYNTHETIC, read_json
from invoice_agent.erp_client import ErpClient, ErpClientError, QueryValidationError, ThrottleExceeded, ToolDeniedError
from invoice_agent.guard import mask_account_numbers
from invoice_agent.models import Extraction, Proposal, WorklistRecord
from invoice_agent.policy import Policy, load_policy
from invoice_agent.propose import apply_checker, check_proposal
from invoice_agent.worklist import BundleWriter, WriteFailure


# Identifiers come from the fixtures at run time, so a new sample set does not break these tests.
CASES = sorted(p.name for p in (SYNTHETIC / 'transcripts').iterdir() if p.is_dir())
CASE = CASES[0]
SYNTHETIC_PO = read_json(SYNTHETIC / 'fixtures/purchase_orders.json')['records'][0]['po_line_key']
MOCK_PO = read_json(ROOT / 'mock_erp/fixtures/purchase_orders.json')['records'][0]['po_line_key']


def proposal():
    # A label for the writer tests; the figures are not under test there.
    return Proposal(invoice_id=CASE, po_reference=SYNTHETIC_PO,
                    governing_term_days=30, term_source='po', proposed_payment_day=30,
                    discount_taken=False, discount_rate=None, annualized_discount=None,
                    reason_code='AGREED_TERM', rule_version='1.0.0', checker_agrees=True)


def payload():
    return dict(invoice_id=CASE, source_sha256='1' * 64,
                semantic_sha256='2' * 64, proposal=proposal(), worklist=None,
                audit_events=[], result={'invoice_id': CASE})


class Clock:
    def __init__(self):
        self.now, self.waits = 0.0, []
    def __call__(self):
        return self.now
    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class Replies:
    def __init__(self, clock, *items):
        self.clock, self.items = clock, iter(items)
        self.calls, self.sent_at = [], []
    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        self.sent_at.append(self.clock())
        result = next(self.items)
        if isinstance(result, Exception):
            raise result
        return result


def response(body=None, error=False):
    return SimpleNamespace(is_error=error, structured_content={'ok': True} if body is None else body)


def connected(*items, **options):
    clock = Clock()
    client = ErpClient(object(), config_path=ROOT / 'integration/agent-tools.yaml',
                           clock=clock, sleep=clock.sleep, **options)
    client._client = Replies(clock, *items)
    return client, clock


class ControlTests(unittest.TestCase):
    def test_independent_checker_holds_a_tampered_payment_day(self):
        from invoice_agent.decide import decide
        from invoice_agent.exceptions import gather_facts
        from invoice_agent.propose import build_proposal
        policy = load_policy(ROOT)
        fixture_rows = read_json(SYNTHETIC / 'fixtures/purchase_orders.json')['records']
        validated_rows = read_json(SYNTHETIC / 'fixtures/validated_terms.json')['records']

        async def check():
            async with ErpClient.stdio(root=ROOT, fixtures_dir=SYNTHETIC / 'fixtures') as client:
                for case in CASES:
                    extraction = Extraction.model_validate(read_json(
                        SYNTHETIC / 'transcripts' / case / 'extractor.json')['response'])
                    facts = await gather_facts(extraction, client)
                    decision = decide(facts.invoice, facts.po, list(facts.validated), policy)
                    if decision.route == 'clear' and not decision.discount_taken:
                        break
                else:
                    self.fail('the synthetic set needs a clear case without a taken discount')
                good_proposal = build_proposal(decision, invoice_id=case, po_reference=facts.invoice.po_reference,
                                               rule_version=policy.rule_version, checker_agrees=True)
                day = good_proposal.proposed_payment_day
                order = next(r for r in fixture_rows if r['po_line_key'] == facts.invoice.po_reference)
                terms = {order['po_term_days'], order['master_term_days'],
                         *(v['term_days'] for v in validated_rows if v['vendor'] == order['vendor'])}
                self.assertIn(day, terms, 'the proposed day must be a term the fixtures hold')
                good = await check_proposal(facts.invoice, good_proposal, client, policy)
                candidate = good_proposal.model_copy(update={'proposed_payment_day': day + 1})
                bad = await check_proposal(facts.invoice, candidate, client, policy)
            self.assertTrue(good.agrees, good.reason)
            self.assertFalse(bad.agrees)
            held = apply_checker(candidate, bad)
            self.assertIsNone(held.proposed_payment_day)
            self.assertFalse(held.discount_taken)
            self.assertFalse(held.checker_agrees)
        asyncio.run(check())

    def test_real_mcp_denies_writes_unknown_queries_and_excess_calls(self):
        async def check():
            events = []
            async with ErpClient.stdio(root=ROOT, calls_per_minute=1, max_wait_seconds=0) as client:
                with client.audit_to(lambda *event: events.append(event)):
                    for tool in ('trigger_erp_action', 'start_workflow',
                                 'resume_workflow'):
                        with self.assertRaises(ToolDeniedError):
                            await client._call_tool(tool, {})
                    with self.assertRaises(QueryValidationError):
                        await client.run_query('unregistered_query', {})
                    await client.run_query('po_terms', {'po_reference': MOCK_PO})
                    with self.assertRaises(ThrottleExceeded):
                        await client.run_query('po_terms', {'po_reference': MOCK_PO})
            denied = [e for e in events if e[3] is not None]
            self.assertEqual(len(denied), 5)
            self.assertTrue(all(e[2] is None for e in denied))
        asyncio.run(check())

    def test_malformed_mcp_response_has_one_failure_event(self):
        # Deliberately create a server contract violation.
        class BrokenResponse:
            async def call_tool(self, name, arguments):
                return SimpleNamespace(is_error=False, structured_content={})

        async def check():
            client = ErpClient(object(), config_path=ROOT / 'integration/agent-tools.yaml')
            client._client = BrokenResponse()
            events = []
            with client.audit_to(lambda *event: events.append(event)):
                with self.assertRaises(ErpClientError):
                    await client.run_query('po_terms', {'po_reference': MOCK_PO})
            self.assertEqual(len(events), 1)
            self.assertIsNone(events[0][2])
            self.assertIsInstance(events[0][3], ErpClientError)
        asyncio.run(check())

    def test_read_methods_reject_undeclared_queries_and_bad_arguments_before_any_call(self):
        async def check():
            client, clock = connected(*(response() for _ in range(4)))
            self.assertFalse(hasattr(client, 'call_tool'), 'the unvalidated entry point must be private')
            queries = client._queries
            query_id = next(iter(queries))
            variable = queries[query_id]['variables'][0]
            id_query = next(q for q in queries.values() if 'invoice_id' in q['variables'])
            rejected = [
                client.run_query('undeclared_query_id', {}),
                client.run_query(query_id, {variable: "x' OR '1'='1"}),
                client.run_query(query_id, {variable: '7' * 10 + '\n'}),
                client.run_query(id_query['id'], {'invoice_id': '../etc'}),
                client.run_query(id_query['id'], {'invoice_id': 'a/b'}),
                client.run_query(id_query['id'], {'invoice_id': 'a' * 41}),
                client.get_kpis('Bad Topic'), client.get_kpis(''), client.get_kpis(None),
                client.get_event_timeline('123'), client.get_event_timeline('7' * 10 + '\n'),
                client.search_records('x' * 81), client.search_records('line\nbreak'), client.search_records(''),
                client.search_records('ok', limit=0), client.search_records('ok', limit=4), client.search_records('ok', limit=True),
            ]
            events = []
            with client.audit_to(lambda *event: events.append(event)):
                for coroutine in rejected:
                    with self.assertRaises(QueryValidationError):
                        await coroutine
            self.assertEqual(client._client.calls, [])
            self.assertEqual(clock.waits, [])
            self.assertEqual(len(events), len(rejected), 'every refusal is audited once')
            self.assertTrue(all(e[3] is not None and e[2] is None for e in events))
            self.assertEqual(client.validate_query(id_query['id'], {'invoice_id': 'A-1.2_b'}), {'invoice_id': 'A-1.2_b'})
            self.assertEqual(await client.get_data_catalog(), {'ok': True})
            self.assertEqual(await client.get_kpis('topic_x'), {'ok': True})
            self.assertEqual(await client.get_event_timeline('7' * 10), {'ok': True})
            self.assertEqual(await client.search_records('some text'), {'ok': True})
            self.assertEqual(client._client.calls, [
                ('get_data_catalog', {}), ('get_kpis', {'topic': 'topic_x'}),
                ('get_event_timeline', {'po_reference': '7' * 10}), ('search_records', {'query': 'some text', 'limit': 3})])
        asyncio.run(check())

    def test_mock_server_matches_whole_values_and_names_events_as_the_contract_does(self):
        references = [r['po_line_key'] for r in read_json(SYNTHETIC / 'fixtures/purchase_orders.json')['records']]
        contract = (ROOT / 'integration/agent-tools.yaml').read_text()
        names = set(re.findall(r"event = '([a-z_]+)'", contract))
        self.assertGreaterEqual(len(names), 6)

        async def check():
            async with ErpClient.stdio(root=ROOT, fixtures_dir=SYNTHETIC / 'fixtures') as client:
                seen = set()
                for reference in references:
                    body = await client.get_event_timeline(reference)
                    seen |= {event['event'] for event in body['events']}
                self.assertTrue(seen and seen <= names, (seen, names))
                # The client check is bypassed on purpose: the server must refuse a trailing newline itself.
                for tool, arguments in (('get_event_timeline', {'po_reference': references[0] + '\n'}),
                                        ('run_query', {'query': 'po_terms', 'variables': {'po_reference': references[0] + '\n'}})):
                    with self.subTest(tool=tool), self.assertRaises(ErpClientError):
                        await client._call_tool(tool, arguments)
        asyncio.run(check())

    def test_interrupted_bundle_recovers_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(WriteFailure):
                BundleWriter(out, fault_after='journal').commit(**payload())
            recovered = BundleWriter(out)
            recovered.recover()
            recovered.recover()
            recovered.finalize_results()
            self.assertEqual(read_json(out / 'proposals' / (CASE + '.json')), proposal().model_dump())
            self.assertEqual(len((out / 'results.jsonl').read_text().splitlines()), 1)
            self.assertEqual(list((out / '.transactions').glob('*.json')), [])

    def test_redirected_audit_directory_cannot_write_outside_output(self):
        for directory in ('audit', '.state', '.locks'):
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory() as tmp:
                    out, outside = Path(tmp) / 'out', Path(tmp) / 'outside'
                    out.mkdir()
                    outside.mkdir()
                    (out / directory).symlink_to(outside, target_is_directory=True)
                    with self.assertRaises(WriteFailure):
                        BundleWriter(out)
                    self.assertEqual(list(outside.iterdir()), [])

    def test_offline_writer_refuses_a_forged_approval(self):
        record = WorklistRecord(invoice_id=CASE, exception_types=['no-validated-term'],
                                evidence=[{'source': 'rules', 'query_id': 'term', 'value': 30}],
                                recommendation='Human review required.', money_impact={'amount': 0, 'currency': 'EUR'},
                                created_at=datetime.now(timezone.utc), status='approved', decided_by='person')
        with tempfile.TemporaryDirectory() as tmp:
            args = payload()
            args['worklist'] = record
            with self.assertRaisesRegex(WriteFailure, 'cannot record approvals'):
                BundleWriter(Path(tmp)).commit(**args)
            self.assertEqual(list((Path(tmp) / 'proposals').glob('*.json')), [])

    def test_local_cap_waits_and_bound_stops_before_an_extra_send(self):
        async def check():
            client, clock = connected(*(response() for _ in range(10)), calls_per_minute=3)
            with self.assertLogs('invoice_agent.erp_client', level='INFO') as logs:
                for _ in range(10):
                    await client._call_tool('get_kpis')
            self.assertEqual(clock.waits, [60, 60, 60])
            self.assertEqual(len(client._client.calls), 10)
            for end in client._client.sent_at:
                self.assertLessEqual(sum(end - 60 < sent <= end for sent in client._client.sent_at), 3)
            self.assertTrue(all('client window' in line for line in logs.output))
            limited, clock = connected(response(), calls_per_minute=1, max_wait_seconds=5)
            await limited._call_tool('get_kpis')
            events=[]
            with limited.audit_to(lambda *event: events.append(event)):
                with self.assertRaises(ThrottleExceeded):
                    await limited._call_tool('get_kpis')
            self.assertEqual(clock.waits, [])
            self.assertEqual(len(limited._client.calls), 1)
            self.assertIsInstance(events[0][3], ThrottleExceeded)
            with self.assertRaisesRegex(ValueError, 'local client ceiling'):
                connected(calls_per_minute=1000)
            connected(calls_per_minute=999)
        asyncio.run(check())

    def test_the_policy_loader_applies_the_same_local_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('policy/term-rules.yaml', 'policy/tool-policy.yaml', 'integration/agent-tools.yaml'):
                (root / name).parent.mkdir(exist_ok=True)
                (root / name).write_text((ROOT / name).read_text())
            self.assertLess(load_policy(root).calls_per_minute, 1000)
            policy_file = root / 'policy/tool-policy.yaml'
            text = policy_file.read_text()
            self.assertEqual(len(re.findall(r'^  calls_per_minute: \d+$', text, flags=re.M)), 1)
            policy_file.write_text(re.sub(r'^  calls_per_minute: \d+$', '  calls_per_minute: 1000', text, flags=re.M))
            with self.assertRaisesRegex(ValueError, 'local client ceiling'):
                load_policy(root)

    def test_transient_attempts_count_toward_cap_and_stop_after_three(self):
        async def check():
            client, clock = connected(TimeoutError(), ConnectionError(), response(), calls_per_minute=2)
            events=[]
            with client.audit_to(lambda *event: events.append(event)):
                self.assertEqual(await client._call_tool('get_kpis', {'scope': 'test'}), {'ok': True})
            self.assertEqual(clock.waits, [0.5, 1.0, 58.5])
            self.assertEqual([e[1]['attempt'] for e in events], [1, 2, 3])
            self.assertTrue(all(args == {'scope': 'test'} for _, args in client._client.calls))
            self.assertTrue(all(e[2] is None and e[3] is not None for e in events[:2]))
            self.assertIsNone(events[2][3])
            failed, clock = connected(TimeoutError(), TimeoutError(), TimeoutError(), response())
            with self.assertRaises(ErpClientError):
                await failed._call_tool('get_kpis')
            self.assertEqual(len(failed._client.calls), 3)
            self.assertEqual(clock.waits, [0.5, 1.0])
        asyncio.run(check())

    def test_server_rate_limit_respects_seconds_and_http_date(self):
        async def check():
            for retry_after in ('3', 'Thu, 01 Jan 1970 00:00:03 GMT'):
                client, clock = connected(response({'status_code': 429,
                    'headers': {'Retry-After': retry_after}}, error=True), response())
                with patch('invoice_agent.erp_client.time.time', return_value=0):
                    with self.assertLogs('invoice_agent.erp_client', level='INFO') as logs:
                        self.assertEqual(await client._call_tool('get_kpis', {'token': 'sensitive-fixture'}), {'ok': True})
                self.assertTrue(any('server Retry-After' in line for line in logs.output))
                self.assertNotIn('sensitive-fixture', ' '.join(logs.output))
                self.assertEqual(clock.waits, [3])
                self.assertEqual(len(client._client.calls), 2)
        asyncio.run(check())

    def test_deterministic_and_malformed_calls_never_retry(self):
        async def check():
            for bad in (ValueError('invalid'), response({'code': 'invalid_query'}, error=True), response({})):
                client, clock = connected(bad, response())
                events=[]
                with client.audit_to(lambda *event: events.append(event)):
                    with self.assertRaises(ErpClientError):
                        await client.run_query('po_terms', {'po_reference': MOCK_PO})
                self.assertEqual(len(client._client.calls), 1)
                self.assertEqual(clock.waits, [])
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0][1]['attempt'], 1)
                self.assertIsNone(events[0][2])
            denied, clock = connected()
            with self.assertRaises(ToolDeniedError):
                await denied._call_tool('trigger_erp_action')
            with self.assertRaises(QueryValidationError):
                await denied.run_query('unregistered_query', {})
            self.assertEqual(denied._client.calls, [])
            self.assertEqual(clock.waits, [])
        asyncio.run(check())

    def test_symlinked_output_parent_is_rejected_before_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside, link = root / 'outside', root / 'linked-parent'
            outside.mkdir()
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(WriteFailure, 'symlink'):
                BundleWriter(link / 'nested' / 'output')
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((outside / 'nested').exists())

    def test_invoice_id_survives_while_account_and_iban_are_masked(self):
        invoice_id = 'INV-123456789012'
        text = f'Invoice {invoice_id}. Account 123456789012. IBAN DE89370400440532013000.'
        masked = mask_account_numbers(text)
        self.assertIn(invoice_id, masked)
        self.assertIn('Account ****9012.', masked)
        self.assertIn('IBAN ****3000.', masked)
        self.assertNotIn('Account 123456789012', masked)
        self.assertNotIn('DE89370400440532013000', masked)
        # A bank-label hyphen must not receive the invoice-ID exemption.
        from invoice_agent.guard import scan_text
        text = 'Updated bank account-123456789012.'
        self.assertEqual(mask_account_numbers(text), 'Updated bank account-****9012.')
        hits = scan_text(text)
        self.assertTrue(hits)
        self.assertTrue(all('123456789012' not in hit.quote for hit in hits))
        self.assertEqual(mask_account_numbers('invoice INV-7731-0418-92'),
                         'invoice INV-7731-0418-92')
