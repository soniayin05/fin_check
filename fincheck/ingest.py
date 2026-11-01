"""Archive a public annual report and build a reproducible local passage index."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import urllib.request

from .retrieval import parse_filing

REPORT_URL = "https://www.microsoft.com/investor/reports/ar24/index.html"
SEC_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"


def prepare(root: Path, include_sec: bool = False) -> dict:
    raw = root / "data/raw"
    index = root / "data/indexes"
    raw.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "manifest.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    sources = [("msft-2024.html", REPORT_URL)]
    if include_sec:
        sources.append(("sec-companyfacts.json", SEC_URL))
    manifest = {}
    for name, url in sources:
        target = raw / name
        cached = target.exists()
        if not cached:
            request = urllib.request.Request(url, headers={"User-Agent": os.getenv(
                "SEC_USER_AGENT") or "FinCheck academic project https://github.com/soniayin05/fin_check"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
            if name.endswith(".json"):
                json.loads(payload)
            elif b"microsoft" not in payload.lower() or b"2024" not in payload:
                raise ValueError("Downloaded page does not look like the Microsoft 2024 report")
            target.write_bytes(payload)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        old = previous.get(name, {})
        manifest[name] = {"url": url, "sha256": digest,
                          "retrieved_at": (old.get("retrieved_at") if cached else
                                           datetime.now(timezone.utc).isoformat()),
                          "cache_used": cached,
                          "note": "Unknown retrieval time for pre-existing files" if cached and not old else ""}
    passages = parse_filing((raw / "msft-2024.html").read_text(), REPORT_URL)
    if len(passages) < 20:
        raise ValueError("Too few filing passages; inspect downloaded HTML")
    (index / "passages.json").write_text(json.dumps(passages, indent=2) + "\n")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"passages": len(passages), "manifest": str(manifest_path),
            "sec_downloaded": "sec-companyfacts.json" in manifest,
            "note": "SEC normalization is not implemented; numeric tool uses the reviewed sample extract."}
