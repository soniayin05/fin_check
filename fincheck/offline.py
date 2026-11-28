"""Cached numeric checks and saved-report discovery, with no model or network calls."""

from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
import re
from time import perf_counter

from .calculations import UNITS, calculate
from .facts import METRICS
from .memo import artifact_dir, write_json
from .ratios import RATIOS
from .tools import FactStore, normalize_company, normalize_metric

ARTIFACT = re.compile(r'(?:offline-check|memo-check)-\d{8}T\d{12}Z')
FIELDS = {'company','year','operation','measure','claimed_value','unit'}


def facts_file(root):
    root=Path(root);path=root/'data/facts/observations.json'
    return path if path.exists() else root/'data/sample/financial_facts.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def snapshot(root):
    path=facts_file(root)
    return dict(facts_sha256=digest(path),coverage_sha256=digest(path.with_name('coverage.json'))
                if path.name=='observations.json' else None)


def catalog(root):
    store=FactStore(facts_file(root));companies=[]
    for company in sorted({o.company for o in store.observations}):
        companies.append(dict(ticker=company,years=sorted({o.fiscal_year for o in store.observations if o.company==company})))
    return dict(mode='offline_numeric',model_calls=0,companies=companies,
        observations=len(store.observations),
        metrics=[dict(name=name,unit=value[1]) for name,value in METRICS.items()],
        ratios=[dict(name=name,label=value[1],unit=value[2]) for name,value in RATIOS.items()],
        units=list(UNITS),**snapshot(root))


def numeric_check(root,payload):
    if not isinstance(payload,dict) or set(payload)!=FIELDS:raise ValueError('Supply exactly the six numeric-check fields')
    if any(not isinstance(payload[key],str) for key in FIELDS-{'year'}):raise ValueError('Numeric-check fields must be strings except year')
    if type(payload['year']) is not int or not 1900<=payload['year']<=2100:raise ValueError('Fiscal year must be an integer from 1900 to 2100')
    company=normalize_company(payload['company'])
    if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,11}',company):raise ValueError('Enter a company ticker, for example MSFT')
    operation=payload['operation'];measure=payload['measure']
    if operation in {'fact','growth'}:measure=normalize_metric(measure)
    elif operation!='ratio' or measure not in RATIOS:raise ValueError('Choose a supported operation and measure')
    inputs=dict(payload,company=company,measure=measure)
    path=facts_file(root);version=snapshot(root);start=perf_counter()
    result=calculate(FactStore(path),**dict(company=company,year=inputs['year'],operation=operation,
        measure=measure,claimed_value=inputs['claimed_value'],unit=inputs['unit']))
    if snapshot(root)!=version:raise ValueError('Cached data changed during the check; retry with the current snapshot')
    # A report describes only the structured fields entered by the user. It
    # does not claim natural-language interpretation or independent LLM review.
    report=dict(kind='fincheck-offline-numeric',schema_version=1,mode='offline_numeric',
        status='complete',created_at=datetime.now(timezone.utc).isoformat(),input=inputs,result=result,
        model_calls=0,network_requests=0,tool_calls=1,latency_seconds=perf_counter()-start,
        code_sha256={name:digest(Path(__file__).with_name(name)) for name in ('offline.py','calculations.py','ratios.py','tools.py','facts.py')},
        **version,note='Deterministic check of user-entered annual company-wide fields against the loaded snapshot. No AI parsing or review.')
    target=artifact_dir(Path(root),'offline-check')
    render_numeric(target,report)
    write_json(target/'report.json',report)
    return target,report


