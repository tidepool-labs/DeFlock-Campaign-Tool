"""Offline acceptance for the Sitting 5 gate pack."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib import import_module
import json
import pkgutil
import unittest

from campaign_tool import records
from campaign_tool.records import golive_gate, golive_pair, golive_record
from campaign_tool.records import golive_gate_specs_s5 as pack
from tests.records import test_golive_gate as support


SWITCH, BACKUP, UNITS = 's5-engine-switch', 's5b-backup', 's8-units'
STAGES = {SWITCH: ['15'], BACKUP: ['19'], UNITS: ['3', '18']}
NOW = datetime(2031, 1, 2, 18, 0, tzinfo=timezone.utc)  # Thursday 10:00 Pacific
ENGINE = 'a' * 40
OTHER_ENGINE = 'b' * 40
FLOOR = 40_000_000_000
WRAPPER = f'#!/bin/sh\n# engine-{ENGINE}\n'.encode()
LIVE = json.dumps({'engine': f'engine-{ENGINE}'}).encode()
PINS = {'wrapper': sha256(WRAPPER).hexdigest(), 'live': sha256(LIVE).hexdigest()}
PASS_CODES = {
    SWITCH: [('sha256_pin', 'GATE_SHA256_MATCH'), ('sha256_pin', 'GATE_SHA256_MATCH'),
             ('disk_floor_gib', 'GATE_DISK_SUFFICIENT'), ('window', 'GATE_WINDOW_OPEN'),
             ('evidence', 'EVIDENCE_MATCH')],
    BACKUP: [('file_present', 'GATE_FILE_PRESENT'), ('disk_floor_gib', 'GATE_DISK_SUFFICIENT'),
             ('evidence', 'EVIDENCE_MATCH')],
    UNITS: [('prior_gate', 'EVIDENCE_MATCH'), ('evidence', 'EVIDENCE_MATCH'), ('evidence', 'EVIDENCE_MATCH')],
}
MISSING = ('unknown', 'GATE_INPUT_MISSING')


def pins_for(gate_id):
    return PINS if gate_id == SWITCH else {}


def prefix_conflicts(ids):
    return sorted((a, b) for a in ids for b in ids if a != b and b.startswith(a))


class RegistryTests(unittest.TestCase):
    def test_c1_registry_references(self):
        """C1: registered stages and predecessor must use their exact identifiers."""
        self.assertEqual({key: (value['gate_id'], value['stages']) for key, value in pack.GATES.items()},
                         {key: (key, stages) for key, stages in STAGES.items()})
        self.assertEqual({key: [c['gate_id'] for c in value['checks'] if c['type'] == 'prior_gate']
                          for key, value in pack.GATES.items()}, {SWITCH: [], BACKUP: [], UNITS: [SWITCH]})
        self.assertIn(SWITCH, pack.GATES)

    def test_c1_pack_ids_are_prefix_free(self):
        """C1: a new gate whose ID prefixes another would alias a prior receipt."""
        registries = [import_module(info.name).GATES
                      for info in pkgutil.iter_modules(records.__path__, records.__name__ + '.')
                      if info.name.rsplit('.', 1)[-1].startswith('golive_gate_specs_')]
        ids = [gate_id for registry in registries for gate_id in registry]
        self.assertEqual(sorted(set(ids)), sorted(ids))
        self.assertEqual(prefix_conflicts(ids), [])
        alias = 's5-engine-switch-other'
        self.assertEqual(prefix_conflicts(ids + [alias]), [(SWITCH, alias)])

    def test_c2_exact_spec_and_fresh_copy(self):
        """C2: serialize all resolved specs without leaving pins or edits in the templates."""
        for gate_id in STAGES:
            with self.subTest(gate_id=gate_id):
                template = deepcopy(pack.GATES[gate_id])
                expected = deepcopy(template)
                for check in expected['checks']:
                    if check['type'] == 'sha256_pin':
                        self.assertIsNone(check['expected'])
                        check['expected'] = PINS[check['input']]
                actual = pack.spec(gate_id, **pins_for(gate_id))
                self.assertEqual(golive_gate.load_spec(json.loads(json.dumps(actual))), expected)
                actual['stages'].clear()
                actual['checks'][0]['label'] = 'changed'
                self.assertEqual(pack.GATES[gate_id], template)
                self.assertEqual(pack.spec(gate_id, **pins_for(gate_id)), expected)

    def test_c2_refusals(self):
        """C2: never accept a prefix alias, unset pin, typo or invalid SHA-256."""
        for gate_id in ('not-a-gate', 's5-engine-switch-other'):
            with self.subTest(gate_id=gate_id):
                with self.assertRaises(KeyError) as caught:
                    pack.spec(gate_id)
                self.assertIs(type(caught.exception), KeyError)
        cases = [(SWITCH, {}), (SWITCH, {'live': PINS['live']}),
                 (SWITCH, {'wrapper': PINS['wrapper']}), (SWITCH, {**PINS, 'other': PINS['live']}),
                 (BACKUP, {'wrapper': PINS['wrapper']}), (UNITS, {'live': PINS['live']})]
        for name in PINS:
            cases.extend((SWITCH, {**PINS, name: bad}) for bad in ('z' * 64, 'a' * 63, None))
        for gate_id, pins in cases:
            with self.subTest(gate_id=gate_id, pins=pins):
                with self.assertRaises(ValueError) as caught:
                    pack.spec(gate_id, **pins)
                self.assertIs(type(caught.exception), ValueError)


class GateEvaluationTests(support.GateFixture):
    def setUp(self):
        super().setUp()
        self.files = {'wrapper': self.fixture('wrapper', WRAPPER), 'live': self.fixture('live', LIVE),
                      'disk': self.fixture('disk', f'Avail\n{FLOOR}\n'.encode()),
                      'quiescence': self.fixture('quiet', b'synthetic quiescence capture\n')}
        pair = golive_pair.inspect(self.files['wrapper'], self.files['live'])
        self.files['pair'] = self.fixture('pair.json', json.dumps(pair).encode())

    def row(self, stage, *, age=timedelta(hours=1), outcome='pass', engine=ENGINE,
            schema='records-rehearsal-v1'):
        return {'schema': 'records-golive-evidence-v1', 'stage': stage, 'kind': 'rehearsal',
                'result': outcome, 'engine': engine,
                'recorded_at': (NOW - age).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'source': {'schema': schema, 'name': f'rehearsal-{stage}.json',
                           'sha256': sha256(f'{stage}:{age}:{outcome}:{engine}'.encode()).hexdigest()},
                'actor': 'rehearsal', 'codes': []}

    def ledger(self, rows):
        folder = self.root / f'ledger-{len(list(self.root.iterdir()))}'
        folder.mkdir(mode=0o700)
        entries = [(golive_record.append(row, folder).name, row) for row in rows]
        raw = json.dumps([row for _, row in sorted(entries)], sort_keys=True, separators=(',', ':'))
        return folder, sha256(raw.encode('ascii')).hexdigest()

    def assert_receipt(self, gate_id, rows=(), *, now=NOW, replace=None, unavailable=(), changes=None,
                       bad_ledger=False, pair_read=False):
        value = pack.spec(gate_id, **pins_for(gate_id))
        ledger, digest = self.ledger(rows)
        files = self.files | {'evidence': ledger} | (replace or {})
        if bad_ledger:
            files['evidence'] = self.files['disk']
        for name in unavailable:
            files.pop(name)
        consulted = {SWITCH: {'wrapper', 'live', 'disk'}, BACKUP: {'quiescence', 'disk'}, UNITS: set()}[gate_id]
        codes = list(PASS_CODES[gate_id])
        outcomes = changes or {}
        # The evaluator reads pair JSON only for a fresh passing rehearsal.
        if pair_read or any(kind == 'evidence' and i not in outcomes for i, (kind, _) in enumerate(codes)) or any(
                code == 'EVIDENCE_ENGINE_MISMATCH' for _, code in outcomes.values()):
            consulted.add('pair')
        hashes = {name: sha256(files[name].read_bytes()).hexdigest()
                  for name in consulted.intersection(files) if files[name].is_file()}
        if not bad_ledger and 'evidence' in files:
            hashes['evidence'] = digest
        self.assertEqual(golive_gate.evaluate(value, files, now), {
            'schema': 'records-golive-gate-v1', 'gate_id': gate_id, 'stages': STAGES[gate_id],
            'evaluated_at': now.isoformat(), 'verdict': 'NO-GO' if outcomes else 'GO', 'inputs': hashes,
            'checks': [support.result({'type': kind, 'label': value['checks'][i]['label']},
                                      *outcomes.get(i, ('pass', code)))
                       for i, (kind, code) in enumerate(codes)],
        })

    def switch_record(self, *, now=NOW, go=True):
        files = self.files | {'evidence': self.ledger([self.row('15')])[0]}
        if not go:
            files.pop('wrapper')
        receipt = golive_gate.evaluate(pack.spec(SWITCH, **PINS), files, NOW)
        self.assertEqual(receipt['verdict'], 'GO' if go else 'NO-GO')
        capture = self.fixture(f'{SWITCH}.json', json.dumps(receipt).encode())
        capture.chmod(0o600)
        return golive_record.from_gate(capture, now)[0]

    def test_c3_go(self):
        """C3: a qualifying snapshot yields a complete receipt for each gate."""
        self.assert_receipt(SWITCH, [self.row('15')])
        self.assert_receipt(BACKUP, [self.row('19')])
        self.assert_receipt(UNITS, [self.switch_record(), self.row('3'), self.row('18')],
                            now=NOW + timedelta(minutes=25))

    def test_c3_pair_pins(self):
        """C3: matching one candidate file cannot excuse a missing or drifting partner."""
        changed = self.fixture('drift', b'different synthetic bytes\n')
        for i, name in enumerate(('wrapper', 'live')):
            with self.subTest(name=name):
                self.assert_receipt(SWITCH, [self.row('15')], unavailable=(name,), changes={i: MISSING})
                self.assert_receipt(SWITCH, [self.row('15')], replace={name: changed},
                                    changes={i: ('fail', 'GATE_SHA256_MISMATCH')})

    def test_c3_disk_and_quiescence(self):
        """C3: enforce the plan's byte floor on both gates and require the S5b capture."""
        low = self.fixture('low-disk', f'Avail\n{FLOOR - 1}\n'.encode())
        malformed = self.fixture('bad-disk', b'not a df capture\n')
        for gate_id, i in ((SWITCH, 2), (BACKUP, 1)):
            rows = [self.row(STAGES[gate_id][0])]
            with self.subTest(gate_id=gate_id):
                self.assert_receipt(gate_id, rows, unavailable=('disk',), changes={i: MISSING})
                self.assert_receipt(gate_id, rows, replace={'disk': low},
                                    changes={i: ('fail', 'GATE_DISK_BELOW_FLOOR')})
                self.assert_receipt(gate_id, rows, replace={'disk': malformed},
                                    changes={i: ('unknown', 'GATE_DISK_UNPARSED')})
        self.assert_receipt(BACKUP, [self.row('19')], unavailable=('quiescence',), changes={0: MISSING})

    def test_c3_pacific_window(self):
        """C3: both the Thursday selector and Pacific minute bounds must apply."""
        last_minute = NOW + timedelta(minutes=9)
        self.assert_receipt(SWITCH, [self.row('15')], now=last_minute)
        for delta in (timedelta(minutes=-1), timedelta(minutes=10), timedelta(days=1)):
            with self.subTest(delta=delta):
                self.assert_receipt(SWITCH, [self.row('15')], now=NOW + delta,
                                    changes={3: ('fail', 'GATE_WINDOW_CLOSED')})

    def test_c3_each_rehearsal_requires_qualification(self):
        """C3: no passing sibling, unrelated proof, or old/wrong-engine run may stand in."""
        prior = self.switch_record()
        for gate_id, stage, i, other in ((SWITCH, '15', 4, []), (BACKUP, '19', 2, []),
                                         (UNITS, '3', 1, [prior, self.row('18')]),
                                         (UNITS, '18', 2, [prior, self.row('3')])):
            for rows, result in [([], ('unknown', 'EVIDENCE_MISSING')),
                                 ([self.row(stage, schema='records-e2e-proof-v1')], ('unknown', 'EVIDENCE_MISSING')),
                                 ([self.row(stage, outcome='fail')], ('fail', 'EVIDENCE_FAILED')),
                                 ([self.row(stage, age=timedelta(days=8))], ('fail', 'EVIDENCE_STALE')),
                                 ([self.row(stage, engine=OTHER_ENGINE)], ('fail', 'EVIDENCE_ENGINE_MISMATCH'))]:
                with self.subTest(gate_id=gate_id, stage=stage, rows=rows):
                    self.assert_receipt(gate_id, other + rows, changes={i: result})

    def test_c3_missing_and_unreadable_inputs(self):
        """C3: unavailable ledger or pair captures are NO-GO in every applicable check."""
        malformed = self.fixture('malformed-pair.json', b'{')
        for gate_id, rows, rehearsal_indices in ((SWITCH, [self.row('15')], [4]),
                                                  (BACKUP, [self.row('19')], [2]),
                                                  (UNITS, [self.switch_record(), self.row('3'), self.row('18')], [1, 2])):
            all_indices = [0] + rehearsal_indices if gate_id == UNITS else rehearsal_indices
            with self.subTest(gate_id=gate_id):
                self.assert_receipt(gate_id, rows, unavailable=('evidence',),
                                    changes=dict.fromkeys(all_indices, ('unknown', 'EVIDENCE_MISSING')))
                self.assert_receipt(gate_id, rows, bad_ledger=True,
                                    changes=dict.fromkeys(all_indices, ('unknown', 'EVIDENCE_UNREADABLE')))
                self.assert_receipt(gate_id, rows, unavailable=('pair',),
                                    changes=dict.fromkeys(rehearsal_indices, ('unknown', 'EVIDENCE_UNREADABLE')))
                self.assert_receipt(gate_id, rows, replace={'pair': malformed}, pair_read=True,
                                    changes=dict.fromkeys(rehearsal_indices, ('unknown', 'EVIDENCE_UNREADABLE')))

    def test_c3_prior_gate(self):
        """C3: S8 refuses an absent, unrelated, failed or stale switch receipt."""
        prior = self.switch_record()
        source = prior['source']
        assert isinstance(source, dict)
        unrelated = dict(prior, source=dict(source, name='unrelated-gate.json'))
        failed = self.switch_record(go=False)
        stale = dict(prior, recorded_at=(NOW - timedelta(days=1, seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        rows = [self.row('3'), self.row('18')]
        for extra, result in (([], ('unknown', 'EVIDENCE_MISSING')),
                              ([unrelated], ('unknown', 'EVIDENCE_MISSING')),
                              ([failed], ('fail', 'EVIDENCE_FAILED')),
                              ([stale], ('fail', 'EVIDENCE_STALE'))):
            with self.subTest(extra=extra):
                self.assert_receipt(UNITS, rows + extra, changes={0: result})
