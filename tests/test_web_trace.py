"""Local upload boundaries, explicit provenance, and content-free trace export."""

from email.message import Message
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fincheck.trace_tools import summaries,export_summaries
from fincheck.web import Application,handler,multipart
from fincheck.uploads import supplementary_pdf
from fincheck.memo import annotate_pdf,demo_memo,write_json


class TraceTests(unittest.TestCase):
    def test_search_and_export_remove_private_content(self):
        with TemporaryDirectory() as d:
            root=Path(d);target=root/'artifacts/local/private';target.mkdir(parents=True)
            secret='private@example.com'
            trace=dict(claim=secret,case_id='s4-001',status='complete',model='valid-model',events=[
                dict(kind='tool',scope='review',name='read_evidence',status='ok',input={'quote':secret},output={'text':secret}),
                dict(kind='model',scope='review',status='ok',output={'usage':{'totalTokenCount':2,'private':secret}}),
                dict(kind='tool',scope=secret,name=secret,status=secret,input=secret)])
            write_json(target/'trace.json',trace)
            rows=summaries(root,scope='review',tool='read_evidence')
            self.assertEqual(len(rows),1)
            self.assertEqual(len(summaries(root,claim_filter='s4-001')),1)
            all_rows=summaries(root)
            path=export_summaries(root,all_rows)
            self.assertNotIn(secret,path.read_text())
            self.assertNotIn('private/',path.read_text())
            self.assertNotIn('s4-001',path.read_text())
            self.assertEqual(len(summaries(root,status='error')),0)

    def test_legacy_case_search_survives_artifact_relocation(self):
        with TemporaryDirectory() as d:
            root=Path(d);trace=root/'artifacts/local/run-1/trace.json';trace.parent.mkdir(parents=True)
            write_json(trace,dict(claim='A claim',status='complete',events=[]))
            comparison=root/'artifacts/local/comparison-1';comparison.mkdir()
            (comparison/'results.jsonl').write_text(json.dumps(dict(case_id='s4-legacy',system='multi',trace='/old/private/path/run-1/trace.json'))+'\n')
            rows=summaries(root,claim_filter='s4-legacy')
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['case_id'],'s4-legacy')
            path=export_summaries(root,rows)
            self.assertNotIn('s4-legacy',path.read_text())


