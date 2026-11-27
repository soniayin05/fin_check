"""Common baseline/agent development runner. Gold labels stay inside the scorer."""

from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import subprocess
from time import perf_counter

from .agent import save_run
from .stats import paired_bootstrap, p95, wilson
from .tools import normalize_company

VERDICTS = {'supported', 'contradicted', 'insufficient_evidence'}
NUMBERS = {'verified_value', 'left_growth_pct', 'right_growth_pct'}


def load_cases(path):
    rows = [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]
    seen = set()
    for row in rows:
        if not isinstance(row.get('id'), str) or row['id'] in seen:
            raise ValueError('Case IDs must be unique strings')
        seen.add(row['id'])
        if not isinstance(row.get('claim'), str) or not 1 <= len(row['claim'].strip()) <= 6000:
            raise ValueError('Each case needs a claim of 1-6000 characters')
        gold = row.get('expected', {})
        allowed = {'verdict', 'company', 'fiscal_year', 'verified_unit', 'tolerance',
                   'required_source_ids', 'evidence_groups'} | NUMBERS
        if not isinstance(gold, dict) or gold.get('verdict') not in VERDICTS or set(gold)-allowed:
            raise ValueError('Invalid expected verdict or unknown scoring field')
        for key in NUMBERS & set(gold):
            if type(gold[key]) not in (int, float) or not isfinite(gold[key]):
                raise ValueError('Expected numbers must be finite')
        tolerance = gold.get('tolerance', .01)
        if type(tolerance) not in (int, float) or not isfinite(tolerance) or tolerance < 0:
            raise ValueError('Expected tolerance must be finite and nonnegative')
        if 'fiscal_year' in gold and type(gold['fiscal_year']) is not int:
            raise ValueError('Expected fiscal_year must be an integer')
        for key in ('company', 'verified_unit'):
            if key in gold and (not isinstance(gold[key], str) or not gold[key].strip()):
                raise ValueError(f'Expected {key} must be a nonempty string')
        if not isinstance(gold.get('required_source_ids', []), list) or any(
                not isinstance(x, str) for x in gold.get('required_source_ids', [])):
            raise ValueError('required_source_ids must be a list of strings')
        groups = gold.get('evidence_groups', [])
        if not isinstance(groups, list) or any(not isinstance(g, list) or not g or
                any(not isinstance(pid, str) for pid in g) for g in groups):
            raise ValueError('Each evidence group must contain alternative acceptable IDs')
        if 'verified_value' in gold and 'verified_unit' not in gold:
            raise ValueError('Numeric equality gold needs verified_unit')
        if gold['verdict'] != 'insufficient_evidence' and not (
                gold.get('required_source_ids') or groups):
            raise ValueError('Supported/contradicted gold needs expected evidence')
    if not rows:
        raise ValueError('No evaluation cases supplied')
    return rows


def score_run(run, gold):
    """Score completion, verdict, numbers, scope and acceptable supporting evidence."""
    failures = []
    result = run.get('result') or {}
    if run.get('status') != 'complete':
        return {'passed': False, 'failures': ['execution_'+str(run.get('status', 'missing'))]}
    if result.get('verdict') != gold['verdict']:
        failures.append('verdict')
    for key in NUMBERS & set(gold):
        value = result.get(key)
        if type(value) not in (int, float) or not isfinite(value) or abs(value-gold[key]) > gold.get('tolerance', .01):
            failures.append(key)
    if 'verified_unit' in gold and result.get('verified_unit') != gold['verified_unit']:
        failures.append('unit')
    citations = set(result.get('evidence_ids', []))
    sources = result.get('sources', {})
    observed = {r['id']: r for r in result.get('observations', []) if isinstance(r, dict) and 'id' in r}
    # Only count a source if cited directly or via a returned cited observation.
    cited_sources = citations & set(sources)
    cited_sources.update(observed[k].get('source_id') for k in citations & set(observed))
    if citations - (set(sources) | set(observed)):
        failures.append('unknown_citation')
    if set(gold.get('required_source_ids', []))-cited_sources:
        failures.append('required_sources')
    for group in gold.get('evidence_groups', []):
        if not set(group) & cited_sources:
            failures.append('supporting_evidence')
            break
    calc = result.get('calculation') or {}
    for key in ('company', 'fiscal_year'):
        if key not in gold:
            continue
        wanted = normalize_company(gold[key]) if key == 'company' else gold[key]
        actual = calc.get(key)
        if key == 'company' and isinstance(actual, str):
            actual = normalize_company(actual)
        if actual is not None:
            ok = actual == wanted
        elif gold['verdict'] == 'insufficient_evidence':
            # No scoped calculation is not proof the right entity/period was checked.
            ok = False
        else:
            values = [sources[k].get('company' if key == 'company' else 'year')
                      for k in cited_sources if k in sources]
            if key == 'company':
                values = [normalize_company(v) if isinstance(v, str) else v for v in values]
            ok = bool(values) and all(v == wanted for v in values)
        if not ok:
            failures.append(key)
    return {'passed': not failures, 'failures': failures}


