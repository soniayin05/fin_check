"""Search local traces and export content-free diagnostic summaries."""

import hashlib
import json
import math
from pathlib import Path
import re

from .memo import artifact_dir, write_json
from .agent import TOOLS
from .workflow import CAPABILITIES

SAFE_TOOLS={t['name'] for t in TOOLS}|set().union(*CAPABILITIES.values())
SAFE_STATUS={'complete','incomplete','error','ok','needs_review','not_run'}
def number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None
def usage(value):
    keys={'promptTokenCount','candidatesTokenCount','totalTokenCount','thoughtsTokenCount','cachedContentTokenCount'}
    return {k:v for k,v in (value or {}).items() if k in keys and type(v) is int and v>=0} if isinstance(value,dict) else None


def summaries(root,scope=None,tool=None,status=None,claim_filter=None):
    rows=[]
    paths=sorted((Path(root)/'artifacts/local').glob('*/trace.json'))
    paths+=sorted((Path(root)/'artifacts/local').glob('*/extraction_trace.json'))
    for path in paths:
        try:run=json.loads(path.read_text())
        except (OSError,ValueError):continue
        if status and run.get('status')!=status:continue
        if claim_filter and claim_filter.lower() not in run.get('claim','').lower():continue
        events=[e for e in run.get('events',[]) if (not scope or e.get('scope')==scope)
                and (not tool or e.get('name')==tool)]
        if (scope or tool) and not events:continue
        model=run.get('model','')
        rows.append(dict(path=str(path.relative_to(root)),status=run.get('status') if run.get('status') in SAFE_STATUS else None,
            model=model if isinstance(model,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',model) else None,
            claim_sha256=hashlib.sha256(run.get('claim','').encode()).hexdigest(),
            model_calls=sum(e.get('kind')=='model' for e in events),
            tool_calls=sum(e.get('charged_calls',1) for e in events if e.get('kind')=='tool'),
            errors=sum(e.get('status')=='error' for e in events),
            per_step=[dict(kind=e.get('kind') if e.get('kind') in {'model','tool','handoff','retry','replan','fault','finalize'} else 'other',
                scope=e.get('scope') if e.get('scope') in set(CAPABILITIES)|{'controller','single'} else 'single',
                tool=e.get('name') if e.get('name') in SAFE_TOOLS else None,
                status=e.get('status') if e.get('status') in SAFE_STATUS else None,
                latency_seconds=number(e.get('latency_seconds')),usage=usage((e.get('output') or {}).get('usage')) if e.get('kind')=='model' else None)
                for e in events],latency_seconds=number(run.get('latency_seconds'))))
    return rows


def export_summaries(root,rows):
    """No raw claims, prompts, model text, source text, URLs or local filenames."""
    clean=[]
    for row in rows:
        copy={k:v for k,v in row.items() if k!='path'}
        copy['trace_sha256']=hashlib.sha256((Path(root)/row['path']).read_bytes()).hexdigest()
        clean.append(copy)
    target=artifact_dir(root,'trace-export')
    write_json(target/'diagnostics.json',dict(traces=clean,redaction='Content removed; only hashes, counters, model IDs and timing/usage metadata remain.'))
    return target/'diagnostics.json'
