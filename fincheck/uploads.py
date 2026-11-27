"""Supplementary PDF evidence: session-local, explicit provenance, no trusted facts."""

import hashlib
from pathlib import Path

from .memo import ingest_pdf
from .retrieval import chunk_passages
from .tools import normalize_company


def supplementary_pdf(path,target,company,year,max_pages=10):
    if not isinstance(company,str) or not company.strip() or type(year) is not int or not 1900<=year<=2100:
        raise ValueError('Supplementary evidence needs an explicit ticker/company and fiscal year')
    memo=ingest_pdf(Path(path),Path(target),max_pages)
    prefix='user-'+memo['source_sha256'][:16]
    blocks=[]
    for page in memo['pages']:
        if not page['text'].strip():continue
        digest=hashlib.sha256(page['text'].encode()).hexdigest()[:16]
        blocks.append(dict(id=f"{prefix}-p{page['page']}-{digest}",doc_id=f"{prefix}-p{page['page']}",
            text=page['text'],company=normalize_company(company),year=year,source_type='user_supplied',
            provenance='User-provided PDF; issuer/authenticity not independently validated.',
            source_sha256=memo['source_sha256'],section=f"Uploaded PDF page {page['page']}",
            page=page['page'],kind='text',block=page['page'],url='',source_name=Path(path).name))
    return chunk_passages(blocks),memo
