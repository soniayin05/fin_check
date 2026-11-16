"""Minimal SEC EDGAR client: declared User-Agent, throttling, bounded retries.

SEC fair-access rules: declare a User-Agent with a contact email and stay under
10 requests/second (https://www.sec.gov/os/accessing-edgar-data).
"""

import gzip
import os
import time
import urllib.error
import urllib.request

# Supported universe. CIKs are re-checked against SEC's ticker file at download time.
COMPANIES = {
    "MSFT": {"cik": 789019, "name": "Microsoft Corporation"},
    "GOOGL": {"cik": 1652044, "name": "Alphabet Inc."},
    "AAPL": {"cik": 320193, "name": "Apple Inc."},
}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class SecError(RuntimeError):
    """SEC access failed. This is an execution failure, never evidence about a claim."""


def facts_url(cik: int) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"


def submissions_url(cik: int) -> str:
    return f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"


def archive_url(cik: int, accession: str, name: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"


class SecClient:
    def __init__(self, user_agent: str | None = None, min_interval: float = 0.2, retry_wait: float = 2.0):
        agent = (os.getenv("SEC_USER_AGENT", "") if user_agent is None else user_agent).strip()
        if "@" not in agent or len(agent.split()) < 2:
            raise SecError(
                "Set SEC_USER_AGENT in .env to an app or person name plus a contact email, "
                "for example: SEC_USER_AGENT=FinCheck Jane Doe jane@example.edu")
        self.user_agent, self.min_interval, self.retry_wait = agent, min_interval, retry_wait
        self._last = 0.0
        self.requests = 0

    def get(self, url: str) -> bytes:
        for attempt in range(3):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            request = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent, "Accept-Encoding": "gzip"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        payload = gzip.decompress(payload)
                    return payload
            except urllib.error.HTTPError as error:
                if error.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(self.retry_wait * (attempt + 1))
                    continue
                hint = (" Check that SEC_USER_AGENT contains a real contact email; if you sent many "
                        "requests, wait ten minutes before retrying.") if error.code == 403 else ""
                raise SecError(f"SEC HTTP {error.code} for {url}.{hint}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    time.sleep(self.retry_wait)
                    continue
                raise SecError(f"SEC connection failed for {url}") from None
        raise SecError(f"SEC request failed for {url}")