def usage_summary(run, pricing=None):
    models = [e for e in run.get('events', []) if e.get('kind') == 'model']
    usage = [(e.get('output') or {}).get('usage') for e in models]
    complete = bool(models) and all(isinstance(u, dict) and e.get('status') == 'ok'
                                   and (e.get('output') or {}).get('retries', 0) == 0
                                   for e, u in zip(models, usage))
    fields = {'input_tokens': 'promptTokenCount', 'output_tokens': 'candidatesTokenCount',
              'total_tokens': 'totalTokenCount'}
    totals = {name: sum((u or {}).get(field, 0) for u in usage) for name, field in fields.items()}
    cost_complete = complete and all('promptTokenCount' in u and 'candidatesTokenCount' in u
                                    and 'totalTokenCount' in u and not u.get('thoughtsTokenCount')
                                    and not u.get('cachedContentTokenCount') for u in usage)
    estimate = None
    if pricing and cost_complete:
        estimate = (totals['input_tokens']*pricing['input_per_million'] +
                    totals['output_tokens']*pricing['output_per_million']) / 1_000_000
    return dict(totals, usage_complete=complete, model_calls=len(models),
                retries=sum((e.get('output') or {}).get('retries', 0) for e in models),
                estimated_cost_usd=estimate, actual_spend_usd=None,
                cost_note='Estimate unavailable for missing usage, retries, cached/thinking tokens, or absent pricing. Billing is not measured.')


def summarize(rows):
    primary = [r for r in rows if r['repeat'] == 0]
    names=sorted({r['system'] for r in rows})
    summary = {'systems': {}, 'primary_cases': len({r['case_id'] for r in primary}),
               'additional_runs': len(rows)-len(primary), 'failure_categories': {}}
    for system in names:
        selected = [r for r in primary if r['system'] == system]
        n, passed = len(selected), sum(r['score']['passed'] for r in selected)
        latency = [r['latency_seconds'] for r in selected]
        costs = [r['usage']['estimated_cost_usd'] for r in selected]
        summary['systems'][system] = dict(passed=passed, total=n, accuracy=passed/n if n else None,
            wilson_95=wilson(passed, n), latency_mean_seconds=sum(latency)/n if n else None,
            latency_p95_seconds=p95(latency),
            estimated_cost_usd=sum(costs) if costs and all(c is not None for c in costs) else None,
            model_calls=sum(r['usage']['model_calls'] for r in selected),
            tool_calls=sum(r['tool_calls'] for r in selected),
            execution_errors=sum(r['status']=='error' for r in selected),
            incomplete=sum(r['status']=='incomplete' for r in selected))
        if any('fault_injected' in r for r in selected):
            summary['systems'][system].update(fault_injected=sum(r.get('fault_injected',False) for r in selected),
                fault_recovered=sum(r.get('fault_recovered',False) for r in selected),
                not_injected=sum(not r.get('fault_injected',False) for r in selected))
        failures = {}
        for r in selected:
            for category in r['score']['failures']:
                failures[category] = failures.get(category, 0)+1
        summary['failure_categories'][system] = failures
    paired = {r['case_id']: {} for r in primary}
    for r in primary:
        paired[r['case_id']][r['system']] = int(r['score']['passed'])
    pairs = [p for p in paired.values() if 'agent' in p and 'baseline' in p]
    summary['agent_minus_baseline'] = paired_bootstrap([p['agent'] for p in pairs], [p['baseline'] for p in pairs]) if pairs else None
    summary['paired_comparisons']={}
    for i,a in enumerate(names):
        for b in names[i+1:]:
            paired_rows=[p for p in paired.values() if a in p and b in p]
            if paired_rows:summary['paired_comparisons'][a+'_minus_'+b]=paired_bootstrap([p[a] for p in paired_rows],[p[b] for p in paired_rows])
    summary['repeated_cases'] = {}
    for r in rows:
        key = r['case_id']+':'+r['system']
        bucket = summary['repeated_cases'].setdefault(key, [])
        bucket.append({'repeat': r['repeat'], 'passed': r['score']['passed'],
                       'status': r['status'], 'latency_seconds': r['latency_seconds']})
    summary['repeated_cases'] = {k:v for k,v in summary['repeated_cases'].items() if len(v)>1}
    return summary


