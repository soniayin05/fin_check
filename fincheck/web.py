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
from .manual_memo import check_reviewed_memo, save_revision
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
            return deepcopy({k:v for k,v in job.items() if k not in {'passages','memo','bundle','directory','allowed'}})

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
        mode = fields.get('mode', 'gemini')
        if mode not in ('gemini', 'manual'):
            raise ValueError('Choose manual review or Gemini extraction')
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
            self.jobs[identifier]=dict(id=identifier,artifact=directory.name,status='extracting',message='Preparing PDF pages...',
                directory=directory,allowed=[directory],bundle=None,claims=[],issues=[],page_warnings=[],mode=mode,revision=0)
            self.busy=True
        def work():
            try:
                path=directory/'uploaded-memo.pdf';path.write_bytes(pdf[1])
                memo=ingest_pdf(path,directory,max_pages=10)
                context={}
                if fields.get('context_company'):context['company']=fields['context_company']
                if fields.get('context_year'):context['fiscal_year']=int(fields['context_year'])
                if mode == 'manual':
                    bundle = dict(schema_version=1, memo_sha256=memo['source_sha256'], context=context,
                                  claims=[], issues=[], model_calls=0, status='needs_review',
                                  claim_limit=12, revision=0, edits=[])
                else:
                    bundle=extract_claims(memo,Gemini(),directory,context,max_claims=12,model_budget=10)
                passages=PassageStore(self.root/'data/indexes/passages.json')
                if isinstance(extra,tuple) and extra[1]:
                    source=directory/'uploaded-evidence.pdf';source.write_bytes(extra[1])
                    added,source_memo=supplementary_pdf(source,directory/'evidence',fields['evidence_company'],int(fields['evidence_year']))
                    passages.passages=passages.passages+added
                    bundle['supplementary_evidence']=dict(source_sha256=source_memo['source_sha256'],provenance='user_supplied')
                write_json(directory/'claims.json',bundle)
                with self.lock:
                    self.jobs[identifier].update(status='review',message='Review the PDF and confirm the claims you select.',
                        bundle=bundle,claims=bundle['claims'],issues=bundle['issues'],
                        memo=memo,pages=[{key:p[key] for key in ('page','image','text_file','warnings')} for p in memo['pages']],
                        page_warnings=[dict(page=p['page'],warnings=p['warnings']) for p in memo['pages'] if p['warnings']],
                        passages=passages)
            except Exception as error:self.update(identifier,status='error',message=str(error))
            finally:
                with self.lock:self.busy=False
        self.executor.submit(work)
        return identifier

    def edit_claim(self, identifier, payload):
        with self.lock:
            job = self.jobs.get(identifier)
            if self.busy or not job or job['status'] not in {'review', 'done'} or job.get('bundle') is None:
                raise ValueError('Open a memo that is ready for editing')
            bundle = save_revision(job['directory'], job['memo'], job['bundle'], payload)
            job.update(status='review', bundle=bundle, claims=bundle['claims'], revision=bundle['revision'],
                       message='Claim saved. Review and select it again before checking.')
            job.pop('report', None)
            job.pop('report_url', None)
        return self.snapshot(identifier)

    def check(self,identifier,payload):
        if not isinstance(payload,dict):raise ValueError('Supply an approval object')
        approved=payload.get('approved_ids',[]);system=payload.get('system','multi')
        if not isinstance(approved,list) or any(not isinstance(x,str) for x in approved) or system not in ('agent','multi','manual_offline'):
            raise ValueError('Invalid approved claims/system')
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job['status'] not in {'review','done'} or job.get('bundle') is None:
                raise ValueError('Select a freshly extracted memo before checking')
            if self.busy:raise ValueError('A task is already running')
            bundle=deepcopy(job['bundle']);directory=job['directory'];passages=job['passages']
            revision = payload.get('revision', 0)
            if type(revision) is not int or revision != job['revision']:
                raise ValueError('The claims changed. Review the latest version before checking.')
            if system == 'manual_offline' and payload.get('confirmed_scope') is not True:
                raise ValueError('Confirm the quotation, scope, value and unit against the PDF first')
            memo = job['memo']
            if not approved or set(approved)-{c['claim_id'] for c in bundle['claims']}:
                raise ValueError('Select existing claims whose quotations and scope you reviewed')
            for c in bundle['claims']:c['approved']=c['claim_id'] in approved
            self.busy=True;job.update(status='checking',message='Checking reviewed claims...')
        def work():
            try:
                if system == 'manual_offline':
                    target, report = check_reviewed_memo(self.root, directory, memo, bundle, set(approved))
                else:
                    path=directory/('approved-'+secrets.token_hex(6)+'.json');write_json(path,bundle)
                    facts_path=self.root/'data/facts/observations.json'
                    if not facts_path.exists():facts_path=self.root/'data/sample/financial_facts.json'
                    target,report=check_claims(self.root,path,FactStore(facts_path),passages,Gemini(),facts_path,
                        model_budget=40,tool_budget=60,max_calls=12,max_turns=12,system=system)
                with self.lock:self.jobs[identifier]['allowed'].append(target)
                self.update(identifier,status='done',message='Review the results and remaining issues.',report=report,
                    report_url=f'/assets/{identifier}/{target.name}/report.html')
            except Exception as error:self.update(identifier,status='review',message=str(error))
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
                elif self.path.startswith('/api/claims/'):
                    self.respond(200, application.edit_claim(self.path.rsplit('/',1)[1], json.loads(body)))
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


