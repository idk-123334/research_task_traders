import csv
import math
import os
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from massive import RESTClient


START_YEAR = 2010
END_YEAR = 2021

TOP_N = 100
REFERENCE_TICKER = "SPY"

OUTPUT_DIR = Path("data")
OUTPUT_FILE = OUTPUT_DIR / "top100_market_cap_2010_2021.csv"
TEMP_FILE = OUTPUT_FILE.with_suffix(".tmp")


MAX_WORKERS = 32

MAX_RETRIES = 6

_thread_local = threading.local()


def get_worker_client(api_key: str) -> RESTClient:
    """
    Each worker thread gets its own RESTClient instance.
    """

    client = getattr(_thread_local, "client", None)

    if client is None:
        client = RESTClient(api_key=api_key)
        _thread_local.client = client

    return client


def timestamp_to_date(timestamp_ms: int):
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).date()


def get_first_trading_day(
    client: RESTClient,
    year: int,
) -> str:
    """
    Find the first SPY trading day of the given year.
    """

    bars = client.list_aggs(
        ticker=REFERENCE_TICKER,
        multiplier=1,
        timespan="day",
        from_=f"{year}-01-01",
        to=f"{year}-01-10",
        adjusted=True,
        sort="asc",
        limit=50,
    )

    for bar in bars:
        date = timestamp_to_date(bar.timestamp)

        if date.year == year:
            return date.isoformat()

    raise RuntimeError(
        f"Could not find the first trading day for {year}."
    )


def get_ticker_universe(
    client: RESTClient,
    date: str,
):
    """
    Get all stock-market tickers that were actively traded
    on the requested historical date.

    No ticker type filter is applied.

    In particular, this intentionally does NOT use:

        type="CS"

    Tickers whose type is None are therefore still included.
    """

    tickers = {}

    for item in client.list_tickers(
        market="stocks",
        active=True,
        date=date,
        sort="ticker",
        order="asc",
        limit=1000,
    ):
        ticker = getattr(item, "ticker", None)

        if not ticker:
            continue

        tickers[ticker] = {
            "ticker": ticker,
            "name": getattr(item, "name", None),
            "type": getattr(item, "type", None),
            "primary_exchange": getattr(
                item,
                "primary_exchange",
                None,
            ),
            "cik": getattr(item, "cik", None),
        }

    return tickers


def fetch_ticker_details(
    api_key: str,
    ticker: str,
    date: str,
    fallback_metadata: dict,
):
    """
    Fetch historical ticker details, including market cap.

    Results are kept in memory only.
    Nothing is written to disk here.
    """

    client = get_worker_client(api_key)

    for attempt in range(MAX_RETRIES):
        try:
            details = client.get_ticker_details(
                ticker=ticker,
                date=date,
            )

            market_cap = getattr(
                details,
                "market_cap",
                None,
            )

            if market_cap is None:
                return {
                    "status": "no_market_cap",
                    "ticker": ticker,
                }

            try:
                market_cap = float(market_cap)
            except (TypeError, ValueError):
                return {
                    "status": "no_market_cap",
                    "ticker": ticker,
                }

            if (
                not math.isfinite(market_cap)
                or market_cap <= 0
            ):
                return {
                    "status": "no_market_cap",
                    "ticker": ticker,
                }

            return {
                "status": "ok",
                "ticker": ticker,
                "name": (
                    getattr(details, "name", None)
                    or fallback_metadata.get("name")
                    or ""
                ),
                "type": (
                    getattr(details, "type", None)
                    or fallback_metadata.get("type")
                    or ""
                ),
                "primary_exchange": (
                    getattr(
                        details,
                        "primary_exchange",
                        None,
                    )
                    or fallback_metadata.get(
                        "primary_exchange"
                    )
                    or ""
                ),
                "cik": (
                    getattr(details, "cik", None)
                    or fallback_metadata.get("cik")
                    or ""
                ),
                "market_cap_usd": market_cap,
                "weighted_shares_outstanding": getattr(
                    details,
                    "weighted_shares_outstanding",
                    None,
                ),
                "share_class_shares_outstanding": getattr(
                    details,
                    "share_class_shares_outstanding",
                    None,
                ),
            }

        except Exception:
            if attempt == MAX_RETRIES - 1:
                return {
                    "status": "error",
                    "ticker": ticker,
                }

            # Exponential backoff in case of temporary
            # network errors or API throttling.
            delay = min(2 ** attempt, 30)
            delay += random.uniform(0, 1)

            time.sleep(delay)

    return {
        "status": "error",
        "ticker": ticker,
    }


