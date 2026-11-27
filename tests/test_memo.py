"""Real PDF ingestion plus scripted extraction tests; not an accuracy evaluation."""

import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fincheck.memo import (artifact_dir, check_claims, claim_review_reasons,
    demo_memo, extract_claims, ingest_pdf, validate_claim, write_json)
from fincheck.model import ModelError
from fincheck.retrieval import PassageStore
from fincheck.tools import FactStore
from test_agent import ScriptedModel, PLAN

ROOT=Path(__file__).resolve().parent.parent
FACTS=ROOT/'data/sample/financial_facts.json'


def item(quote,**changes):
    return dict(dict(quote=quote,company='MSFT',metric='revenue',period='FY2024',
                     value='245.1',unit='USD_billions',claim_type='numeric',qualifiers=[]),**changes)


class ExtractionModel:
    model='test-only-extractor'
    def __init__(self,outputs):self.outputs=iter(outputs);self.prompts=[]
    def answer(self,prompt,instruction):
        self.prompts.append(prompt)
        payload=next(self.outputs)
        if isinstance(payload,Exception):raise payload
        return dict(text=json.dumps(payload),usage=None,retries=0)


class GroundingTests(unittest.TestCase):
    def test_exact_quote_location_and_value(self):
        quote='Microsoft revenue was $245.1B in FY2024.'
        p=dict(page=2,text='Heading\n'+quote+'\n')
        c=validate_claim(item(quote),p)
        self.assertEqual(p['text'][c['start']:c['end']],quote)
        self.assertFalse(c['approved'])
        for bad in [item(quote+' invented'),item(quote,value='250'),item(quote,qualifiers=['excluding'])]:
            with self.assertRaises(ValueError):validate_claim(bad,p)

    def test_negation_and_suffix_cannot_be_dropped(self):
        p=dict(page=1,text='Not '+ 'Microsoft revenue was $245.1B in FY2024. excluding acquisitions\n')
        with self.assertRaises(ValueError):validate_claim(item('Microsoft revenue was $245.1B in FY2024.'),p)

    def test_duplicate_location_is_not_silently_chosen(self):
        quote='Microsoft revenue was $245.1B in FY2024.'
        with self.assertRaises(ValueError):validate_claim(item(quote),dict(page=1,text=quote+'\n'+quote))

    def test_forecasts_and_adjustments_need_review(self):
        for quote in ['Revenue may grow 10%.','Adjusted revenue excluding acquisitions was $245.1B.']:
            self.assertTrue(claim_review_reasons(quote,item(quote)))

    def test_extraction_rejects_hallucination_and_carries_context(self):
        with TemporaryDirectory() as d:
            target=Path(d)
            quote='Microsoft revenue was $245.1B in FY2024.'
            memo=dict(source_sha256='hash',pages=[dict(page=1,text='Company: Microsoft\nFiscal year: 2024'),dict(page=2,text=quote)])
            model=ExtractionModel([{'claims':[]},{'claims':[item(quote),item('Made-up quote')]}])
            result=extract_claims(memo,model,target)
            self.assertEqual(len(result['claims']),1)
            self.assertTrue(result['issues'])
            self.assertIn('Company: Microsoft',model.prompts[1])
            self.assertEqual(result['claims'][0]['page'],2)
            self.assertTrue((target/'extraction_trace.json').exists())

    def test_extraction_errors_and_budget_keep_prior_progress(self):
        with TemporaryDirectory() as d:
            quote='Microsoft revenue was $245.1B in FY2024.'
            memo=dict(source_sha256='h',pages=[dict(page=n,text=quote) for n in (1,2,3)])
            model=ExtractionModel([{'claims':[item(quote)]},ModelError('quota')])
            result=extract_claims(memo,model,Path(d),model_budget=2)
            self.assertEqual(len(result['claims']),1)
            self.assertEqual(result['model_calls'],2)
            self.assertTrue(any(i['reason']=='extraction_model_budget_exhausted' for i in result['issues']))


