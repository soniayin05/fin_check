"""Offline UI/backend contracts using cached facts; no model or network substitution."""

from email.message import Message
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fincheck.offline import catalog,history,history_directories,numeric_check,read_report
from fincheck.web import Application,handler

ROOT=Path(__file__).resolve().parent.parent


def claim(**changes):
    return dict(dict(company='MSFT',year=2024,operation='fact',measure='revenue',claimed_value='245.1',unit='USD_billions'),**changes)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup);self.root=Path(directory.name)
        target=self.root/'data/facts';target.mkdir(parents=True)
        for name in ('observations.json','coverage.json'):(target/name).write_bytes((ROOT/'data/facts'/name).read_bytes())
        self.addCleanup(patch.stopall)
        patch('fincheck.web.Gemini',side_effect=AssertionError('Offline mode must not construct Gemini')).start()
        patch('urllib.request.urlopen',side_effect=AssertionError('Offline mode must not access the network')).start()

    def app(self):
        app=Application(self.root);self.addCleanup(app.executor.shutdown);return app

    def test_cached_facts_growth_ratio_missing_evidence_and_precision(self):
        cases=[(claim(),'supported',245.122),
               (claim(claimed_value='250'),'contradicted',245.122),
               (claim(year=2030),'insufficient_evidence',None),
               (claim(operation='growth',claimed_value='15.7',unit='%'),'supported',15.66996201307127),
               (claim(operation='ratio',measure='operating_profit_margin',claimed_value='44.6',unit='%'),'supported',44.64429957311056),
               (claim(claimed_value='245.10000'),'contradicted',245.122)]
        for payload,verdict,number in cases:
            with self.subTest(payload=payload):
                target,report=numeric_check(self.root,payload)
                self.assertEqual(report['result']['verdict'],verdict)
                if number is not None:self.assertAlmostEqual(report['result']['verified_value'],number,places=6)
                self.assertEqual(report['model_calls'],0)
                self.assertEqual(report['network_requests'],0)
                self.assertTrue(all(isinstance(value,str) and len(value)==64 for value in report['code_sha256'].values()))
                self.assertEqual(report['input']['claimed_value'],payload['claimed_value'])
                self.assertEqual(json.loads((target/'report.json').read_text()),report)
                self.assertIn('Zero model calls',(target/'report.html').read_text())

    def test_invalid_inputs_do_not_create_results(self):
        cases=[[],claim(year=True),claim(year='2024'),claim(year=0),claim(claimed_value=245.1),
               claim(claimed_value='NaN'),claim(unit='%'),claim(measure='invented'),
               claim(operation='ratio',measure='invented'),claim(company='../../secret'),dict(claim(),forecast=True)]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):numeric_check(self.root,payload)
        self.assertFalse((self.root/'artifacts/local').exists())

    def test_catalog_describes_actual_snapshot_without_a_model(self):
        result=catalog(self.root)
        self.assertEqual({c['ticker'] for c in result['companies']},{'MSFT','AAPL','GOOGL'})
        self.assertEqual(result['observations'],546)
        self.assertIn('USD_billions',result['units'])
        self.assertIn('operating_profit_margin',{r['name'] for r in result['ratios']})
        self.assertEqual(result['model_calls'],0)

    def test_history_survives_restart_and_does_not_rerun_or_mutate_old_result(self):
        app=self.app();saved=app.offline_check(claim());folder=saved['artifact']
        original=(self.root/'artifacts/local'/folder/'report.json').read_bytes()
        second=self.app();entries=history(self.root)
        self.assertEqual(entries[0]['id'],folder)
        self.assertTrue(entries[0]['snapshot_matches_current'])
        path=self.root/'data/facts/observations.json';data=json.loads(path.read_text())
        next(o for o in data['observations'] if o['company']=='MSFT' and o['metric']=='revenue' and o['fiscal_year']==2024)['value']=250000000000
        path.write_text(json.dumps(data))
        self.assertFalse(history(self.root)[0]['snapshot_matches_current'])
        opened=second.open_history({'id':folder})
        source=second.asset(opened['id'],folder,'report.json')
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(json.loads(source.read_text())['result']['verified_value'],245.122)
        with self.assertRaises(ValueError):second.check(opened['id'],{'approved_ids':['invented']})

    def test_memo_history_allows_only_linked_original_document_hash(self):
        base=self.root/'artifacts/local';folder=base/'memo-check-20261127T000000000000Z';folder.mkdir(parents=True)
        source=base/'memo-source';(source/'pages').mkdir(parents=True);(source/'pages/page-001.png').write_bytes(b'image fixture')
        (source/'memo.json').write_text(json.dumps({'source_sha256':'a'*64}))
        (folder/'report.json').write_text(json.dumps(dict(memo_sha256='a'*64,rows=[],status='needs_review',system='multi')))
        (folder/'report.html').write_text('<a href="../memo-source/pages/page-001.png">Page</a>')
        app=self.app();opened=app.open_history({'id':folder.name})
        self.assertEqual(app.asset(opened['id'],'memo-source','pages/page-001.png').read_bytes(),b'image fixture')
        (source/'memo.json').write_text(json.dumps({'source_sha256':'b'*64}))
        denied=app.open_history({'id':folder.name})
        with self.assertRaises(ValueError):app.asset(denied['id'],'memo-source','pages/page-001.png')

    def test_history_rejects_paths_symlinks_and_broken_reports(self):
        target,_=numeric_check(self.root,claim())
        with self.assertRaises(ValueError):read_report(self.root,'../'+target.name)
        outside=self.root/'outside.json';outside.write_bytes((target/'report.json').read_bytes())
        (target/'report.json').unlink();(target/'report.json').symlink_to(outside)
        with self.assertRaises(ValueError):read_report(self.root,target.name)
        self.assertEqual(history(self.root),[])
        (target/'report.json').unlink();(target/'report.json').write_text('{incomplete')
        self.assertEqual(history(self.root),[])

    def test_html_escapes_source_text_and_rejects_unsafe_source_links(self):
        path=self.root/'data/facts/observations.json';data=json.loads(path.read_text())
        source=next(o['source_id'] for o in data['observations'] if o['company']=='MSFT' and o['metric']=='revenue' and o['fiscal_year']==2024)
        data['sources'][source].update(title='<script>alert(1)</script>',url='javascript:alert(1)')
        path.write_text(json.dumps(data))
        target,_=numeric_check(self.root,claim());html=(target/'report.html').read_text()
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('href="javascript:',html)

    def test_http_catalog_offline_check_and_history_require_local_token(self):
        app=self.app();obj=object.__new__(handler(app,8765));obj.headers=Message()
        obj.headers['Host']='127.0.0.1:8765';obj.path='/api/catalog';responses=[]
        obj.respond=lambda *values:responses.append(values)
        obj.do_GET();self.assertEqual(responses[-1][0],403)
        obj.headers['X-Fincheck-Token']=app.token;obj.do_GET();self.assertEqual(responses[-1][0],200)
        body=json.dumps(claim()).encode();obj.path='/api/offline-check';obj.rfile=io.BytesIO(body)
        obj.headers['Content-Length']=str(len(body));obj.do_POST()
        self.assertEqual(responses[-1][0],201);saved=responses[-1][1]
        obj.path='/api/history';obj.do_GET();self.assertEqual(responses[-1][1]['reports'][0]['id'],saved['artifact'])
        body=json.dumps({'id':saved['artifact']}).encode();obj.path='/api/history/open';obj.rfile=io.BytesIO(body)
        obj.headers.replace_header('Content-Length',str(len(body)));obj.do_POST()
        self.assertEqual(responses[-1][0],200)