def render_numeric(target,report):
    inputs,result=report['input'],report['result']
    verdict=result['verdict']
    checked=(f"Verified {result['verified_value']:g} {result['verified_unit']}; "
             f"claimed {inputs['claimed_value']} {inputs['unit']}.") if 'verified_value' in result else result.get('reason','Evidence is incomplete in the loaded snapshot.')
    fields=''.join('<dt>'+escape(k)+'</dt><dd>'+escape(str(v))+'</dd>' for k,v in inputs.items())
    sources=[]
    for key,source in result.get('sources',{}).items():
        label=escape(key);url=source.get('url','')
        if isinstance(url,str) and url.startswith('https://'):
            label='<a href="'+escape(url,quote=True)+'" rel="noopener noreferrer" target="_blank">'+label+'</a>'
        sources.append('<li>'+label+'<pre>'+escape(json.dumps(source,indent=2))+'</pre></li>')
    details=escape(json.dumps(dict(formula=result.get('formula'),assumptions=result.get('assumptions',[]),
        observations=result.get('observations',[])),indent=2))
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinCheck offline numeric check</title><style>body{font:16px system-ui;max-width:900px;margin:40px auto;padding:0 24px;color:#182537}section{border:1px solid #cbd5e1;border-radius:10px;padding:20px;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}dt{font-weight:600}dd{margin:4px 0 12px}a{color:#155ac0}</style>
<h1>Offline numeric check</h1><p>Saved result · Cached evidence · Zero model calls</p>'''
    html+='<section><h2>'+escape(verdict.replace('_',' ').capitalize())+'</h2><p>'+escape(checked)+'</p><dl>'+fields+'</dl></section>'
    html+='<section><h2>Calculation and observations</h2><pre>'+details+'</pre></section>'
    html+='<section><h2>Sources</h2><ul>'+''.join(sources)+'</ul></section>'
    html+='<p>'+escape(report['note'])+'</p><p>Created: '+escape(report['created_at'])+'</p><p>Facts SHA-256: '+escape(report['facts_sha256'])+'</p><p><a href="report.json">Download JSON</a></p></html>'
    (target/'report.html').write_text(html)


def read_report(root,identifier):
    if not isinstance(identifier,str) or not ARTIFACT.fullmatch(identifier):raise ValueError('Unknown saved report')
    base=(Path(root)/'artifacts/local').resolve();folder=(base/identifier).resolve()
    if folder.parent!=base or folder.name!=identifier:raise ValueError('Saved report is outside the artifact directory')
    path=folder/'report.json'
    html=folder/'report.html'
    if (not path.is_file() or not html.is_file() or not path.resolve().is_relative_to(folder)
            or not html.resolve().is_relative_to(folder) or path.stat().st_size>10*1024*1024
            or html.stat().st_size>20*1024*1024):
        raise ValueError('Saved report unavailable')
    report=json.loads(path.read_text())
    if not isinstance(report,dict):raise ValueError('Invalid saved report')
    if identifier.startswith('offline-check-'):
        if report.get('kind')!='fincheck-offline-numeric' or not isinstance(report.get('input'),dict) or not isinstance(report.get('result'),dict):
            raise ValueError('Invalid offline report')
    elif not isinstance(report.get('rows'),list) or not re.fullmatch('[a-f0-9]{64}',str(report.get('memo_sha256',''))):
        raise ValueError('Invalid memo report')
    return folder,report


def history(root):
    base=Path(root)/'artifacts/local';current=snapshot(root);reports=[]
    paths=sorted(base.glob('*/report.json'),key=lambda p:p.parent.name.rsplit('-',1)[-1],reverse=True)
    for path in paths:
        try:folder,report=read_report(root,path.parent.name)
        except (ValueError,OSError,TypeError):continue
        offline=report.get('kind')=='fincheck-offline-numeric'
        fields=report.get('input',{})
        measure=str(fields.get('measure','')).replace('_',' ')
        created=report.get('created_at')
        if not created:
            try:created=datetime.strptime(folder.name.rsplit('-',1)[-1],'%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc).isoformat()
            except ValueError:created='Unknown creation time'
        match=None
        if current['facts_sha256'] and report.get('facts_sha256'):
            match=report['facts_sha256']==current['facts_sha256']
            if 'coverage_sha256' in report:match=match and report['coverage_sha256']==current['coverage_sha256']
        reports.append(dict(id=folder.name,mode='offline_numeric' if offline else report.get('system','memo'),
            title=f"{fields.get('company')} FY{fields.get('year')} {measure}" if offline else f"Memo verification ({len(report['rows'])} claims)",
            status=report['result'].get('verdict') if offline else report.get('status'),
            created_at=created,snapshot_matches_current=match))
        if len(reports)==100:break
    return reports


def history_directories(root,identifier):
    folder,report=read_report(root,identifier);allowed=[folder]
    if identifier.startswith('memo-check-'):
        # Only original document folders explicitly linked by this report and
        # matching its PDF hash can accompany it. Never serve arbitrary paths.
        for name in set(re.findall(r'\.\./([A-Za-z0-9_-]+)/pages/page-\d+\.png', (folder/'report.html').read_text())):
            source=(folder.parent/name).resolve()
            if source.parent!=folder.parent:continue
            try:memo=json.loads((source/'memo.json').read_text())
            except (OSError,ValueError):continue
            if isinstance(memo,dict) and memo.get('source_sha256')==report['memo_sha256']:allowed.append(source)
    return folder,report,allowed
