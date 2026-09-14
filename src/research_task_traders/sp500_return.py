import csv
import os
from bisect import bisect_right
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from massive import RESTClient


TICKER = "SPY"

START_YEAR = 2010
END_YEAR = 2021
HOLDING_YEARS = 5

OUTPUT_FILE = Path("data/spy_5y_returns.csv")


def timestamp_to_date(timestamp_ms: int):
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).date()


def parse_date(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    if not text:
        return None

    return datetime.strptime(
        text[:10],
        "%Y-%m-%d",
    ).date()


def add_years(d: date, years: int):
    """
    Add whole calendar years.

    Feb 29 is converted to Feb 28 if necessary.
    """
    try:
        return d.replace(
            year=d.year + years
        )
    except ValueError:
        return d.replace(
            year=d.year + years,
            month=2,
            day=28,
        )


def get_split_adjusted_dividend(dividend):
    """
    Return cash dividend per share on the same
    split-adjusted share basis as adjusted prices.
    """

    adjusted_amount = getattr(
        dividend,
        "split_adjusted_cash_amount",
        None,
    )

    if adjusted_amount is not None:
        return float(adjusted_amount)

    cash_amount = getattr(
        dividend,
        "cash_amount",
        None,
    )

    if cash_amount is None:
        raise RuntimeError(
            "Dividend record has neither "
            "split_adjusted_cash_amount nor cash_amount."
        )

    # SPY did not undergo a stock split during the
    # research period, so raw cash amount is a valid fallback.
    return float(cash_amount)


def main():
    load_dotenv()

    api_key = os.getenv("MASSIVE_API_KEY")

    if not api_key:
        raise RuntimeError(
            "MASSIVE_API_KEY was not found in .env"
        )

    client = RESTClient(api_key)

    # ------------------------------------------------------------
    # Download SPY daily prices
    # ------------------------------------------------------------

    last_year = END_YEAR + HOLDING_YEARS

    print(
        f"Downloading {TICKER} daily prices "
        f"from {START_YEAR} through {last_year}..."
    )

    bars = list(
        client.list_aggs(
            ticker=TICKER,
            multiplier=1,
            timespan="day",
            from_=f"{START_YEAR}-01-01",
            to=f"{last_year}-01-10",
            adjusted=True,
            sort="asc",
            limit=50000,
        )
    )

    if not bars:
        raise RuntimeError(
            "No SPY price data was returned by Massive."
        )

    # ------------------------------------------------------------
    # Parse daily CLOSE prices
    # ------------------------------------------------------------

    daily_prices = []

    for bar in bars:
        bar_date = timestamp_to_date(
            bar.timestamp
        )

        close = float(bar.close)

        if close <= 0:
            continue

        daily_prices.append(
            (bar_date, close)
        )

    daily_prices.sort(
        key=lambda x: x[0]
    )

    if not daily_prices:
        raise RuntimeError(
            "No usable SPY prices were returned."
        )

    price_dates = [
        item[0]
        for item in daily_prices
    ]

    # ------------------------------------------------------------
    # Find first trading day of each formation year
    # ------------------------------------------------------------

    first_trading_day = {}

    for bar_date, close in daily_prices:
        year = bar_date.year

        if (
            START_YEAR
            <= year
            <= END_YEAR
            and year not in first_trading_day
        ):
            first_trading_day[year] = {
                "date": bar_date,
                "close": close,
            }

    # ------------------------------------------------------------
    # Download SPY dividends
    # ------------------------------------------------------------

    print(
        f"Downloading {TICKER} dividend history..."
    )

    dividends = list(
        client.list_stocks_dividends(
            ticker=TICKER,
            limit=5000,
            sort="ex_dividend_date.asc",
        )
    )

    parsed_dividends = []

    for dividend in dividends:
        ex_date = parse_date(
            getattr(
                dividend,
                "ex_dividend_date",
                None,
            )
        )

        if ex_date is None:
            continue

        currency = (
            getattr(
                dividend,
                "currency",
                "",
            )
            or ""
        ).upper()

        if currency and currency != "USD":
            raise RuntimeError(
                f"Unexpected non-USD SPY dividend "
                f"on {ex_date}: {currency}"
            )

        amount = get_split_adjusted_dividend(
            dividend
        )

        parsed_dividends.append(
            {
                "ex_date": ex_date,
                "amount": amount,
            }
        )

    print(
        f"Downloaded "
        f"{len(parsed_dividends)} dividend records."
    )

    # ------------------------------------------------------------
    # Calculate five-year total returns
    # ------------------------------------------------------------

    rows = []

    print()

    for buy_year in range(
        START_YEAR,
        END_YEAR + 1,
    ):
        if buy_year not in first_trading_day:
            print(
                f"Warning: no trading day "
                f"found for {buy_year}"
            )
            continue

        buy = first_trading_day[
            buy_year
        ]

        buy_date = buy["date"]
        buy_price = buy["close"]

        # Exact five-year target date.
        target_sell_date = add_years(
            buy_date,
            HOLDING_YEARS,
        )

        # Match the individual-stock calculation:
        # use the last available trading day on or
        # before the five-year target date.
        end_index = (
            bisect_right(
                price_dates,
                target_sell_date,
            )
            - 1
        )

        if end_index < 0:
            print(
                f"Warning: no ending price "
                f"found for {buy_year}"
            )
            continue

        sell_date, sell_price = (
            daily_prices[end_index]
        )

        # Safety check: the chosen endpoint should
        # actually belong to the approximate five-year
        # endpoint, not some much earlier observation.
        if sell_date < buy_date:
            raise RuntimeError(
                f"Invalid sell date for {buy_year}: "
                f"{sell_date}"
            )

        # --------------------------------------------------------
        # Dividend eligibility
        #
        # Buy at CLOSE:
        #     ex_date == buy_date is not received.
        #
        # Sell at CLOSE:
        #     ex_date == sell_date is received.
        #
        # Therefore:
        #
        #     buy_date < ex_date <= sell_date
        # --------------------------------------------------------

        total_dividend_cash = 0.0

        for dividend in parsed_dividends:
            ex_date = dividend["ex_date"]

            if (
                buy_date
                < ex_date
                <= sell_date
            ):
                total_dividend_cash += (
                    dividend["amount"]
                )

        # --------------------------------------------------------
        # Total return
        #
        # One split-adjusted share is purchased.
        # Dividends are received as cash and NOT reinvested.
        # --------------------------------------------------------

        price_return = (
            sell_price
            / buy_price
            - 1
        )

        dividend_return = (
            total_dividend_cash
            / buy_price
        )

        total_return = (
            price_return
            + dividend_return
        )

        total_return_pct = (
            total_return * 100
        )

        row = {
            "buy_year": buy_year,
            "buy_date": (
                buy_date.isoformat()
            ),
            "buy_price": round(
                buy_price,
                4,
            ),
            "sell_year": sell_date.year,
            "sell_date": (
                sell_date.isoformat()
            ),
            "sell_price": round(
                sell_price,
                4,
            ),
            "total_return_pct": round(
                total_return_pct,
                4,
            ),
        }

        rows.append(row)

        print(
            f"{buy_year}: "
            f"{buy_date} CLOSE "
            f"${buy_price:.2f} -> "
            f"{sell_date} CLOSE "
            f"${sell_price:.2f} | "
            f"Target: {target_sell_date} | "
            f"Dividends: "
            f"${total_dividend_cash:.2f} | "
            f"Total return: "
            f"{total_return_pct:+.2f}%"
        )

    # ------------------------------------------------------------
    # Save CSV
    # ------------------------------------------------------------

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "buy_year",
        "buy_date",
        "buy_price",
        "sell_year",
        "sell_date",
        "sell_price",
        "total_return_pct",
    ]

    with OUTPUT_FILE.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(rows)

    print()
    print(
        f"Saved {len(rows)} results "
        f"to {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()