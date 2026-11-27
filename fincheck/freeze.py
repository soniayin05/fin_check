"""Explicit runtime/data/scoring freeze for a later independently authored test set."""

from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .embeddings import DEFAULT_MODEL,DEFAULT_DIM


def configuration(args,passages):
    systems=args.systems.split(',')
    if len(systems)<2 or len(systems)!=len(set(systems)) or set(systems)-{'baseline','agent','multi','multi_prefetch'}:
        raise ValueError('Choose at least two distinct supported systems')
    interval=float(os.getenv('GEMINI_MIN_INTERVAL_SECONDS','1'))
    if not 0<=interval<=60 or not 1<=args.max_calls<=100 or not 1<=args.max_turns<=100:
        raise ValueError('Invalid request spacing or shared budgets')
    return dict(model=os.getenv('GEMINI_MODEL') or 'gemini-3.5-flash-lite',
        retriever=passages.default_method,max_calls=args.max_calls,max_turns=args.max_turns,
        systems=systems,temperature=0,request_interval_seconds=interval,
        embedding_model=os.getenv('GEMINI_EMBED_MODEL') or DEFAULT_MODEL,
        embedding_dim=int(os.getenv('GEMINI_EMBED_DIM') or DEFAULT_DIM))


def snapshot(root,facts_path,config):
    root=Path(root)
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    keys={'model','retriever','max_calls','max_turns','systems','temperature','request_interval_seconds','embedding_model','embedding_dim'}
    settings={key:config[key] for key in keys}
    inputs=dict(facts=sha(Path(facts_path)),passage_index=sha(root/'data/indexes/passages.json'),
                archive_manifest=sha(root/'data/raw/manifest.json'))
    if not inputs['facts'] or not inputs['passage_index']:raise ValueError('Prepare facts and the filing index before freezing')
    return dict(kind='fincheck-evaluation-freeze',version=1,configuration=settings,inputs=inputs,
                code_sha256={p.name:sha(p) for p in sorted((root/'fincheck').glob('*.py'))})


def verify(root,facts_path,config,path):
    frozen=json.loads(Path(path).read_text())
    current=snapshot(root,facts_path,config)
    if any(frozen.get(key)!=value for key,value in current.items()):
        raise ValueError('Frozen code, data or runtime settings changed; do not tune an opened held-out evaluation')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_freeze(root,facts_path,config):
    from .memo import artifact_dir,write_json
    state=subprocess.run(['git','status','--porcelain','--untracked-files=all','--','fincheck'],cwd=root,capture_output=True,text=True)
    if state.returncode or state.stdout.strip():raise ValueError('Commit source changes before freezing the evaluation')
    result=snapshot(root,facts_path,config)
    commit=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True)
    result.update(created_at=datetime.now(timezone.utc).isoformat(),git_commit=commit.stdout.strip(),
                  note='Gold must be independently authored. This records a freeze, not completed evaluation or human review.')
    target=artifact_dir(Path(root),'evaluation-freeze')/'freeze.json';write_json(target,result)
    return target
