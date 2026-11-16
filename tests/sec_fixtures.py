"""Synthetic SEC payloads that mirror the real JSON shapes. Test-only; not filing data,
except the two MSFT revenue and cash-flow figures, which match data/sample."""

FY = {2021: ("2020-07-01", "2021-06-30"), 2022: ("2021-07-01", "2022-06-30"),
      2023: ("2022-07-01", "2023-06-30"), 2024: ("2023-07-01", "2024-06-30")}
FILED = {2021: "2021-07-29", 2022: "2022-07-28", 2023: "2023-07-27", 2024: "2024-07-30"}


def accn(year: int) -> str:
    return f"0000950170-{str(year)[2:]}-000001"


def flow(year: int, val, reported_in: int | None = None, form: str = "10-K") -> dict:
    """An annual duration. `reported_in` is the filing year; note fy follows the FILING."""
    filing = reported_in or year
    start, end = FY[year]
    return {"start": start, "end": end, "val": val, "accn": accn(filing), "fy": filing, "fp": "FY",
            "form": form, "filed": FILED[filing]}


def balance(year: int, val, reported_in: int | None = None) -> dict:
    filing = reported_in or year
    return {"end": FY[year][1], "val": val, "accn": accn(filing), "fy": filing, "fp": "FY",
            "form": "10-K", "filed": FILED[filing]}


def concept(rows: list[dict], unit: str = "USD") -> dict:
    return {"label": "x", "description": "x", "units": {unit: rows}}


def companyfacts() -> dict:
    gaap = {
        # Tag switch: older years under Revenues, newer under the ASC 606 tag.
        "Revenues": concept([flow(2021, 168088e6), flow(2022, 198270e6)]),
        "RevenueFromContractWithCustomerExcludingAssessedTax": concept([
            flow(2023, 211915e6), flow(2023, 211915e6, reported_in=2024),  # comparative: fy says 2024
            flow(2024, 245122e6),
            {"start": "2023-10-01", "end": "2024-06-30", "val": 180000e6, "accn": accn(2024), "fy": 2024,
             "fp": "FY", "form": "10-K", "filed": FILED[2024]},            # nine months inside a 10-K
            {"start": "2024-04-01", "end": "2024-06-30", "val": 64727e6, "accn": "0000950170-24-000009",
             "fy": 2024, "fp": "Q4", "form": "10-Q", "filed": "2024-07-30"},
        ]),
        "NetIncomeLoss": concept([
            flow(2022, 100e6), flow(2022, 90e6, reported_in=2023),          # restated downwards
            flow(2023, 72361e6), flow(2024, 88136e6)]),
        "OperatingIncomeLoss": concept([
            flow(2023, 88523e6), flow(2024, 109433e6), flow(2024, 109000e6)]),  # same filing, two values
        "NetCashProvidedByUsedInOperatingActivities": concept([flow(2023, 87582e6), flow(2024, 118548e6)]),
        "EarningsPerShareDiluted": concept([flow(2023, 9.68), flow(2024, 11.80)], "USD/shares"),
        "Assets": concept([
            balance(2023, 411976e6), balance(2023, 411976e6, reported_in=2024), balance(2024, 512163e6),
            {"end": "2024-03-31", "val": 484275e6, "accn": "0000950170-24-000005", "fy": 2024, "fp": "Q3",
             "form": "10-Q", "filed": "2024-04-25"},
            {"end": "2024-07-25", "val": 1, "accn": accn(2024), "fy": 2024, "fp": "FY", "form": "10-K",
             "filed": FILED[2024]},                                          # not a fiscal year-end
        ]),
    }
    return {"cik": 789019, "entityName": "MICROSOFT CORP", "facts": {"us-gaap": gaap, "dei": {}}}


def submissions(ticker: str, older_page: bool = False) -> dict:
    """Three 10-Ks and their Item 2.02 8-Ks, plus unrelated filings."""
    rows = []
    for year in (2024, 2023, 2022):
        rows.append({"accessionNumber": accn(year), "filingDate": FILED[year], "reportDate": FY[year][1],
                     "form": "10-K", "items": "", "primaryDocument": f"{ticker.lower()}-{year}0630.htm"})
        rows.append({"accessionNumber": f"0000950170-{str(year)[2:]}-000002",
                     "filingDate": f"{year}-07-2{year % 5}", "reportDate": f"{year}-07-2{year % 5}",
                     "form": "8-K", "items": "2.02,9.01", "primaryDocument": f"{ticker.lower()}-8k.htm"})
        rows.append({"accessionNumber": f"0000950170-{str(year)[2:]}-000003", "filingDate": f"{year}-12-05",
                     "reportDate": f"{year}-12-05", "form": "8-K", "items": "5.07",
                     "primaryDocument": "vote.htm"})
        rows.append({"accessionNumber": f"0000950170-{str(year)[2:]}-000004", "filingDate": f"{year}-09-01",
                     "reportDate": f"{year}-08-30", "form": "4", "items": "", "primaryDocument": "form4.xml"})
    keys = list(rows[0])
    recent_rows = rows[:8] if older_page else rows      # older page holds the FY2022 filings
    payload = {"cik": "1", "tickers": [ticker], "fiscalYearEnd": "0630",
               "filings": {"recent": {k: [r[k] for r in recent_rows] for k in keys}, "files": []}}
    extra = None
    if older_page:
        payload["filings"]["files"] = [{"name": f"CIK-{ticker}-submissions-001.json", "filingCount": 4}]
        extra = {k: [r[k] for r in rows[8:]] for k in keys}
    return payload, extra


def filing_html(ticker: str, year: int) -> str:
    body = "".join(f"<p>{ticker} fiscal {year} paragraph {n}: cloud revenue and operating results "
                   f"are discussed here in enough words to form a passage.</p>" for n in range(12))
    risk = "".join(f"<p>{ticker} fiscal {year} risk factor {n}: competition and regulation could "
                   f"adversely affect results of operations and financial condition.</p>" for n in range(12))
    return ("<html><body><ix:header><ix:hidden>HIDDENXBRLFACT 999999 should never become "
            "a passage because it is hidden inline XBRL metadata</ix:hidden></ix:header>"
            f"<div>Item 1A. Risk Factors</div>{risk}"
            "<div>Item 7. Management's Discussion and Analysis</div>" + body + "</body></html>")
