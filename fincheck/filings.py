"""Choose which SEC filings to archive. Pure functions over submissions JSON."""

from datetime import date, timedelta
import re

EXHIBIT_991 = re.compile(r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", re.IGNORECASE)


def filing_rows(columns: dict) -> list[dict]:
    """Submissions JSON stores filings as parallel arrays; turn them into rows."""
    keys = list(columns)
    return [dict(zip(keys, values)) for values in zip(*(columns[key] for key in keys))]


def select_annual_reports(rows: list[dict], count: int = 3) -> list[dict]:
    """Most recent `count` original 10-K filings, newest first, one per report date."""
    chosen: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("filingDate", "")):
        if row.get("form") == "10-K" and row.get("reportDate") and row.get("primaryDocument"):
            chosen.setdefault(row["reportDate"], row)  # keep the original, not a later duplicate
    return [chosen[key] for key in sorted(chosen, reverse=True)[:count]]


def select_earnings_release(rows: list[dict], period_end: str, window_days: int = 75) -> dict | None:
    """First 8-K with Item 2.02 (results of operations) filed after the fiscal year-end."""
    start = date.fromisoformat(period_end)
    limit = (start + timedelta(days=window_days)).isoformat()
    matches = [row for row in rows
               if row.get("form") == "8-K"
               and "2.02" in [item.strip() for item in str(row.get("items", "")).split(",")]
               and period_end < row.get("filingDate", "") <= limit]
    return min(matches, key=lambda r: r["filingDate"]) if matches else None


def pick_exhibit(index: dict, primary_document: str = "") -> str | None:
    """Find the EX-99.1 press release in an EDGAR filing index. No match means None, never a guess."""
    names = [item.get("name", "") for item in index.get("directory", {}).get("item", [])]
    candidates = sorted(name for name in names
                        if name.lower().endswith((".htm", ".html"))
                        and name != primary_document and EXHIBIT_991.search(name))
    return candidates[0] if candidates else None