class WebTests(unittest.TestCase):
    def test_reviewed_upload_creates_scoped_report_and_pdf(self):
        from test_memo import ExtractionModel,item
        from test_workflow import Script,PLAN,APPROVE,protocol
        class Immediate:
            def submit(self,fn):fn()
            def shutdown(self,**kw):pass
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root)
            facts_path=root/'data/facts/observations.json';facts_path.parent.mkdir(parents=True)
            source=Path(__file__).resolve().parent.parent/'data/facts/observations.json'
            facts_path.write_bytes(source.read_bytes())
            app=Application(root);app.executor.shutdown();app.executor=Immediate()
            extractor=ExtractionModel([{'claims':[item('Microsoft revenue was $245.1B in FY2024.')]},*([{'claims':[]}]*4)])
            _,calc,draft,reads=protocol()
            with patch('fincheck.web.Gemini',side_effect=[extractor,Script([PLAN,calc,draft,reads,APPROVE])]):
                identifier=app.upload({'memo':('memo.pdf',pdf.read_bytes())})
                claim=app.snapshot(identifier)['claims'][0]
                app.check(identifier,{'approved_ids':[claim['claim_id']],'system':'multi'})
            snapshot=app.snapshot(identifier)
            self.assertEqual(snapshot['status'],'done')
            self.assertEqual(snapshot['report']['rows'][0]['result']['verified_value'],245.122)
            self.assertEqual(snapshot['report']['pdf_annotations']['status'],'created')
            report_artifact=snapshot['report_url'].split('/')[-2]
            self.assertTrue(app.asset(identifier,report_artifact,'annotated-memo.pdf').is_file())

    def test_upload_and_approval_session_pipeline(self):
        class Immediate:
            def submit(self,fn):fn()
            def shutdown(self,**kw):pass
        class Extractor:
            model='test-only-model'
            def answer(self,prompt,instruction):return dict(text='{"claims":[]}',usage=None)
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root)
            app=Application(root);app.executor.shutdown();app.executor=Immediate()
            with patch('fincheck.web.Gemini',return_value=Extractor()):
                identifier=app.upload({'memo':('memo.pdf',pdf.read_bytes())})
            snapshot=app.snapshot(identifier)
            self.assertEqual(snapshot['status'],'review')
            self.assertIn('artifact',snapshot)
            self.assertEqual(snapshot['claims'],[])
            with self.assertRaises(ValueError):app.check(identifier,{'approved_ids':['invented']})
            with self.assertRaises(ValueError):app.check(identifier,[])
            self.assertFalse(app.busy)
    def test_annotated_pdf_retains_original_and_reopens(self):
        import hashlib
        import pymupdf
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root);before=hashlib.sha256(pdf.read_bytes()).hexdigest()
            report={'rows':[dict(claim=dict(claim_id='c1',page=1,quote='Microsoft revenue was $245.1B in FY2024.'),
                status='complete',result=dict(verdict='supported',explanation='Matches the approved observation.',sources={'official-source':{}}))]}
            result=annotate_pdf(pdf,root,report)
            self.assertEqual(result['claims'][0]['location'],'highlight')
            self.assertEqual(hashlib.sha256(pdf.read_bytes()).hexdigest(),before)
            with pymupdf.open(root/'annotated-memo.pdf') as doc:
                annotation=next(doc[0].annots())
                self.assertIn('official-source',annotation.info['content'])
                self.assertEqual(len(doc),5)
    def test_explicit_company_expansion_preserves_defaults(self):
        from fincheck.sec import company_universe,COMPANIES
        table={'0':dict(ticker='NVDA',cik_str=1045810,title='NVIDIA CORP')}
        result=company_universe(table,['NVDA'])
        self.assertTrue(set(COMPANIES)<=set(result))
        self.assertEqual(result['NVDA']['cik'],1045810)
        with self.assertRaises(ValueError):company_universe(table,['NOT_A_TICKER'])
    def test_multipart_preserves_bytes_and_rejects_duplicates(self):
        body=b'--B\r\nContent-Disposition: form-data; name="memo"; filename="memo.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF\x00binary\r\n--B--\r\n'
        fields=multipart('multipart/form-data; boundary=B',body)
        self.assertEqual(fields['memo'][1],b'%PDF\x00binary')
        duplicate=body.replace(b'--B--',body.split(b'--B--')[0]+b'--B--')
        with self.assertRaises(ValueError):multipart('multipart/form-data; boundary=B',duplicate)

    def test_local_host_origin_and_token_are_enforced(self):
        with TemporaryDirectory() as d:
            app=Application(Path(d));cls=handler(app,8765)
            obj=object.__new__(cls);obj.headers=Message();obj.headers['Host']='evil.example:8765'
            obj.path='/api/upload';results=[];obj.respond=lambda *args:results.append(args)
            obj.do_POST();self.assertEqual(results[-1][0],403)
            obj.headers.replace_header('Host','127.0.0.1:8765');obj.headers['X-Fincheck-Token']=app.token
            obj.headers['Origin']='https://evil.example'
            obj.do_POST();self.assertEqual(results[-1][0],403)
            app.executor.shutdown()

    def test_assets_cannot_escape_session(self):
        with TemporaryDirectory() as d:
            root=Path(d);app=Application(root);folder=root/'artifact';folder.mkdir()
            (folder/'page.txt').write_text('source')
            app.jobs['j']={'allowed':[folder],'directory':folder,'passages':'private'}
            self.assertEqual(app.asset('j','artifact','page.txt'),(folder/'page.txt').resolve())
            with self.assertRaises(ValueError):app.asset('j','artifact','../secret.txt')
            with self.assertRaises(ValueError):app.asset('j','other','page.txt')
            self.assertNotIn('passages',app.snapshot('j'))
            app.executor.shutdown()

    def test_upload_rejects_invalid_inputs_before_work(self):
        with TemporaryDirectory() as d:
            app=Application(Path(d))
            with self.assertRaises(ValueError):app.upload({'memo':('file.exe',b'bad')})
            with self.assertRaises(ValueError):app.upload({'memo':('memo.pdf',b'x'),'evidence':('source.pdf',b'x')})
            app.executor.shutdown()

    def test_supplementary_sources_are_not_official_facts(self):
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root)
            passages,memo=supplementary_pdf(pdf,root/'source','TSLA',2024)
            self.assertTrue(passages)
            self.assertTrue(all(p['company']=='TSLA' and p['source_type']=='user_supplied' for p in passages))
            self.assertTrue(all(p['source_sha256']==memo['source_sha256'] for p in passages))


if __name__=='__main__':unittest.main()