def redact(text):
    for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        if os.getenv(key):
            text = text.replace(os.environ[key], '[REDACTED]')
    return text


def run_comparison(root, cases, systems, facts_path, cases_path, config, repeats=1, repeat_ids=()):
    if len(systems)<2:
        raise ValueError('At least two systems are required')
    if repeats < 1 or set(repeat_ids)-{c['id'] for c in cases}:
        raise ValueError('Invalid repeat count or unknown repeated case ID')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = Path(root)/'artifacts/local'/('comparison-'+run_id)
    target.mkdir(parents=True)
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    commit = subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True)
    meta = dict(config, run_id=run_id, split='dev', git_commit=commit.stdout.strip(),
                facts_sha256=sha(Path(facts_path)), cases_sha256=sha(Path(cases_path)),
                index_sha256=sha(Path(root)/'data/indexes/passages.json'),
                repeats=repeats, repeat_ids=list(repeat_ids),
                code_sha256={p.name:sha(p) for p in sorted((Path(root)/'fincheck').glob('*.py'))},
                measurement_boundary='System run includes model/tool calls, retries and query embedding; excludes initial data loading and trace serialization. Caches may be warm. Orders alternate by case/repeat.',
                budget_note='Same configured ceilings; baseline has a fixed two-model/two-tool pipeline. Agent may spend more within the ceilings.')
    (target/'metadata.json').write_text(redact(json.dumps(meta, indent=2))+'\n')
    rows=[]
    scheduled=len(systems)*(len(cases)+(repeats-1)*len(repeat_ids))
    consecutive_quota_failures=0
    def checkpoint(status,**extra):
        summary=summarize(rows)
        summary.update(run_status=status,scheduled_runs=scheduled,completed_runs=len(rows),**extra)
        (target/'summary.json').write_text(redact(json.dumps(summary,indent=2,allow_nan=False))+'\n')
        return summary
    for repeat in range(repeats):
        for i, case in enumerate(cases):
            if repeat and case['id'] not in repeat_ids:
                continue
            names=list(systems)
            offset=(i+repeat)%len(names)
            order=names[offset:]+names[:offset]
            for name in order:
                tick=perf_counter()
                try:
                    run=systems[name].run(case['claim'])  # never pass input hints or expected labels
                except KeyboardInterrupt:
                    return target,checkpoint('interrupted',interrupted_run=dict(case_id=case['id'],system=name,repeat=repeat))
                except Exception as error:
                    run=dict(claim=case['claim'],status='error',result={'error':type(error).__name__},
                             events=[],tool_calls=0,latency_seconds=perf_counter()-tick,
                             model=getattr(systems[name].model,'model',None))
                trace=save_run(Path(root),run,Path(facts_path))
                row=dict(case_id=case['id'],system=name,repeat=repeat,status=run['status'],
                         score=score_run(run,case['expected']),trace=str(trace),
                         latency_seconds=run['latency_seconds'],tool_calls=run['tool_calls'],
                         usage=usage_summary(run,config.get('pricing')))
                injected=run.get('fault_injections',[])
                if run.get('fault_requested'):
                    row.update(fault_injected=bool(injected),fault_recovered=bool(injected) and run['status']=='complete',
                               fault_task_success=bool(injected) and row['score']['passed'])
                rows.append(row)
                with (target/'results.jsonl').open('a') as file:
                    file.write(redact(json.dumps(row,allow_nan=False))+'\n')
                print(f"{name} {case['id']} repeat={repeat}: {run['status']} {'PASS' if row['score']['passed'] else 'FAIL'}",flush=True)
                errors=[str((run.get('result') or {}).get('error',''))]+[str(e.get('error','')) for e in run.get('events',[]) if e.get('kind')=='model']
                quota=any('HTTP 429' in message for message in errors)
                consecutive_quota_failures=consecutive_quota_failures+1 if quota else 0
                checkpoint('running')
                if consecutive_quota_failures>=3:
                    print('Stopped after three consecutive quota failures; remaining runs were not executed.',flush=True)
                    return target,checkpoint('quota_stopped')
    return target, checkpoint('complete')