def deduplicate_companies(rows):
    """
    Deduplicate multiple ticker/share classes belonging
    to the same company.

    If two tickers have the same SEC CIK, they are treated
    as the same company.

    The ticker with the highest reported market cap is kept.

    If CIK is unavailable, the ticker itself becomes the
    unique key.
    """

    companies = {}

    for row in rows:
        cik = row.get("cik")

        if cik:
            company_key = f"CIK:{cik}"
        else:
            company_key = f"TICKER:{row['ticker']}"

        existing = companies.get(company_key)

        if (
            existing is None
            or row["market_cap_usd"]
            > existing["market_cap_usd"]
        ):
            companies[company_key] = row

    return list(companies.values())


def load_existing_results():
    """
    Load previously completed years so the script can
    resume at the year level.
    """

    if not OUTPUT_FILE.exists():
        return []

    with OUTPUT_FILE.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as f:
        return list(csv.DictReader(f))


def write_results(rows):
    """
    Write the CSV atomically.

    Data is first written to a temporary file, then the
    temporary file replaces the real CSV.

    This prevents an interrupted write from corrupting the
    existing result file.
    """

    fieldnames = [
        "year",
        "date",
        "rank",
        "ticker",
        "name",
        "market_cap_usd",
        "type",
        "primary_exchange",
        "cik",
        "weighted_shares_outstanding",
        "share_class_shares_outstanding",
    ]

    try:
        with TEMP_FILE.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
            )

            writer.writeheader()

            for row in rows:
                writer.writerow(row)

        os.replace(
            TEMP_FILE,
            OUTPUT_FILE,
        )

    finally:
        # Normally os.replace() removes TEMP_FILE.
        # This also cleans it up if writing is interrupted.
        if TEMP_FILE.exists():
            try:
                TEMP_FILE.unlink()
            except OSError:
                pass


def process_year(
    api_key: str,
    client: RESTClient,
    year: int,
):
    trading_date = get_first_trading_day(
        client,
        year,
    )

    print(
        f"First trading day: {trading_date}"
    )

    ticker_universe = get_ticker_universe(
        client,
        trading_date,
    )

    print(
        f"Active stock tickers: "
        f"{len(ticker_universe):,}"
    )

    ticker_rows = []
    failed_tickers = []

    executor = ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    )

    futures = {}

    try:
        futures = {
            executor.submit(
                fetch_ticker_details,
                api_key,
                ticker,
                trading_date,
                metadata,
            ): ticker
            for ticker, metadata
            in ticker_universe.items()
        }

        total = len(futures)

        for completed, future in enumerate(
            as_completed(futures),
            start=1,
        ):
            ticker = futures[future]

            try:
                result = future.result()

            except Exception:
                failed_tickers.append(ticker)
                continue

            if result["status"] == "ok":
                ticker_rows.append(result)

            elif result["status"] == "error":
                failed_tickers.append(ticker)

            if (
                completed % 250 == 0
                or completed == total
            ):
                print(
                    f"  {completed:,}/{total:,} "
                    f"ticker details processed"
                )

    except KeyboardInterrupt:
        print()
        print("Interrupted by user.")
        print("Cancelling pending API requests...")
        print(
            "Partial data for the current year "
            "will not be saved."
        )

        for future in futures:
            future.cancel()

        executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

        raise

    else:
        executor.shutdown(wait=True)

    if failed_tickers:
        print()
        print(
            f"{len(failed_tickers):,} ticker detail "
            f"requests failed."
        )

        print(
            "The ranking will not be saved because an "
            "incomplete ticker universe could produce an "
            "incorrect Top 100."
        )

        print(
            "Failed tickers: "
            + ", ".join(failed_tickers[:20])
        )

        if len(failed_tickers) > 20:
            print(
                f"... and "
                f"{len(failed_tickers) - 20:,} more."
            )

        raise RuntimeError(
            f"Incomplete data for {year}. "
            "Run the script again to retry."
        )

    companies = deduplicate_companies(
        ticker_rows
    )

    companies.sort(
        key=lambda row: row["market_cap_usd"],
        reverse=True,
    )

    if len(companies) < TOP_N:
        raise RuntimeError(
            f"Only {len(companies)} companies with "
            f"valid market cap data were found for "
            f"{year}."
        )

    return (
        trading_date,
        companies[:TOP_N],
    )


