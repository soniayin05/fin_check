"""Offline protocol and scoring tests; these are not live accuracy measurements."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fincheck.agent import Agent
from fincheck.baseline import FixedBaseline
from fincheck.evaluation import load_cases, run_comparison, score_run, summarize, usage_summary
from fincheck.model import ModelError
from fincheck.retrieval import PassageStore
from fincheck.tools import FactStore
from test_agent import ScriptedModel, COMPARE, FINAL

ROOT=Path(__file__).resolve().parent.parent
FACTS=ROOT/'data/sample/financial_facts.json'


class BaselineTests(unittest.TestCase):
    def baseline(self,calls,**options):
        return FixedBaseline(FactStore(FACTS),PassageStore(ROOT/'tests/missing.json'),ScriptedModel(calls),**options)

    def test_fixed_numeric_path_and_signature(self):
        b=self.baseline([COMPARE,FINAL])
        run=b.run('Microsoft revenue grew faster than operating cash flow in FY2024.')
        self.assertEqual(run['status'],'complete')
        self.assertEqual(run['tool_calls'],2)
        self.assertAlmostEqual(run['result']['left_growth_pct'],15.66996,places=4)
        self.assertEqual(b.model.requests[1][1]['parts'][0]['thoughtSignature'],'test-signature')

    def test_no_adaptive_search_or_unknown_citations(self):
        bad={'name':'submit_verdict','args':dict(FINAL['args'],evidence_ids=['fake'])}
        self.assertEqual(self.baseline([COMPARE,bad]).run('Check Microsoft FY2024.')['status'],'error')
        repeated={'name':'search_filings','args':{'company':'MSFT','year':2024,'query':'Azure'}}
        run=self.baseline([repeated,repeated]).run('Check Azure.')
        self.assertEqual(run['status'],'error')
        self.assertNotIn('verdict',run['result'])

    def test_numeric_literal_cannot_change(self):
        call={'name':'calculate','args':dict(company='MSFT',year=2024,operation='fact',measure='revenue',claimed_value='245.1',unit='USD_billions')}
        run=self.baseline([call]).run('Microsoft revenue was $250B in FY2024.')
        self.assertEqual(run['status'],'error')

    def test_final_schema_omits_uncomputed_growth_fields(self):
        b=self.baseline([COMPARE,FINAL])
        seen=[]
        generate=b.model.generate
        def capture(messages,tools,instruction):
            seen.append(tools)
            return generate(messages,tools,instruction)
        b.model.generate=capture
        b.run('Microsoft revenue grew faster than operating cash flow in FY2024.')
        self.assertNotIn('left_growth_pct',seen[1][0]['parameters']['properties'])
        self.assertNotIn('right_growth_pct',seen[1][0]['parameters']['properties'])

    def test_failure_vs_budget_exhaustion(self):
        self.assertEqual(self.baseline([ModelError('quota')]).run('Check this')['status'],'error')
        for option in ({'max_turns':1},{'max_calls':1}):
            run=self.baseline([COMPARE,FINAL],**option).run('Check Microsoft FY2024.')
            self.assertEqual(run['status'],'incomplete')
            self.assertNotIn('verdict',run['result'])

    def test_disclosure_uses_evidence_and_preserves_scope(self):
        search={'name':'search_filings','args':dict(company='MSFT',year=2024,query='Azure growth')}
        final={'name':'submit_verdict','args':dict(verdict='supported',explanation='Source states 30%.',evidence_ids=['p'])}
        b=self.baseline([search,final])
        generate=b.model.generate
        declarations=[]
        def capture(messages,tools,instruction):
            declarations.append(tools)
            return generate(messages,tools,instruction)
        b.model.generate=capture
        b.passages.passages=[dict(id='p',company='MSFT',year=2024,source_type='10-K',text='Azure growth was 30%.')]
        run=b.run('Azure growth was 30% in FY2024.')
        self.assertEqual(run['status'],'complete')
        gold=dict(verdict='supported',company='MSFT',fiscal_year=2024,evidence_groups=[['p','alternative']])
        self.assertTrue(score_run(run,gold)['passed'])
        self.assertNotIn('calc_id',declarations[1][0]['parameters']['properties'])


class ScoringTests(unittest.TestCase):
    def test_quota_stops_batch_and_interrupt_saves_partial_summary(self):
        class Stub:
            model=ScriptedModel([])
            def __init__(self,interrupt=False):self.interrupt=interrupt
            def run(self,claim):
                if self.interrupt:raise KeyboardInterrupt
                return dict(claim=claim,status='error',result={'error':'Gemini HTTP 429. Check quota.'},
                            events=[],tool_calls=0,latency_seconds=.1,model=self.model.model)
        cases=[dict(id=str(n),claim='claim '+str(n),expected={'verdict':'insufficient_evidence'}) for n in range(4)]
        with TemporaryDirectory() as d:
            root=Path(d);path=root/'cases.jsonl';path.write_text('\n'.join(json.dumps(c) for c in cases))
            with patch('builtins.print'):
                target,result=run_comparison(root,cases,{'baseline':Stub(),'agent':Stub()},FACTS,path,{})
            self.assertEqual(result['run_status'],'quota_stopped')
            self.assertEqual(result['completed_runs'],3)
            self.assertEqual(result['scheduled_runs'],8)
            self.assertEqual(json.loads((target/'summary.json').read_text()),json.loads(json.dumps(result)))
            with patch('builtins.print'):
                target,result=run_comparison(root,cases,{'baseline':Stub(True),'agent':Stub()},FACTS,path,{})
            self.assertEqual(result['run_status'],'interrupted')
            self.assertEqual(result['completed_runs'],0)
            self.assertTrue((target/'summary.json').exists())

    def test_wrong_unit_entity_period_number_and_source_fail(self):
        gold=dict(verdict='supported',company='MSFT',fiscal_year=2024,verified_value=245.122,
                  verified_unit='USD_billions',required_source_ids=['s'])
        result=dict(verdict='supported',verified_value=245.122,verified_unit='USD_billions',
                    calculation=dict(company='MSFT',fiscal_year=2024),evidence_ids=['s'],sources={'s':{}})
        self.assertTrue(score_run(dict(status='complete',result=result),gold)['passed'])
        for key,value in [('verified_value',float('nan')),('verified_value',True),('verified_unit','USD')]:
            self.assertFalse(score_run(dict(status='complete',result=dict(result,**{key:value})),gold)['passed'])
        for scope in (dict(company='AAPL',fiscal_year=2024),dict(company='MSFT',fiscal_year=2025)):
            self.assertFalse(score_run(dict(status='complete',result=dict(result,calculation=scope)),gold)['passed'])
        # Merely returning a correct source without citing it must not pass.
        self.assertFalse(score_run(dict(status='complete',result=dict(result,evidence_ids=['fake'])),gold)['passed'])

    def test_operational_failure_never_matches_insufficient_gold(self):
        for status in ('error','incomplete'):
            self.assertFalse(score_run(dict(status=status,result={'verdict':'insufficient_evidence'}),
                                       {'verdict':'insufficient_evidence'})['passed'])

    def test_evidence_groups_require_each_group(self):
        run=dict(status='complete',result=dict(verdict='supported',evidence_ids=['a'],sources={'a':{}}))
        self.assertTrue(score_run(run,dict(verdict='supported',evidence_groups=[['a','b']]))['passed'])
        self.assertFalse(score_run(run,dict(verdict='supported',evidence_groups=[['a'],['c']]))['passed'])

    def test_usage_missing_retries_and_cost(self):
        self.assertIsNone(usage_summary({'events':[]})['estimated_cost_usd'])
        e=dict(kind='model',status='ok',output=dict(usage=dict(promptTokenCount=100,candidatesTokenCount=50,totalTokenCount=150),retries=0))
        prices=dict(input_per_million=1,output_per_million=2)
        self.assertAlmostEqual(usage_summary({'events':[e]},prices)['estimated_cost_usd'],.0002)
        e['output']['retries']=1
        self.assertIsNone(usage_summary({'events':[e]},prices)['estimated_cost_usd'])

    def test_loader_rejects_duplicate_ids_and_unscoped_numeric_gold(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'cases.jsonl'
            case=dict(id='x',claim='test',expected=dict(verdict='insufficient_evidence'))
            path.write_text(json.dumps(case)+'\n'+json.dumps(case))
            with self.assertRaises(ValueError):load_cases(path)
            case['expected']=dict(verdict='supported',verified_value=1,required_source_ids=['s'])
            path.write_text(json.dumps(case))
            with self.assertRaises(ValueError):load_cases(path)

    def test_comparison_keeps_gold_out_and_repeats_out_of_primary(self):
        class Stub:
            def __init__(self):self.seen=[];self.model=ScriptedModel([])
            def run(self,claim):
                self.seen.append(claim)
                return dict(claim=claim,status='complete',result={'verdict':'insufficient_evidence'},
                            events=[],tool_calls=0,latency_seconds=.1,model=self.model.model)
        cases=[dict(id=k,claim='claim '+k,expected={'verdict':'insufficient_evidence'},secret_gold='NEVER SEND') for k in ('a','b')]
        with TemporaryDirectory() as directory:
            root=Path(directory);path=root/'cases.jsonl'
            path.write_text('\n'.join(json.dumps(c) for c in cases))
            systems={k:Stub() for k in ('baseline','agent')}
            with patch('builtins.print'):
                target,summary=run_comparison(root,cases,systems,FACTS,path,{},3,['a'])
            self.assertEqual(summary['primary_cases'],2)
            self.assertEqual(summary['additional_runs'],4)
            self.assertEqual(summary['systems']['agent']['total'],2)
            self.assertEqual(summary['agent_minus_baseline']['both'],2)
            self.assertEqual(len((target/'results.jsonl').read_text().splitlines()),8)
            self.assertEqual(systems['agent'].seen,['claim a','claim b','claim a','claim a'])
            self.assertNotIn('NEVER SEND', ''.join(systems['agent'].seen))


if __name__=='__main__':unittest.main()
