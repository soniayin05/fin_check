"""Human claim revisions and deterministic checks of reviewed memo entries."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from .calculations import NUMBER, UNITS, calculate
from .memo import annotate_pdf, artifact_dir, render_report, validate_claim, write_json
from .offline import digest, facts_file, snapshot
from .ratios import RATIOS
from .tools import FactStore, normalize_company, normalize_metric

CLAIM_FIELDS = {'quote', 'company', 'metric', 'period', 'value', 'unit', 'claim_type', 'qualifiers'}
OPERATIONS = {'fact', 'growth', 'ratio'}


def save_revision(directory, memo, bundle, payload):
    if not isinstance(payload, dict) or set(payload) != CLAIM_FIELDS | {'page', 'operation', 'replace_id', 'revision'}:
        raise ValueError('Supply the quotation and claim fields from the editor')
    revision = bundle.get('revision', 0)
    if type(payload['revision']) is not int or payload['revision'] != revision:
        raise ValueError('The claim list changed. Reload it before editing.')
    if revision >= 100:
        raise ValueError('This memo has reached its edit limit')
    page_number = payload['page']
    if type(page_number) is not int or not 1 <= page_number <= len(memo['pages']):
        raise ValueError('Choose a page from this memo')
    operation = payload['operation']
    if operation is not None and (not isinstance(operation, str) or operation not in OPERATIONS):
        raise ValueError('Choose a supported calculation or leave it blank')
    fields = {key: payload[key] for key in CLAIM_FIELDS}
    for key in CLAIM_FIELDS - {'quote', 'qualifiers', 'value'}:
        if not isinstance(fields[key], str) or len(fields[key]) > 160:
            raise ValueError('Claim metadata must be short text')
        fields[key] = fields[key].strip()
    if not isinstance(fields['qualifiers'], list) or len(fields['qualifiers']) > 20:
        raise ValueError('Enter up to 20 qualifiers')
    claim = validate_claim(fields, memo['pages'][page_number - 1])
    claim.update(origin='manual', operation=operation, revision=revision + 1)
    replace_id = payload['replace_id']
    claims = list(bundle['claims'])
    position = next((i for i, item in enumerate(claims) if item['claim_id'] == replace_id), None)
    if replace_id is not None and position is None:
        raise ValueError('The claim you are editing is unavailable')
    old = None if position is None else claims[position]
    if any(i != position and item['claim_id'] == claim['claim_id'] for i, item in enumerate(claims)):
        raise ValueError('This quotation is already in the claim list')
    if position is None:
        if len(claims) >= bundle['claim_limit']:
            raise ValueError('Claim limit reached. Edit an existing claim.')
        claims.append(claim)
    else:
        claims[position] = claim
    edit = dict(revision=revision + 1, edited_at=datetime.now(timezone.utc).isoformat(), before=old, after=claim)
    updated = dict(bundle, claims=claims, revision=revision + 1, edits=[*bundle.get('edits', []), edit])
    pending = directory/'claims.pending'
    try:
        write_json(pending, updated)
        os.replace(pending, directory/'claims.json')
    finally:
        pending.unlink(missing_ok=True)
    return updated


def numeric_inputs(claim):
    """Use reviewer-supplied scope, with extra gates for common ambiguous claims."""
    reasons = list(claim['review_reasons'])
    if claim['claim_type'] != 'numeric' or claim.get('operation') not in OPERATIONS:
        reasons.append('Requires a single historical numeric claim and a calculation type')
    if re.search(r'\b(segment|division|geograph\w*|quarter\w*|Q[1-4]|half.year|calendar.year|'
                 r'not|never|neither|at least|at most|more than|less than|over|under|between)\b', claim['quote'], re.I):
        reasons.append('Review nonannual scope, negation or a range')
    period = re.fullmatch(r'FY\s*((?:19|20)\d{2}|2100)', claim['period'], re.I)
    quoted_years = set(re.findall(r'\b(?:FY\s*|fiscal year\s+)((?:19|20)\d{2}|2100)\b', claim['quote'], re.I))
    if not period:
        reasons.append('Use a fiscal year such as FY2024')
    elif quoted_years and (period[1] not in quoted_years or (len(quoted_years) > 1 and claim.get('operation') != 'growth')):
        reasons.append('Review the fiscal years in the quotation')
    value = claim['value']
    if not value or len(value) > 64 or not NUMBER.fullmatch(value) or not re.search(
            r'(?<![\w.,+−-])(?<![-−+]\$)' + re.escape(value) + r'(?!\d|[.,]\d)', claim['quote']):
        reasons.append('Use the complete numeric literal from the quote')
    if claim['unit'] not in UNITS:
        reasons.append('Choose the unit used in the quote')
    company = normalize_company(claim['company'])
    if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}', company):
        reasons.append('Use the company ticker')
    if reasons:
        raise ValueError('; '.join(reasons))
    operation = claim['operation']
    measure = claim['metric']
    if operation == 'ratio':
        if measure not in RATIOS:
            raise ValueError('Choose a cached ratio')
    else:
        measure = normalize_metric(measure)
    return dict(company=company, year=int(period[1]), operation=operation, measure=measure,
                claimed_value=value, unit=claim['unit'])


def check_reviewed_memo(root, directory, memo, bundle, approved):
    source = directory/'source.pdf'
    if digest(source) != memo['source_sha256'] or bundle['memo_sha256'] != memo['source_sha256']:
        raise ValueError('The source PDF changed. Upload it again.')
    version = snapshot(root)
    facts = FactStore(facts_file(root))
    rows = []
    calls = 0
    for item in bundle['claims']:
        if item['claim_id'] not in approved:
            continue
        claim = dict(item, approved=True)
        row = dict(claim=claim, status='needs_review')
        try:
            checked = validate_claim({key: claim[key] for key in CLAIM_FIELDS}, memo['pages'][claim['page'] - 1])
            if any(checked[key] != claim[key] for key in ('claim_id', 'page', 'start', 'end')):
                raise ValueError('The saved quotation location changed')
            inputs = numeric_inputs(dict(checked, operation=claim.get('operation')))
            calls += 1
            result = calculate(facts, **inputs)
            row.update(status='complete', result=result)
        except ValueError as error:
            row['reason'] = str(error)
        rows.append(row)
    if snapshot(root) != version:
        raise ValueError('Cached facts changed during this check. Try again.')
    warnings = [dict(page=page['page'], warnings=page['warnings']) for page in memo['pages'] if page['warnings']]
    pending_ids = [item['claim_id'] for item in bundle['claims'] if item['claim_id'] not in approved]
    complete = all(row['status'] == 'complete' for row in rows) and not (warnings or bundle['issues'] or pending_ids)
    result = dict(status='complete' if complete else 'needs_review', system='manual_offline',
                  memo_sha256=memo['source_sha256'], claims_sha256=digest(directory/'claims.json'),
                  claim_revision=bundle.get('revision', 0), rows=rows, unselected_claim_ids=pending_ids,
                  verification_model_calls=0, verification_tool_calls=calls, network_requests=0,
                  extraction_issues=bundle['issues'], page_warnings=warnings, **version,
                  note='Calculations use the annual company-wide scope supplied and confirmed by the reviewer.')
    target = artifact_dir(root, 'memo-check')
    result['pdf_annotations'] = annotate_pdf(source, target, result)
    render_report(target, memo, result, directory)
    write_json(target/'report.json', result)
    return target, result
