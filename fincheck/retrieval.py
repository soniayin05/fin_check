"""Small, inspectable keyword retriever. This is not the final F1 index."""

from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from .tools import normalize_company


class FilingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.table_depth = 0
        self.buffer = []
        self.blocks = []

    def flush(self):
        text = " ".join(" ".join(self.buffer).split())
        if len(text) >= 50:
            self.blocks.append(text)
        self.buffer = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if tag == "table":
            if not self.table_depth:
                self.flush()
            self.table_depth += 1
        elif not self.table_depth and tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.flush()

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
            if not self.table_depth:
                self.flush()
        elif not self.table_depth and tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)


def parse_filing(html: str, url: str) -> list[dict]:
    parser = FilingParser()
    parser.feed(html)
    parser.flush()
    passages = []
    seen = set()
    for position, text in enumerate(parser.blocks):
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        if digest in seen:
            continue
        seen.add(digest)
        passages.append({"id": f"msft-ar2024-{digest}", "company": "MSFT",
                         "year": 2024, "url": url, "block": position,
                         "text": text})
    return passages


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class PassageStore:
    def __init__(self, path: Path):
        self.passages = json.loads(path.read_text()) if path.exists() else []
        self.by_id = {p["id"]: p for p in self.passages}

    def search(self, query: str, company: str = "MSFT", year: int = 2024) -> list[dict]:
        company = normalize_company(company)
        words = set(tokens(query)) - {"the", "and", "of", "in", "a", "is", "was"}
        ranked = []
        for p in self.passages:
            if (p["company"], p["year"]) != (company, year):
                continue
            counts = Counter(tokens(p["text"]))
            # Coverage first; bounded frequency breaks ties without favoring long tables.
            score = sum(1 + min(counts[w], 3) * .1 for w in words if counts[w])
            if score:
                ranked.append((score, p))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [dict(p, score=round(score, 3)) for score, p in ranked[:5]]