def main():
    load_dotenv()

    api_key = os.getenv(
        "MASSIVE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "MASSIVE_API_KEY was not found in .env"
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Clean up a stale temporary file from an abnormal
    # previous termination, if one exists.
    if TEMP_FILE.exists():
        try:
            TEMP_FILE.unlink()
        except OSError:
            pass

    client = RESTClient(
        api_key=api_key
    )

    existing_rows = load_existing_results()

    completed_counts = Counter(
        int(row["year"])
        for row in existing_rows
        if row.get("year")
    )

    try:
        for year in range(
            START_YEAR,
            END_YEAR + 1,
        ):
            if completed_counts[year] >= TOP_N:
                print(
                    f"{year}: already completed, skipping."
                )
                continue

            print()
            print("=" * 70)
            print(f"Processing {year}")
            print("=" * 70)

            (
                trading_date,
                top_companies,
            ) = process_year(
                api_key,
                client,
                year,
            )

            # Remove any incomplete or old entries for
            # this year before replacing them.
            existing_rows = [
                row
                for row in existing_rows
                if int(row["year"]) != year
            ]

            for rank, company in enumerate(
                top_companies,
                start=1,
            ):
                existing_rows.append(
                    {
                        "year": year,
                        "date": trading_date,
                        "rank": rank,
                        "ticker": company["ticker"],
                        "name": company["name"],
                        "market_cap_usd": (
                            f"{company['market_cap_usd']:.2f}"
                        ),
                        "type": company["type"],
                        "primary_exchange": (
                            company["primary_exchange"]
                        ),
                        "cik": company["cik"],
                        "weighted_shares_outstanding": (
                            company[
                                "weighted_shares_outstanding"
                            ]
                            or ""
                        ),
                        "share_class_shares_outstanding": (
                            company[
                                "share_class_shares_outstanding"
                            ]
                            or ""
                        ),
                    }
                )

            existing_rows.sort(
                key=lambda row: (
                    int(row["year"]),
                    int(row["rank"]),
                )
            )

            # Only a completely finished year is saved.
            write_results(existing_rows)

            completed_counts[year] = TOP_N

            print()
            print(
                f"Top {TOP_N} for {year} saved."
            )

            print(
                f"#1: "
                f"{top_companies[0]['ticker']} - "
                f"{top_companies[0]['name']} - "
                f"${top_companies[0]['market_cap_usd']:,.0f}"
            )

    except KeyboardInterrupt:
        print()
        print("Program stopped.")
        print(
            "Completed years remain safely stored in the CSV."
        )
        print(
            "Partial data from the current year was kept "
            "in memory only and has been discarded."
        )
        return

    print()
    print("Finished.")
    print(
        f"Results saved to: {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()