def comparison_command(args, parser, root, facts, passages):
    from .agent import Agent
    from .baseline import FixedBaseline
    from .model import Gemini, ModelError
    from .workflow import Workflow
    try:
        cases=load_cases(args.cases)
        if args.only:
            ids=set(args.only.split(','))
            if ids-{c['id'] for c in cases}:
                raise ValueError('Unknown --only case IDs')
            cases=[c for c in cases if c['id'] in ids]
        repeats=args.repeats
        repeat_ids=args.repeat_ids.split(',') if args.repeat_ids else []
        if not 1 <= repeats <= 10 or set(repeat_ids)-{c['id'] for c in cases}:
            raise ValueError('Use 1-10 repeats and existing selected case IDs')
        if not 1 <= args.max_calls <= 100 or not 1 <= args.max_turns <= 100:
            raise ValueError('Budgets must be 1-100')
        prices=(args.input_price_per_million,args.output_price_per_million)
        pricing=None
        if any(v is not None for v in prices):
            if any(v is None or not isfinite(v) or v < 0 for v in prices) or not args.pricing_date:
                raise ValueError('Supply both nonnegative token prices and --pricing-date')
            pricing=dict(input_per_million=prices[0],output_per_million=prices[1],
                         date=args.pricing_date,provider='Google Gemini; user-supplied list prices')
        source_ids=set(facts.sources)|set(passages.by_id)
        for case in cases:
            expected=case['expected']
            if set(expected.get('required_source_ids',[]))-source_ids:
                raise ValueError(f"{case['id']}: required sources absent from loaded snapshot")
            if any(not set(group)&source_ids for group in expected.get('evidence_groups',[])):
                raise ValueError(f"{case['id']}: acceptable evidence absent; restore the frozen corpus or review gold labels")
        config=dict(max_calls=args.max_calls,max_turns=args.max_turns,retriever=passages.default_method,
                    temperature=0,pricing=pricing,case_ids=[c['id'] for c in cases])
        selected=args.systems.split(',')
        if len(set(selected))!=len(selected) or len(selected)<2 or set(selected)-{'baseline','agent','multi','multi_prefetch'}:
            raise ValueError('Choose distinct systems: baseline,agent,multi,multi_prefetch')
        config['systems']=selected
        if args.dry_run:
            print(json.dumps(dict(config,cases=len(cases),scheduled_runs=len(selected)*(len(cases)+(repeats-1)*len(repeat_ids)),
                                  model_calls_made=0,facts=str(args.facts),cases_path=str(args.cases)),indent=2))
            return 0
        model=Gemini()
        config['model']=model.model
        config['request_interval_seconds']=model.min_interval
        implementations={'baseline':FixedBaseline,'agent':Agent,'multi':Workflow,
                         'multi_prefetch':lambda *a:Workflow(*a,prefetch_fact=True)}
        systems={name:implementations[name](facts,passages,model,args.max_calls,args.max_turns) for name in selected}
        target,summary=run_comparison(root,cases,systems,args.facts,args.cases,config,repeats,repeat_ids)
        print(json.dumps(summary,indent=2))
        print(f'Comparison saved: {target}')
        return 0 if summary['run_status']=='complete' else 2  # factual failures remain valid completed results
    except (ValueError, OSError, ModelError) as error:
        parser.error(str(error))