HTML=r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinCheck</title><style>
body{margin:0;background:#f6f8fb;color:#17283d;font:16px system-ui}main{max-width:1000px;margin:48px auto;padding:0 24px}h1{font-size:36px;margin-bottom:8px}
.card{background:white;border:1px solid #dbe3ee;border-radius:12px;padding:24px;margin:22px 0}label{display:block;margin:14px 0 5px}input,select,button{font:inherit;padding:9px;max-width:100%;box-sizing:border-box}button{border:0;border-radius:6px;background:#155ac0;color:white;cursor:pointer;margin-top:18px}button:disabled{opacity:.5;cursor:wait}.hint{color:#52657a;font-size:14px}blockquote{white-space:pre-wrap;margin:12px 0;padding:12px;background:#f1f5fa}.claim{border-top:1px solid #dbe3ee;padding:18px 0}.claim label{display:flex;gap:12px}.status{font-weight:600}a{color:#155ac0}.error{color:#a12727}
textarea{font:inherit;width:100%;box-sizing:border-box;padding:9px}.memo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px;margin:16px 0}.memo-grid img{display:block;width:100%;max-height:34rem;object-fit:contain;background:#f1f5fa}.memo-fields{display:flex;flex-wrap:wrap;gap:0 14px}.memo-fields>div{flex:1 1 14rem}.secondary{background:#e9f1ff;color:#154579;margin-right:12px}.page-text{font:14px ui-monospace,monospace}
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
<h2>PDF memo workflow</h2>
<form id="upload" class="card"><h2>Open a memo</h2><label for="memo-file">Memo PDF (up to 10 pages, 20 MiB)</label><input id="memo-file" type="file" name="memo" accept="application/pdf" required>
<label for="upload-mode">Claim entry</label><select id="upload-mode" name="mode"><option value="manual">Enter claims manually (offline)</option><option value="gemini">Extract claims with Gemini</option></select>
<p class="hint">Manual entry keeps processing on this computer. Gemini extraction sends page text to the configured API.</p>
<details><summary>Optional context and supplementary evidence</summary><label>Memo company/ticker hint</label><input name="context_company" placeholder="e.g. MSFT"><label>Memo fiscal-year hint</label><input name="context_year" type="number" placeholder="2024">
<label>Supplementary source PDF</label><input type="file" name="evidence" accept="application/pdf"><label>Source company/ticker</label><input name="evidence_company" placeholder="Required if attaching evidence"><label>Source fiscal year</label><input name="evidence_year" type="number" placeholder="2024"><p class="hint">Uploaded sources are marked user supplied; they do not become trusted SEC observations or independent corroboration.</p></details>
<button id="extract">Open PDF</button></form><p id="message" role="status"></p>
<section id="review" class="card" hidden><h2>Review the PDF and claims</h2>
<label for="memo-page">PDF page</label><select id="memo-page"></select> <a id="page-link" target="_blank" rel="noopener">Open full page image</a>
<p id="page-warning" class="hint" role="status"></p>
<div class="memo-grid"><img id="page-image" alt="Memo page preview"><div>
<label for="page-text">Extracted page text</label><textarea id="page-text" class="page-text" rows="12" readonly></textarea>
<button id="copy-quote" class="secondary" type="button">Use selected lines as quote</button>
</div></div>
<form id="claim-editor">
<h3 id="editor-title">Add a claim</h3>
<label for="claim-quote">Exact quotation (include complete lines and qualifiers)</label><textarea id="claim-quote" name="quote" rows="4" maxlength="4000" required></textarea>
<div class="memo-fields">
<div><label for="claim-company">Company or ticker</label><input id="claim-company" name="company" list="cached-companies" maxlength="160"></div>
<div><label for="claim-period">Period</label><input id="claim-period" name="period" placeholder="FY2024" maxlength="160"></div>
<div><label for="claim-type">Claim type</label><select id="claim-type" name="claim_type"><option value="numeric">Numeric</option><option value="comparison">Comparison</option><option value="disclosure">Disclosure</option><option value="forecast">Forecast</option></select></div>
<div><label for="claim-operation">Offline calculation</label><select id="claim-operation" name="operation"><option value="">Leave for review</option><option value="fact">Annual reported value</option><option value="growth">Year-over-year growth</option><option value="ratio">Annual ratio</option></select></div>
<div><label for="claim-metric">Metric or ratio</label><input id="claim-metric" name="metric" list="memo-measures" maxlength="160"><datalist id="memo-measures"></datalist></div>
<div><label for="claim-value">Value as written</label><input id="claim-value" name="value" maxlength="64" placeholder="245.1"></div>
<div><label for="claim-unit">Unit</label><input id="claim-unit" name="unit" list="memo-units" maxlength="160"><datalist id="memo-units"></datalist></div>
</div><label for="claim-qualifiers">Qualifiers copied from the quote (one per line)</label><textarea id="claim-qualifiers" name="qualifiers" rows="2"></textarea>
<button id="save-claim">Save claim</button> <button id="cancel-edit" class="secondary" type="button" hidden>Cancel edit</button>
<p id="editor-message" role="status"></p></form>
<h3>Saved claims</h3><p><a id="claim-history" target="_blank" rel="noopener">View claim file and edit history</a></p><div id="claims"></div>
<label for="system">Verification mode</label><select id="system"><option value="manual_offline">Cached numeric check (offline)</option><option value="multi">Access-scoped review with Gemini</option><option value="agent">Single agent with Gemini</option></select>
<label id="scope-label"><input type="checkbox" id="confirmed-scope"> I reviewed the selected quotes and their fields. Any numeric claim I ask the offline checker to calculate is historical, annual and company-wide.</label>
<button id="check">Check selected claims</button></section>
<section id="results" class="card" hidden><h2>Results</h2><div id="rows"></div><p id="report"></p></section>
<section class="card"><h2>Saved reports</h2><p class="hint">Open previous results without extraction, verification or model calls. Saved reports keep their original data snapshot.</p><button id="history-refresh" type="button">Refresh reports</button><p id="history-message" role="status"></p><div id="history-list"></div></section>
<p class="hint">Built-in SEC coverage: Microsoft, Apple and Alphabet. Missing evidence, execution failures and review flags are reported separately. This tool checks factual consistency; it does not recommend investments.</p></main>
<script>
const token='__TOKEN__';let job=null, memoState=null, editId=null;const $=id=>document.getElementById(id);
function message(s){$('message').textContent=s}function node(tag,text,parent){let e=document.createElement(tag);e.textContent=text;parent.append(e);return e}
async function request(url,options={}){options.headers={...(options.headers||{}),'X-Fincheck-Token':token};let r=await fetch(url,options);let data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data}
function memoBusy(value) {
  for (const id of ['extract','check','save-claim']) $(id).disabled=value;
}
function jsonPost(url, value) {
  return request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
}
$('upload').onsubmit=async event=>{
  event.preventDefault();
  memoBusy(true);
  message('Opening PDF...');
  try {
    const data=await request('/api/upload',{method:'POST',body:new FormData(event.target)});
    job=data.id;
    memoState=null;
    $('review').hidden=true;
    $('results').hidden=true;
    poll();
  } catch(error) { message(error.message); memoBusy(false); }
};
function memoAsset(path) { return '/assets/'+job+'/'+memoState.artifact+'/'+path; }
async function previewPage() {
  const page=memoState.pages.find(p=>p.page===Number($('memo-page').value));
  if (!page) return;
  const textUrl=memoAsset(page.text_file);
  const isCurrent=() => memoState && textUrl===memoAsset(page.text_file) && Number($('memo-page').value)===page.page;
  $('page-link').href=$('page-image').src=memoAsset(page.image);
  $('page-image').alt='Memo page '+page.page;
  $('page-warning').textContent=page.warnings.map(outcomeLabel).join('. ');
  $('page-text').value='';
  try {
    const response=await fetch(textUrl);
    if (!response.ok) throw Error('Page text is unavailable.');
    const text=await response.text();
    if (isCurrent()) $('page-text').value=text;
  } catch(error) { if (isCurrent()) $('page-warning').textContent=error.message; }
}
function resetEditor() {
  editId=null;
  $('claim-editor').reset();
  $('editor-title').textContent='Add a claim';
  $('cancel-edit').hidden=true;
  $('editor-message').textContent='';
}
$('cancel-edit').onclick=resetEditor;
$('memo-page').onchange=()=>{
  resetEditor();
  previewPage();
};
$('copy-quote').onclick=()=>{
  const text=$('page-text');
  if (text.selectionStart===text.selectionEnd) {
    $('editor-message').textContent='Select the source text you want to quote first.';
    return;
  }
  const start=text.value.lastIndexOf('\n',text.selectionStart-1)+1;
  const end=text.value.indexOf('\n',text.selectionEnd-1);
  $('claim-quote').value=text.value.slice(start,end<0?undefined:end).trim();
  $('editor-message').textContent='Copied complete lines. Check the quote against the page.';
  $('claim-quote').focus();
};
function editClaim(claim) {
  resetEditor();
  editId=claim.claim_id;
  $('memo-page').value=claim.page;
  previewPage();
  $('editor-title').textContent='Edit claim on page '+claim.page;
  $('cancel-edit').hidden=false;
  for (const key of ['quote','company','period','metric','value','unit','claim_type','operation']) {
    $('claim-editor').elements.namedItem(key).value=claim[key]??'';
  }
  $('claim-operation').onchange();
  $('claim-qualifiers').value=claim.qualifiers.join('\n');
  $('claim-quote').focus();
}
$('claim-editor').onsubmit=async event=>{
  event.preventDefault();
  memoBusy(true);
  const fields=Object.fromEntries(new FormData(event.target));
  Object.assign(fields,{page:Number($('memo-page').value),replace_id:editId,revision:memoState.revision,
    value:fields.value.trim()||null,operation:fields.operation||null,qualifiers:fields.qualifiers.split('\n').map(v=>v.trim()).filter(Boolean)});
  try {
    const data=await jsonPost('/api/claims/'+job,fields);
    resetEditor();
    showClaims(data);
    $('results').hidden=true;
    $('editor-message').textContent=data.message;
  } catch(error) { $('editor-message').textContent=error.message; }
  finally { memoBusy(false); }
};
function showClaims(data) {
  const firstView=!memoState;
  memoState=data;
  if (firstView) {
    resetEditor();
    $('memo-page').replaceChildren();
    for (const page of data.pages) node('option','Page '+page.page,$('memo-page')).value=page.page;
    $('claim-history').href=memoAsset('claims.json');
    $('system').value=data.mode==='manual'?'manual_offline':'multi';
    $('system').onchange();
    previewPage();
  }
  $('confirmed-scope').checked=false;
  const list=$('claims');
  list.replaceChildren();
  if (!data.claims.length) node('p','Use the page text above to add a quotation.',list);
  for (const claim of data.claims) {
    const box=node('div','',list);
    box.className='claim';
    const label=node('label','',box);
    const input=document.createElement('input');
    input.type='checkbox';
    input.value=claim.claim_id;
    input.onchange=()=>{$('confirmed-scope').checked=false;};
    label.append(input);
    node('span','Page '+claim.page+' · '+[claim.company,claim.period].filter(Boolean).join(' · '),label);
    node('blockquote',claim.quote,box);
    node('p',[claim.metric,claim.value,claim.unit,claim.operation&&outcomeLabel(claim.operation)].filter(Boolean).join(' · '),box);
    if (claim.review_reasons.length) node('p','Needs review · '+claim.review_reasons.map(outcomeLabel).join(', '),box);
    const edit=node('button','Edit claim',box);
    edit.className='secondary';
    edit.onclick=()=>editClaim(claim);
  }
  $('review').hidden=false;
}
function showMemoReport(data) {
  const rows=$('rows');
  rows.replaceChildren();
  if (data.report.system==='manual_offline') node('p','Cached numeric check · Zero model calls',rows);
  for (const row of data.report.rows) {
    const box=node('div','',rows);
    box.className='claim';
    node('blockquote',row.claim.quote,box);
    node('p',outcomeLabel(row.result?.verdict||row.status),box);
    if (row.result?.verified_value!=null) node('p','Verified '+row.result.verified_value+' '+row.result.verified_unit,box);
    if (row.reason||row.result?.reason||row.result?.explanation) node('p',row.reason||row.result.reason||row.result.explanation,box);
    if (row.result?.sources) node('p','Evidence: '+Object.keys(row.result.sources).join(', '),box);
  }
  $('report').replaceChildren();
  const link=node('a','Open report and source-page links',$('report'));
  link.href=data.report_url;
  link.target='_blank';
  link.rel='noopener';
  $('results').hidden=false;
  loadHistory();
}
async function poll() {
  try {
    const data=await request('/api/jobs/'+job);
    message(data.message);
    if (data.status==='extracting'||data.status==='checking') { setTimeout(poll,500); return; }
    if (data.status==='review'||data.status==='done') showClaims(data);
    if (data.issues.length) message(data.message+' Extraction issues: '+data.issues.map(i=>'Page '+i.page+' '+i.reason).join('. '));
    if (data.status==='done') showMemoReport(data);
  } catch(error) { message(error.message); }
  memoBusy(false);
}
$('system').onchange=()=>{
  $('scope-label').hidden=$('system').value!=='manual_offline';
  $('confirmed-scope').checked=false;
};
$('check').onclick=async()=>{
  if (editId!==null) { message('Save or cancel the current edit before checking.'); return; }
  const approved=[...$('claims').querySelectorAll('input:checked')].map(input=>input.value);
  const system=$('system').value;
  const confirmed=$('confirmed-scope').checked;
  if (!approved.length) { message('Select at least one reviewed claim.'); return; }
  if (system==='manual_offline'&&!confirmed) { message('Confirm the quotation and scope before checking.'); return; }
  memoBusy(true);
  $('results').hidden=true;
  try {
    await jsonPost('/api/check/'+job,{approved_ids:approved,system,confirmed_scope:confirmed,revision:memoState.revision});
    poll();
  } catch(error) { message(error.message); memoBusy(false); }
};
const unitLabels={USD:'USD (dollars)',USD_thousands:'USD (thousands)',USD_millions:'USD (millions)',USD_billions:'USD (billions)',shares:'Shares',shares_millions:'Shares (millions)','USD/shares':'USD per share','%':'Percent','x':'Times','days':'Days'};let offlineCatalog=null;
function outcomeLabel(value){let label=String(value||'Unknown').replaceAll('_',' ');return label[0].toUpperCase()+label.slice(1)}
function modeLabel(value){return {offline_numeric:'Offline numeric check',manual_offline:'Offline memo check',multi:'Scoped verification',agent:'Single agent',memo:'Memo verification'}[value]||'Saved verification'}
function dateLabel(value){let date=new Date(value);return Number.isNaN(date.getTime())?'Unknown creation time':date.toISOString().slice(0,19).replace('T',' ')+' UTC'}
function suggestedUnit(){if(!offlineCatalog)return;let operation=$('offline-operation').value,items=operation==='ratio'?offlineCatalog.ratios:offlineCatalog.metrics;let selected=items.find(x=>x.name===$('offline-measure').value);let unit=operation==='growth'?'%':selected?.unit;if(operation==='fact'&&unit==='USD')unit='USD_billions';if(operation==='fact'&&unit==='shares')unit='shares_millions';if(unit)$('offline-unit').value=unit}
function measures(){if(!offlineCatalog)return;let operation=$('offline-operation').value,items=operation==='ratio'?offlineCatalog.ratios:offlineCatalog.metrics;$('offline-measure').replaceChildren();for(let item of items){let option=node('option',item.label||item.name.replaceAll('_',' '),$('offline-measure'));option.value=item.name}$('offline-measure').value=operation==='ratio'?'operating_profit_margin':'revenue';suggestedUnit()}
async function loadCatalog(){try{offlineCatalog=await request('/api/catalog');$('coverage').textContent='Cached coverage: '+offlineCatalog.companies.map(c=>c.ticker+' (FY'+c.years.join(', ')+')').join('; ')+'. Missing periods are reported as insufficient evidence.';for(let company of offlineCatalog.companies){let option=node('option',company.ticker,$('cached-companies'));option.value=company.ticker}for(let unit of offlineCatalog.units){let option=node('option',unitLabels[unit]||unit,$('offline-unit'));option.value=unit}measures();$('offline-submit').disabled=false}catch(error){$('coverage').textContent=error.message}}
$('offline-operation').onchange=measures;$('offline-measure').onchange=suggestedUnit;
$('offline').onsubmit=async event=>{event.preventDefault();$('offline-submit').disabled=true;$('offline-message').textContent='Checking cached observations...';$('offline-result').hidden=true;try{let payload=Object.fromEntries(new FormData(event.target));payload.year=Number(payload.year);let saved=await request('/api/offline-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let result=saved.report.result;let box=$('offline-result');box.replaceChildren();node('h3',outcomeLabel(result.verdict),box);node('p',result.verified_value==null?(result.reason||'Evidence is incomplete in the loaded snapshot.'):'Verified '+Number(result.verified_value.toPrecision(10))+' '+result.verified_unit+'; claimed '+payload.claimed_value+' '+payload.unit+'.',box);node('p','Formula: '+(result.formula||'unavailable'),box);node('p','Observations: '+(result.observations||[]).map(o=>o.id+' ['+o.period_start+' to '+o.period_end+']').join('; '),box);if(result.assumptions?.length)node('p','Assumptions: '+result.assumptions.join('; '),box);let link=node('a','Open calculation, source evidence and JSON report',box);link.href=saved.report_url;link.target='_blank';link.rel='noopener';box.hidden=false;$('offline-message').textContent='Saved offline numeric check. Zero model calls.';loadHistory()}catch(error){$('offline-message').textContent=error.message}finally{$('offline-submit').disabled=false}};
async function loadHistory(){try{let data=await request('/api/history');$('history-list').replaceChildren();$('history-message').textContent=data.reports.length?data.reports.length+' saved reports (newest first).':'No saved reports yet.';for(let report of data.reports){let row=node('div','',$('history-list'));row.className='claim';node('p',report.title+' · '+outcomeLabel(report.status)+' · '+modeLabel(report.mode),row);node('p',dateLabel(report.created_at),row);if(report.snapshot_matches_current===false)node('p','Data snapshot changed since this report was created.',row);let button=node('button','Open saved result',row);button.type='button';button.onclick=async()=>{button.disabled=true;try{let opened=await request('/api/history/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:report.id})});let link=node('a','View saved report',row);link.href=opened.report_url;link.target='_blank';link.rel='noopener';button.remove();link.click()}catch(error){$('history-message').textContent=error.message;button.disabled=false}}}}catch(error){$('history-message').textContent=error.message}}
$('history-refresh').onclick=loadHistory;loadCatalog();loadHistory();
for (const [unit,label] of Object.entries(unitLabels)) node('option',label,$('memo-units')).value=unit;
$('claim-operation').onchange=()=>{
  const list=$('memo-measures');
  list.replaceChildren();
  const items=$('claim-operation').value==='ratio'?offlineCatalog?.ratios:offlineCatalog?.metrics;
  for (const item of items||[]) node('option',item.label||outcomeLabel(item.name),list).value=item.name;
};
</script></html>'''