@unittest.skipUnless(importlib.util.find_spec('pymupdf'),'Install requirements.txt for PDF integration tests')
class PdfMemoTests(unittest.TestCase):
    def test_five_pages_and_images_match(self):
        import pymupdf
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root);target=root/'ingest'
            memo=ingest_pdf(pdf,target)
            self.assertEqual([p['page'] for p in memo['pages']],[1,2,3,4,5])
            self.assertIn('$245.1B',memo['pages'][0]['text'])
            self.assertIn('expects',memo['pages'][4]['text'])
            for p in memo['pages']:
                self.assertEqual((target/p['text_file']).read_text(),p['text'])
                image=pymupdf.Pixmap(str(target/p['image']))
                self.assertLessEqual(max(image.width,image.height),1801)
                self.assertGreater(min(image.width,image.height),0)
            with self.assertRaises(ValueError):ingest_pdf(pdf,root/'over-limit',max_pages=4)

    def test_scanned_page_and_encrypted_pdf(self):
        import pymupdf
        with TemporaryDirectory() as d:
            root=Path(d);pdf=root/'blank.pdf'
            with pymupdf.open() as doc:
                doc.new_page();doc.save(str(pdf))
            self.assertTrue(ingest_pdf(pdf,root/'ingest')['pages'][0]['warnings'])
            locked=root/'locked.pdf'
            with pymupdf.open() as doc:
                doc.new_page();doc.save(str(locked),encryption=pymupdf.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw='secret')
            with self.assertRaises(ValueError):ingest_pdf(locked,root/'locked-out')

    def test_approval_and_shared_budget(self):
        with TemporaryDirectory() as d:
            root=Path(d);target=root/'input';target.mkdir()
            quote='Microsoft revenue was $245.1B in FY2024.'
            page=dict(page=1,text=quote,text_sha256=hashlib.sha256(quote.encode()).hexdigest(),warnings=[])
            memo=dict(source_sha256='h',source_name='example.pdf',pages=[page])
            # Distinct pages produce distinct quote IDs.
            memo['pages']=[dict(page,page=n) for n in (1,2)]
            claims=[validate_claim(item(quote),p) for p in memo['pages']]
            write_json(target/'memo.json',memo)
            write_json(target/'claims.json',dict(memo_sha256='h',claims=claims,issues=[]))
            store=FactStore(FACTS);passages=PassageStore(root/'missing.json')
            _,report=check_claims(root,target/'claims.json',store,passages,ScriptedModel([]),FACTS)
            self.assertEqual(report['verification_model_calls'],0)
            self.assertTrue(all(r['status']=='needs_review' for r in report['rows']))
            calculate={'name':'calculate','args':dict(company='MSFT',year=2024,operation='fact',measure='revenue',claimed_value='245.1',unit='USD_billions')}
            final={'name':'submit_verdict','args':dict(verdict='supported',explanation='Verified.',evidence_ids=['msft-ar2024-income'],calc_id='calc-1')}
            model=ScriptedModel([PLAN,calculate,final])
            out,report=check_claims(root,target/'claims.json',store,passages,model,FACTS,approve=True,model_budget=3,tool_budget=3)
            self.assertEqual(report['rows'][0]['status'],'complete')
            self.assertEqual(report['rows'][1]['status'],'not_run')
            self.assertEqual(report['verification_model_calls'],3)
            self.assertEqual(report['verification_tool_calls'],3)
            self.assertTrue((out/'report.html').exists())

    def test_tampered_location_and_forecast_are_not_verified(self):
        with TemporaryDirectory() as d:
            root=Path(d);target=root/'input';target.mkdir()
            quote='Microsoft expects revenue to reach $245.1B in FY2024.'
            page=dict(page=1,text=quote,text_sha256=hashlib.sha256(quote.encode()).hexdigest(),warnings=[])
            write_json(target/'memo.json',dict(source_sha256='h',source_name='memo.pdf',pages=[page]))
            c=validate_claim(item(quote),page)
            write_json(target/'claims.json',dict(memo_sha256='h',claims=[c],issues=[]))
            args=(root,target/'claims.json',FactStore(FACTS),PassageStore(root/'none'),ScriptedModel([]),FACTS)
            _,r=check_claims(*args,approve=True)
            self.assertEqual(r['rows'][0]['status'],'needs_review')
            c['start']=999
            write_json(target/'claims.json',dict(memo_sha256='h',claims=[c],issues=[]))
            _,r=check_claims(*args,approve=True)
            self.assertEqual(r['rows'][0]['status'],'error')

    def test_html_escapes_untrusted_quotes(self):
        from fincheck.memo import render_report
        with TemporaryDirectory() as d:
            target=Path(d)
            claim=dict(page=1,claim_id='x',quote='<script>alert(1)</script>')
            render_report(target,dict(source_name='memo.pdf',pages=[dict(page=1)]),dict(status='needs_review',rows=[dict(claim=claim,status='needs_review')]),target)
            html=(target/'report.html').read_text()
            self.assertNotIn('<script>',html)
            self.assertIn('&lt;script&gt;',html)

    def test_extraction_dry_run_never_constructs_model(self):
        from fincheck.memo import memo_command
        with TemporaryDirectory() as d:
            root=Path(d);pdf=demo_memo(root)
            args=SimpleNamespace(command='extract-memo',text=str(pdf),dry_run=True,max_pages=10,memo_model_budget=20)
            with patch('fincheck.model.Gemini',side_effect=AssertionError('Must not create model')),patch('builtins.print'):
                self.assertEqual(memo_command(args,None,root),0)

    def test_checking_dry_run_never_constructs_model(self):
        from fincheck.memo import memo_command
        with TemporaryDirectory() as d:
            root=Path(d);quote='Microsoft revenue was $245.1B in FY2024.'
            page=dict(page=1,text=quote)
            c=validate_claim(item(quote),page)
            write_json(root/'memo.json',dict(source_sha256='h',pages=[page]))
            write_json(root/'claims.json',dict(memo_sha256='h',claims=[c]))
            args=SimpleNamespace(command='check-memo',text=str(root/'claims.json'),dry_run=True,
                memo_model_budget=20,memo_tool_budget=36,approve_extracted=True)
            with patch('fincheck.model.Gemini',side_effect=AssertionError('Must not create model')),patch('builtins.print'):
                self.assertEqual(memo_command(args,None,root),0)


if __name__=='__main__':unittest.main()
