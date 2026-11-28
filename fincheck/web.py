"""Local-only PDF upload/review/check UI over the existing workflow."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from socketserver import TCPServer
import json
from pathlib import Path
import secrets
from threading import Lock
from urllib.parse import unquote,urlsplit

from .memo import artifact_dir,ingest_pdf,extract_claims,check_claims,write_json
from .model import Gemini,load_env
from .retrieval import PassageStore
from .tools import FactStore
from .uploads import supplementary_pdf
from .offline import catalog,numeric_check,history,history_directories

MAX_BODY=42*1024*1024


def multipart(content_type,body):
    if len(body)>MAX_BODY or not content_type.startswith('multipart/form-data'):
        raise ValueError('Supply PDF form data within the upload limit')
    message=BytesParser(policy=default).parsebytes(b'Content-Type: '+content_type.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+body)
    fields={}
    for part in message.iter_parts():
        name=part.get_param('name',header='content-disposition')
        if not name or name in fields:raise ValueError('Invalid or duplicate form field')
        data=part.get_payload(decode=True) or b''
        filename=part.get_filename()
        fields[name]=(Path(filename).name,data) if filename else data.decode('utf-8')
    return fields


class Application:
    def __init__(self,root):
        self.root=Path(root);self.jobs={};self.lock=Lock()
        self.executor=ThreadPoolExecutor(max_workers=1);self.busy=False
        self.token=secrets.token_urlsafe(32)

    def snapshot(self,identifier):
        with self.lock:
            job=self.jobs.get(identifier)
            if job is None:raise ValueError('Unknown session')
            return deepcopy({k:v for k,v in job.items() if k not in {'passages','bundle','directory','allowed'}})

    def update(self,identifier,**values):
        with self.lock:self.jobs[identifier].update(values)

    def offline_check(self,payload):
        with self.lock:
            if len(self.jobs)>=20:raise ValueError('Session limit reached; restart the local application')
            target,report=numeric_check(self.root,payload)
            identifier=secrets.token_hex(16)
            self.jobs[identifier]=dict(id=identifier,artifact=target.name,status='done',mode='offline_numeric',
                message='Completed from cached observations without model calls.',directory=target,allowed=[target],
                report=report,report_url=f'/assets/{identifier}/{target.name}/report.html')
        return self.snapshot(identifier)

    def open_history(self,payload):
        if not isinstance(payload,dict) or set(payload)!={'id'}:raise ValueError('Supply a saved report ID')
        with self.lock:
            if len(self.jobs)>=20:raise ValueError('Session limit reached; restart the local application')
            target,report,allowed=history_directories(self.root,payload['id'])
            identifier=secrets.token_hex(16)
            self.jobs[identifier]=dict(id=identifier,artifact=target.name,status='done',mode='saved_report',
                message='Saved result opened; no extraction or verification was rerun.',directory=target,allowed=allowed,
                report=report,report_url=f'/assets/{identifier}/{target.name}/report.html')
        return dict(id=identifier,report_url=f'/assets/{identifier}/{target.name}/report.html')

    def upload(self,fields):
        pdf=fields.get('memo')
        if not isinstance(pdf,tuple) or not pdf[0].lower().endswith('.pdf') or not pdf[1]:
            raise ValueError('Choose a PDF memo')
        if len(pdf[1])>20*1024*1024:raise ValueError('Memo PDF exceeds 20 MiB')
        extra=fields.get('evidence')
        if isinstance(extra,tuple) and extra[1]:
            if not extra[0].lower().endswith('.pdf') or len(extra[1])>20*1024*1024:
                raise ValueError('Supplementary evidence must be a PDF of at most 20 MiB')
            if not fields.get('evidence_company') or not fields.get('evidence_year'):
                raise ValueError('Specify company/ticker and year for supplementary evidence')
        with self.lock:
            if self.busy:raise ValueError('A task is already running; wait for it to finish')
            if len(self.jobs)>=20:raise ValueError('Session limit reached; restart the local application')
            identifier=secrets.token_hex(16);directory=artifact_dir(self.root,'web-memo')
            self.jobs[identifier]=dict(id=identifier,artifact=directory.name,status='extracting',message='Extracting PDF pages and claims...',
                directory=directory,allowed=[directory],bundle=None,claims=[],issues=[],page_warnings=[])
            self.busy=True
        def work():
            try:
                model=Gemini()
                path=directory/'uploaded-memo.pdf';path.write_bytes(pdf[1])
                memo=ingest_pdf(path,directory,max_pages=10)
                context={}
                if fields.get('context_company'):context['company']=fields['context_company']
                if fields.get('context_year'):context['fiscal_year']=int(fields['context_year'])
                bundle=extract_claims(memo,model,directory,context,max_claims=12,model_budget=10)
                passages=PassageStore(self.root/'data/indexes/passages.json')
                if isinstance(extra,tuple) and extra[1]:
                    source=directory/'uploaded-evidence.pdf';source.write_bytes(extra[1])
                    added,source_memo=supplementary_pdf(source,directory/'evidence',fields['evidence_company'],int(fields['evidence_year']))
                    passages.passages=passages.passages+added
                    bundle['supplementary_evidence']=dict(source_sha256=source_memo['source_sha256'],provenance='user_supplied')
                with self.lock:
                    self.jobs[identifier].update(status='review',message='Review quotes and scope, then select claims to check.',
                        bundle=bundle,claims=bundle['claims'],issues=bundle['issues'],
                        page_warnings=[dict(page=p['page'],warnings=p['warnings']) for p in memo['pages'] if p['warnings']],
                        passages=passages)
                write_json(directory/'claims.json',bundle)
            except Exception as error:self.update(identifier,status='error',message=str(error))
            finally:
                with self.lock:self.busy=False
        self.executor.submit(work)
        return identifier

    def check(self,identifier,payload):
        if not isinstance(payload,dict):raise ValueError('Supply an approval object')
        approved=payload.get('approved_ids',[]);system=payload.get('system','multi')
        if not isinstance(approved,list) or any(not isinstance(x,str) for x in approved) or system not in {'agent','multi'}:
            raise ValueError('Invalid approved claims/system')
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job['status'] not in {'review','done'} or job.get('bundle') is None:
                raise ValueError('Select a freshly extracted memo before checking')
            if self.busy:raise ValueError('A task is already running')
            bundle=deepcopy(job['bundle']);directory=job['directory'];passages=job['passages']
            if not approved or set(approved)-{c['claim_id'] for c in bundle['claims']}:
                raise ValueError('Select existing claims whose quotations and scope you reviewed')
            for c in bundle['claims']:c['approved']=c['claim_id'] in approved
            self.busy=True;job.update(status='checking',message='Checking approved claims with shared budgets...')
        def work():
            try:
                path=directory/('approved-'+secrets.token_hex(6)+'.json');write_json(path,bundle)
                facts_path=self.root/'data/facts/observations.json'
                if not facts_path.exists():facts_path=self.root/'data/sample/financial_facts.json'
                target,report=check_claims(self.root,path,FactStore(facts_path),passages,Gemini(),facts_path,
                    model_budget=40,tool_budget=60,max_calls=12,max_turns=12,system=system)
                with self.lock:self.jobs[identifier]['allowed'].append(target)
                self.update(identifier,status='done',message='Review the results and remaining issues.',report=report,
                    report_url=f'/assets/{identifier}/{target.name}/report.html')
            except Exception as error:self.update(identifier,status='error',message=str(error))
            finally:
                with self.lock:self.busy=False
        self.executor.submit(work)

    def asset(self,identifier,artifact,path):
        with self.lock:
            job=self.jobs.get(identifier)
            if not job:raise ValueError('Unknown session')
            directories=list(job['allowed'])
        folder=next((p for p in directories if p.name==artifact),None)
        if folder is None:raise ValueError('Artifact not in this session')
        target=(folder/path).resolve()
        if not target.is_relative_to(folder.resolve()) or target.suffix not in {'.png','.html','.json','.pdf','.txt'} or not target.is_file():
            raise ValueError('Asset unavailable')
        return target


def handler(application,port):
    class Handler(BaseHTTPRequestHandler):
        def host_ok(self):
            return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}
        def respond(self,status,body,kind='application/json'):
            if isinstance(body,dict):body=json.dumps(body).encode()
            if isinstance(body,str):body=body.encode()
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if not self.host_ok():self.respond(403,{'error':'Local host required'});return
            parts=urlsplit(self.path).path.split('/')
            try:
                if self.path=='/':self.respond(200,HTML.replace('__TOKEN__',application.token),'text/html; charset=utf-8')
                elif self.path in {'/api/catalog','/api/history'}:
                    if self.headers.get('X-Fincheck-Token')!=application.token:
                        self.respond(403,{'error':'Local session authentication required'});return
                    self.respond(200,catalog(application.root) if self.path=='/api/catalog' else {'reports':history(application.root)})
                elif len(parts)==4 and parts[1:3]==['api','jobs']:self.respond(200,application.snapshot(parts[3]))
                elif len(parts)>=5 and parts[1]=='assets':
                    # Reports link ../other-artifact/pages/...; normalize inside the session, never across it.
                    decoded=unquote('/'.join(parts[3:]))
                    from posixpath import normpath
                    normalized=normpath(decoded).split('/')
                    path=application.asset(parts[2],normalized[0],'/'.join(normalized[1:]))
                    kinds={'.png':'image/png','.html':'text/html; charset=utf-8','.pdf':'application/pdf','.json':'application/json','.txt':'text/plain; charset=utf-8'}
                    self.respond(200,path.read_bytes(),kinds[path.suffix])
                else:self.respond(404,{'error':'Not found'})
            except (ValueError,OSError,KeyError):self.respond(404,{'error':'Not found'})
        def do_POST(self):
            origin=self.headers.get('Origin')
            if not self.host_ok() or self.headers.get('X-Fincheck-Token')!=application.token or (origin and origin not in {f'http://127.0.0.1:{port}',f'http://localhost:{port}'}):
                self.respond(403,{'error':'Local session authentication required'});return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=MAX_BODY:raise ValueError('Invalid upload size')
                body=self.rfile.read(size)
                if len(body)!=size:raise ValueError('Incomplete upload')
                if self.path=='/api/upload':
                    identifier=application.upload(multipart(self.headers.get('Content-Type',''),body))
                    self.respond(202,{'id':identifier})
                elif self.path=='/api/offline-check':self.respond(201,application.offline_check(json.loads(body)))
                elif self.path=='/api/history/open':self.respond(200,application.open_history(json.loads(body)))
                elif self.path.startswith('/api/check/'):
                    application.check(self.path.rsplit('/',1)[1],json.loads(body));self.respond(202,{'started':True})
                else:self.respond(404,{'error':'Not found'})
            except (ValueError,KeyError,TypeError,OSError) as error:self.respond(400,{'error':str(error)})
        def log_message(self,*args):pass
    return Handler


def serve(root,port=8765):
    if not 1024<=port<=65535:raise ValueError('Use a local port from 1024 to 65535')
    load_env(Path(root));application=Application(root)
    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            TCPServer.server_bind(self)
            self.server_name='localhost';self.server_port=self.server_address[1]
    server=LocalServer(('127.0.0.1',port),handler(application,port))
    print(f'FinCheck: http://127.0.0.1:{port} (local only; Ctrl+C to stop)',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();application.executor.shutdown(wait=False,cancel_futures=True)


HTML='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinCheck</title><style>
body{margin:0;background:#f6f8fb;color:#17283d;font:16px system-ui}main{max-width:1000px;margin:48px auto;padding:0 24px}h1{font-size:36px;margin-bottom:8px}
.card{background:white;border:1px solid #dbe3ee;border-radius:12px;padding:24px;margin:22px 0}label{display:block;margin:14px 0 5px}input,select,button{font:inherit;padding:9px;max-width:100%;box-sizing:border-box}button{border:0;border-radius:6px;background:#155ac0;color:white;cursor:pointer;margin-top:18px}button:disabled{opacity:.5;cursor:wait}.hint{color:#52657a;font-size:14px}blockquote{white-space:pre-wrap;margin:12px 0;padding:12px;background:#f1f5fa}.claim{border-top:1px solid #dbe3ee;padding:18px 0}.claim label{display:flex;gap:12px}.status{font-weight:600}a{color:#155ac0}.error{color:#a12727}
</style><main><h1>FinCheck</h1><p>Check financial claims against cached source evidence.</p>
<section class="card"><h2>Check a number offline</h2><p class="hint">Works without Gemini or an API key. Enter one historical, company-wide numeric claim. The fields you enter are checked directly; natural-language meaning, segment figures and forecasts need separate review.</p>
<p id="coverage" class="hint">Loading cached coverage...</p>
<form id="offline"><label for="offline-company">Company ticker</label><input id="offline-company" name="company" list="cached-companies" value="MSFT" maxlength="40" required><datalist id="cached-companies"></datalist>
<label for="offline-year">Fiscal year</label><input id="offline-year" name="year" type="number" min="1900" max="2100" step="1" value="2024" required>
<label for="offline-operation">Check type</label><select id="offline-operation" name="operation"><option value="fact">Reported annual value</option><option value="growth">Year-over-year growth</option><option value="ratio">Financial ratio</option></select>
<label for="offline-measure">Measure</label><select id="offline-measure" name="measure" required></select>
<label for="offline-value">Claimed value (keep the written precision)</label><input id="offline-value" name="claimed_value" value="245.1" inputmode="decimal" maxlength="64" required>
<label for="offline-unit">Unit</label><select id="offline-unit" name="unit" required></select>
<button id="offline-submit" disabled>Check cached data</button></form><p id="offline-message" role="status"></p><div id="offline-result" hidden></div></section>
<h2>PDF memo workflow · Gemini required</h2>
<form id="upload" class="card"><h2>1. Extract claims</h2><label>Memo PDF (up to 10 pages, 20 MiB)</label><input type="file" name="memo" accept="application/pdf" required>
<p class="hint">Extraction sends page text to your configured Gemini API. Files and reports are stored locally. Charts and scans need visual review.</p>
<details><summary>Optional context and supplementary evidence</summary><label>Memo company/ticker hint</label><input name="context_company" placeholder="e.g. MSFT"><label>Memo fiscal-year hint</label><input name="context_year" type="number" placeholder="2024">
<label>Supplementary source PDF</label><input type="file" name="evidence" accept="application/pdf"><label>Source company/ticker</label><input name="evidence_company" placeholder="Required if attaching evidence"><label>Source fiscal year</label><input name="evidence_year" type="number" placeholder="2024"><p class="hint">Uploaded sources are marked user supplied; they do not become trusted SEC observations or independent corroboration.</p></details>
<button id="extract">Extract claims</button></form><p id="message" role="status"></p>
<section id="review" class="card" hidden><h2>2. Review quotations and scope</h2><p class="hint">Select only claims whose quotation, company, period and units you have reviewed against the PDF. Forecasts and special qualifiers stay flagged.</p><div id="claims"></div>
<label>Verification mode</label><select id="system"><option value="multi">Access-scoped evidence and read-only review</option><option value="agent">Single agent</option></select>
<p class="hint">Shared verification cap: 40 logical model requests and 60 tool calls. Each claim is capped at 12 model turns and 12 tool calls. API quota may be used.</p><button id="check">Check selected claims</button></section>
<section id="results" class="card" hidden><h2>3. Results</h2><div id="rows"></div><p id="report"></p></section>
<section class="card"><h2>Saved reports</h2><p class="hint">Open previous results without extraction, verification or model calls. Saved reports keep their original data snapshot.</p><button id="history-refresh" type="button">Refresh reports</button><p id="history-message" role="status"></p><div id="history-list"></div></section>
<p class="hint">Built-in SEC coverage: Microsoft, Apple and Alphabet. Missing evidence, execution failures and review flags are reported separately. This tool checks factual consistency; it does not recommend investments.</p></main>
<script>
const token='__TOKEN__';let job=null,timer=null;const $=id=>document.getElementById(id);
function message(s){$('message').textContent=s}function node(tag,text,parent){let e=document.createElement(tag);e.textContent=text;parent.append(e);return e}
async function request(url,options={}){options.headers={...(options.headers||{}),'X-Fincheck-Token':token};let r=await fetch(url,options);let data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data}
$('upload').onsubmit=async e=>{e.preventDefault();$('extract').disabled=true;message('Uploading...');try{let d=await request('/api/upload',{method:'POST',body:new FormData(e.target)});job=d.id;$('review').hidden=true;$('results').hidden=true;poll()}catch(err){message(err.message);$('extract').disabled=false}};
function showClaims(d){$('claims').replaceChildren();for(let c of d.claims){let box=node('div','',$('claims'));box.className='claim';let label=node('label','',box);let input=document.createElement('input');input.type='checkbox';input.value=c.claim_id;label.append(input);node('span','Page '+c.page+' · '+c.company+' · '+c.period,label);node('blockquote',c.quote,box);node('p','Metric: '+c.metric+' | Value: '+(c.value??'not specified')+' | Unit: '+c.unit,box);if(c.review_reasons.length)node('p','Needs review: '+c.review_reasons.join(', '),box);let a=node('a','View source page',box);a.href='/assets/'+job+'/'+d.artifact+'/pages/page-'+String(c.page).padStart(3,'0')+'.png';a.target='_blank'}$('review').hidden=false}
async function poll(){try{let d=await request('/api/jobs/'+job);message(d.message);if(d.status==='extracting'||d.status==='checking'){timer=setTimeout(poll,1500);return}$('extract').disabled=false;$('check').disabled=false;if(d.status==='review'){showClaims(d);if(d.issues.length||d.page_warnings.length)message(d.message+' Extraction/page warnings: '+JSON.stringify([...d.issues,...d.page_warnings]));}if(d.status==='done'){$('rows').replaceChildren();for(let r of d.report.rows){let p=node('div','',$('rows'));p.className='claim';node('blockquote',r.claim.quote,p);node('p',r.status+' · '+(r.result?.verdict||r.reason||''),p);if(r.result?.explanation)node('p',r.result.explanation,p);if(r.result?.sources)node('p','Evidence: '+Object.keys(r.result.sources).join(', '),p)}$('report').replaceChildren();let a=node('a','Open report and source-page links',$('report'));a.href=d.report_url;a.target='_blank';$('results').hidden=false}}catch(e){message(e.message);$('extract').disabled=false;$('check').disabled=false}}
$('check').onclick=async()=>{let approved_ids=[...$('claims').querySelectorAll('input:checked')].map(e=>e.value);if(!approved_ids.length){message('Select at least one reviewed claim.');return}$('check').disabled=true;try{await request('/api/check/'+job,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved_ids,system:$('system').value})});poll()}catch(e){message(e.message);$('check').disabled=false}};
const unitLabels={USD:'USD (dollars)',USD_thousands:'USD (thousands)',USD_millions:'USD (millions)',USD_billions:'USD (billions)',shares:'Shares',shares_millions:'Shares (millions)','USD/shares':'USD per share','%':'Percent','x':'Times','days':'Days'};let offlineCatalog=null;
function outcomeLabel(value){let label=String(value||'Unknown').replaceAll('_',' ');return label[0].toUpperCase()+label.slice(1)}
function modeLabel(value){return {offline_numeric:'Offline numeric check',multi:'Scoped verification',agent:'Single agent',memo:'Memo verification'}[value]||'Saved verification'}
function dateLabel(value){let date=new Date(value);return Number.isNaN(date.getTime())?'Unknown creation time':date.toISOString().slice(0,19).replace('T',' ')+' UTC'}
function suggestedUnit(){if(!offlineCatalog)return;let operation=$('offline-operation').value,items=operation==='ratio'?offlineCatalog.ratios:offlineCatalog.metrics;let selected=items.find(x=>x.name===$('offline-measure').value);let unit=operation==='growth'?'%':selected?.unit;if(operation==='fact'&&unit==='USD')unit='USD_billions';if(operation==='fact'&&unit==='shares')unit='shares_millions';if(unit)$('offline-unit').value=unit}
function measures(){if(!offlineCatalog)return;let operation=$('offline-operation').value,items=operation==='ratio'?offlineCatalog.ratios:offlineCatalog.metrics;$('offline-measure').replaceChildren();for(let item of items){let option=node('option',item.label||item.name.replaceAll('_',' '),$('offline-measure'));option.value=item.name}$('offline-measure').value=operation==='ratio'?'operating_profit_margin':'revenue';suggestedUnit()}
async function loadCatalog(){try{offlineCatalog=await request('/api/catalog');$('coverage').textContent='Cached coverage: '+offlineCatalog.companies.map(c=>c.ticker+' (FY'+c.years.join(', ')+')').join('; ')+'. Missing periods are reported as insufficient evidence.';for(let company of offlineCatalog.companies){let option=node('option',company.ticker,$('cached-companies'));option.value=company.ticker}for(let unit of offlineCatalog.units){let option=node('option',unitLabels[unit]||unit,$('offline-unit'));option.value=unit}measures();$('offline-submit').disabled=false}catch(error){$('coverage').textContent=error.message}}
$('offline-operation').onchange=measures;$('offline-measure').onchange=suggestedUnit;
$('offline').onsubmit=async event=>{event.preventDefault();$('offline-submit').disabled=true;$('offline-message').textContent='Checking cached observations...';$('offline-result').hidden=true;try{let payload=Object.fromEntries(new FormData(event.target));payload.year=Number(payload.year);let saved=await request('/api/offline-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let result=saved.report.result;let box=$('offline-result');box.replaceChildren();node('h3',outcomeLabel(result.verdict),box);node('p',result.verified_value==null?(result.reason||'Evidence is incomplete in the loaded snapshot.'):'Verified '+Number(result.verified_value.toPrecision(10))+' '+result.verified_unit+'; claimed '+payload.claimed_value+' '+payload.unit+'.',box);node('p','Formula: '+(result.formula||'unavailable'),box);node('p','Observations: '+(result.observations||[]).map(o=>o.id+' ['+o.period_start+' to '+o.period_end+']').join('; '),box);if(result.assumptions?.length)node('p','Assumptions: '+result.assumptions.join('; '),box);let link=node('a','Open calculation, source evidence and JSON report',box);link.href=saved.report_url;link.target='_blank';link.rel='noopener';box.hidden=false;$('offline-message').textContent='Saved offline numeric check. Zero model calls.';loadHistory()}catch(error){$('offline-message').textContent=error.message}finally{$('offline-submit').disabled=false}};
async function loadHistory(){try{let data=await request('/api/history');$('history-list').replaceChildren();$('history-message').textContent=data.reports.length?data.reports.length+' saved reports (newest first).':'No saved reports yet.';for(let report of data.reports){let row=node('div','',$('history-list'));row.className='claim';node('p',report.title+' · '+outcomeLabel(report.status)+' · '+modeLabel(report.mode),row);node('p',dateLabel(report.created_at),row);if(report.snapshot_matches_current===false)node('p','Data snapshot changed since this report was created.',row);let button=node('button','Open saved result',row);button.type='button';button.onclick=async()=>{button.disabled=true;try{let opened=await request('/api/history/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:report.id})});let link=node('a','View saved report',row);link.href=opened.report_url;link.target='_blank';link.rel='noopener';button.remove();link.click()}catch(error){$('history-message').textContent=error.message;button.disabled=false}}}}catch(error){$('history-message').textContent=error.message}}
$('history-refresh').onclick=loadHistory;loadCatalog();loadHistory();
</script></html>'''
