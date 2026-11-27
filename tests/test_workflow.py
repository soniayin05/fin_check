"""Capability boundaries, independent reads, repairs and fault recovery protocols."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from fincheck.workflow import Workflow,CAPABILITIES
from fincheck.faults import FaultInjector
from fincheck.model import ModelError
from fincheck.tools import FactStore
from fincheck.retrieval import PassageStore

ROOT=Path(__file__).resolve().parent.parent


def call(name,**args):return dict(name=name,args=args)
PLAN=call('set_plan',plan='Verify the original fiscal-year figures, then review source evidence.')
APPROVE=call('record_review',approved=True,issues=[])


class Script:
    model='test-only-scoped-model'
    def __init__(self,items):self.items=iter(items);self.requests=[]
    def generate(self,messages,tools,instruction):
        self.requests.append((deepcopy(messages),deepcopy(tools)))
        calls=next(self.items)
        if isinstance(calls,Exception):raise calls
        if isinstance(calls,dict):calls=[calls]
        return {'content':{'role':'model','parts':[{'functionCall':c} for c in calls]},'usage':None}


def protocol(company='MSFT',metric='revenue',value='245.1',unit='USD_billions',year=2024):
    facts=FactStore(ROOT/'data/facts/observations.json')
    o=facts.get_financial_fact(company,metric,year)
    calc=call('calculate',company=company,year=year,operation='fact',measure=metric,claimed_value=value,unit=unit)
    draft=call('propose_verdict',verdict='supported',explanation='PRIVATE_EVIDENCE_RATIONALE',
               evidence_ids=[o.source_id],calc_id='calc-1')
    reads=[call('read_evidence',source_id=o.source_id),call('read_evidence',source_id=o.id)]
    return facts,calc,draft,reads


class WorkflowTests(unittest.TestCase):
    def run_flow(self,steps,**kwargs):
        f,_,_,_=protocol()
        model=Script(steps)
        flow=Workflow(f,PassageStore(ROOT/'none.json'),model,**kwargs)
        return flow.run('Microsoft revenue was $245.1B in FY2024.'),model

    def test_permissions_and_fresh_review(self):
        _,calc,draft,reads=protocol()
        run,model=self.run_flow([PLAN,calc,draft,reads,APPROVE])
        self.assertEqual(run['status'],'complete')
        self.assertEqual(run['tool_calls'],6)
        self.assertEqual(run['result']['verified_value'],245.122)
        for _,tools in model.requests:
            names={t['name'] for t in tools}
            self.assertTrue(any(names<=allowed for allowed in CAPABILITIES.values()))
        self.assertNotIn('PRIVATE_EVIDENCE_RATIONALE',json.dumps(model.requests[3][0]))
        self.assertEqual(set(run['result']['review']['original_evidence_read']),{r['args']['source_id'] for r in reads})

    def test_denied_tools_consume_budget(self):
        _,calc,_,_=protocol()
        run,_=self.run_flow([calc],max_turns=1)
        self.assertEqual(run['status'],'incomplete')
        self.assertEqual(run['tool_calls'],1)
        self.assertIn('Permission denied',run['events'][1]['output']['error'])
        self.assertNotIn('verdict',run['result'])

    def test_cannot_approve_without_reading(self):
        _,calc,draft,reads=protocol()
        run,_=self.run_flow([PLAN,calc,draft,APPROVE,reads,APPROVE])
        self.assertEqual(run['status'],'complete')
        self.assertTrue(any('Read original' in e.get('output',{}).get('error','') for e in run['events']))

    def test_review_cannot_calculate_or_finalize(self):
        _,calc,draft,reads=protocol()
        illegal=call('submit_verdict',verdict='supported',explanation='Bypass',evidence_ids=[])
        run,_=self.run_flow([PLAN,calc,draft,calc,illegal,reads,APPROVE])
        self.assertEqual(run['status'],'complete')
        self.assertEqual(sum(e.get('scope')=='review' and e.get('status')=='error' for e in run['events']),2)

    def test_repair_handoff_and_limit(self):
        _,calc,draft,reads=protocol()
        reject=call('record_review',approved=False,issues=['Check the fiscal period again.'])
        fixed=deepcopy(draft);fixed['args']['calc_id']='calc-2'
        run,_=self.run_flow([PLAN,calc,draft,reads,reject,PLAN,calc,fixed,reads,APPROVE],max_turns=12)
        self.assertEqual(run['status'],'complete')
        self.assertEqual(run['result']['review']['repairs'],1)
        self.assertEqual(run['tool_calls'],12)
        steps=[]
        for n in range(1,4):
            d=deepcopy(draft);d['args']['calc_id']='calc-'+str(n)
            steps.extend([PLAN,calc,d,reads,reject])
        run,_=self.run_flow(steps,max_calls=40,max_turns=30)
        self.assertEqual(run['status'],'incomplete')
        self.assertEqual(run['workflow_state']['repair_count'],2)
        self.assertNotIn('verdict',run['result'])

    def test_worker_failure_is_incomplete_not_draft_verdict(self):
        _,calc,draft,_=protocol()
        run,_=self.run_flow([PLAN,calc,draft,ModelError('quota')])
        self.assertEqual(run['status'],'incomplete')
        self.assertIn('review model failed',run['result']['error'])
        self.assertNotIn('verdict',run['result'])

    def test_shared_budget_stops_before_review_approval(self):
        _,calc,draft,reads=protocol()
        run,_=self.run_flow([PLAN,calc,draft,reads,APPROVE],max_calls=5)
        self.assertEqual(run['status'],'incomplete')
        self.assertEqual(run['tool_calls'],5)

    def test_all_fifteen_fault_protocols(self):
        cases=[json.loads(l) for l in (ROOT/'evals/faults/cases.jsonl').read_text().splitlines()]
        self.assertEqual(len(cases),15)
        for case in cases:
            with self.subTest(case=case['id']):
                gold=case['expected'];company=gold['company'];year=gold['fiscal_year']
                metric=next(k for k in ['operating_cash_flow','total_assets','net_income','eps_diluted','revenue']
                            if {'operating_cash_flow':'cash flow','total_assets':'assets','net_income':'net income','eps_diluted':'EPS','revenue':'revenue'}[k] in case['claim'])
                from fincheck.calculations import NUMBER
                literal=NUMBER.findall(case['claim'])[0]
                facts,calc,draft,reads=protocol(company,metric,literal,gold['verified_unit'],year)
                fault=case['fault'];steps=[PLAN,calc]
                if fault['kind']=='bad_data':steps.extend([PLAN,calc])
                steps.extend([draft,reads,APPROVE])
                flow=Workflow(facts,PassageStore(ROOT/'none'),Script(steps),max_turns=12,
                              fault=FaultInjector(fault['kind'],fault.get('variant','unit')),retry_wait=0)
                run=flow.run(case['claim'])
                self.assertEqual(run['status'],'complete')
                self.assertTrue(run['fault_injections'])
                flow=Workflow(facts,PassageStore(ROOT/'none'),Script([calc]),require_plan=False,
                              fault=FaultInjector(fault['kind'],fault.get('variant','unit')),retry_wait=0)
                run=flow.run(case['claim'])
                self.assertEqual(run['status'],'incomplete')
                self.assertNotIn('verdict',run['result'])


if __name__=='__main__':unittest.main()
