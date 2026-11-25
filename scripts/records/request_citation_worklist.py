"""List citation gaps from one saved private run; no acquisition or state writes.

Run from the engine checkout: python -m scripts.records.request_citation_worklist
--run-dir /absolute/copied/project/run-outputs/RUN
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import sys

from campaign_tool.records import console_ocr, draft_templates
from campaign_tool.records.outcomes import reading, received_items, register


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('invalid rows')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing identity')
    return value


def _original(row):
    digest = row.get('original_sha256')
    if not isinstance(digest, str) or not received_items.is_hash(digest):
        raise ValueError('invalid original')
    return digest


def _locator(row):
    basis = row.get('kind_basis')
    if not isinstance(basis, dict) or not draft_templates._cited(basis):
        return None
    return 'part' if 'part' in basis else 'page'


def _type(row):
    mime = row.get('mime')
    if row.get('kind') == 'mail':
        return 'part' if mime in (None, 'message/rfc822') else 'unknown'
    if mime == 'application/pdf':
        return 'page'
    return 'attachment' if mime is None and row.get('kind') == 'attachment' else 'unknown'


def build(register_doc, received_doc):
    registered, observed, occurrences = defaultdict(list), defaultdict(list), defaultdict(set)
    for req in _rows(register_doc.get('requests')):
        key = (_text(req.get('agency_key')), _text(req.get('ledger_request_id')))
        for row in _rows(req.get('arrived')):
            digest = _original(row)
            oid = _text(row.get('occurrence_id'))
            registered[digest].append((key, oid, _type(row)))
            occurrences[oid].add(digest)
    for row in _rows(received_doc.get('items')):
        ids = row.get('occurrence_ids')
        if not isinstance(ids, list):
            raise ValueError('invalid occurrence list')
        observed[_original(row)].append(((row.get('agency_id'), row.get('request_id')),
                                         {_text(oid) for oid in ids}, _locator(row)))

    missing, unassigned = {}, []
    for digest in sorted(registered.keys() | observed.keys()):
        arrivals, sightings = registered[digest], observed[digest]
        owners = {key for key, _, _ in arrivals}
        if not arrivals:
            reason = 'no_registered_occurrence'
        elif len(owners) != 1 or any(key not in owners for key, _, _ in sightings):
            reason = 'conflicting_request'
        elif any(len(occurrences[oid]) != 1 for _, oid, _ in arrivals):
            reason = 'conflicting_occurrence'
        else:
            ids = {oid for _, oid, _ in arrivals}
            locators = {locator for _, seen, locator in sightings if ids & seen}
            kinds = {kind for _, _, kind in arrivals}
            if kinds == {'part'} or (kinds <= {'page', 'attachment'} and 'page' in kinds):
                kind = 'part' if kinds == {'part'} else 'page'
                if kind not in locators:
                    agency, request = next(iter(owners))
                    group = missing.setdefault((agency, request), {
                        'agency': agency, 'request': request, 'mail_part_missing': [], 'pdf_page_missing': []})
                    group['mail_part_missing' if kind == 'part' else 'pdf_page_missing'].append(digest)
                continue
            # An unknown-MIME original stays unassigned even when a page citation exists: a page locator
            # does not establish that the original is a PDF.
            reason = 'unknown_type'
        unassigned.append({'original': digest, 'reason': reason})
    return {'run': received_doc['run_id'], 'review_only': True,
            'requests': [missing[key] for key in sorted(missing)], 'unassigned': unassigned}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with reading._directory(args.run_dir) as run_fd:
            # Register v1 has no run_id; the pinned parent directory and occurrence join supply the binding.
            reg, _ = console_ocr._read_json(run_fd, 'CURRENT-REQUEST-REGISTER.json', register.SCHEMA, None)
            outcomes = os.open('outcomes', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=run_fd)
            try:
                rec, _ = console_ocr._read_json(outcomes, 'received-items.json', received_items.SCHEMA, args.run_dir.name)
            finally:
                os.close(outcomes)
        result = build(reg, rec)
    except (OSError, ValueError, TypeError):
        print('Citation worklist: inputs unavailable or inconsistent', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
