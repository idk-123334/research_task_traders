import csv
import statistics
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

TOP100_FILE = Path("data/top100_5y_returns.csv")
SPY_FILE = Path("data/spy_5y_returns.csv")

RESULT_DIR = Path("experiment_results")
OUTPUT_FILE = RESULT_DIR / "benchmark_summary.csv"


# ---------------------------------------------------------------------------
# Benchmark policy
# ---------------------------------------------------------------------------

# "ok_price_partial_dividend" means price return is valid, but some non-USD
# dividends were skipped by the Top-100 return script.
#
# True  = include them in the benchmark now, but print a warning.
# False = exclude them until their dividend returns are completed.
INCLUDE_PARTIAL_DIVIDEND = True

VALID_TOP100_STATUSES = {
    "ok",
    "delisted_-100pct_rule",
}

if INCLUDE_PARTIAL_DIVIDEND:
    VALID_TOP100_STATUSES.add(
        "ok_price_partial_dividend"
    )


def load_csv(path: Path):
    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path}"
        )

    with path.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as f:
        return list(csv.DictReader(f))


def parse_float(value):
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def round2(value):
    return round(value, 2)


def main():
    top100_rows = load_csv(TOP100_FILE)
    spy_rows = load_csv(SPY_FILE)

    # -----------------------------------------------------------------------
    # Load SPY benchmark returns by buy year
    # -----------------------------------------------------------------------

    spy_by_year = {}

    for row in spy_rows:
        year_text = (
            row.get("buy_year")
            or row.get("year")
            or ""
        ).strip()

        if not year_text:
            continue

        year = int(year_text)


        return_pct = parse_float(
            row.get("total_return_pct")
        )

        if return_pct is None:
            return_pct = parse_float(
                row.get("return_pct")
            )

        if return_pct is None:
            continue

        spy_by_year[year] = return_pct

    if not spy_by_year:
        raise RuntimeError(
            "No usable SPY returns were found."
        )

    # -----------------------------------------------------------------------
    # Load usable Top-100 returns
    # -----------------------------------------------------------------------

    top100_by_year = {}
    error_count = 0
    partial_count = 0
    excluded_status_count = 0

    for row in top100_rows:
        status = (
            row.get("status", "")
            or ""
        ).strip()

        if status == "error":
            error_count += 1
            continue

        if status == "ok_price_partial_dividend":
            partial_count += 1

        if status not in VALID_TOP100_STATUSES:
            excluded_status_count += 1
            continue

        year_text = (
            row.get("year", "")
            or ""
        ).strip()

        return_pct = parse_float(
            row.get("total_return_pct")
        )

        if not year_text or return_pct is None:
            continue

        year = int(year_text)

        top100_by_year.setdefault(
            year,
            [],
        ).append(return_pct)

    if not top100_by_year:
        raise RuntimeError(
            "No usable Top-100 returns were found."
        )

    # -----------------------------------------------------------------------
    # Annual benchmark
    # -----------------------------------------------------------------------

    output_rows = []

    all_top100_returns = []
    all_excess_returns = []
    all_beats = []
    annual_spy_returns = []

    years = sorted(
        set(top100_by_year)
        & set(spy_by_year)
    )

    if not years:
        raise RuntimeError(
            "Top-100 and SPY files have no matching years."
        )

    for year in years:
        stock_returns = top100_by_year[year]
        spy_return = spy_by_year[year]

        if not stock_returns:
            continue

        top100_mean = statistics.fmean(
            stock_returns
        )
        top100_median = statistics.median(
            stock_returns
        )

        excess_returns = [
            value - spy_return
            for value in stock_returns
        ]

        beat_count = sum(
            value > spy_return
            for value in stock_returns
        )

        beat_spy_pct = (
            beat_count
            / len(stock_returns)
            * 100
        )

        mean_excess = statistics.fmean(
            excess_returns
        )

        output_rows.append(
            {
                "period": str(year),
                "n": len(stock_returns),
                "top100_mean_pct": round2(
                    top100_mean
                ),
                "top100_median_pct": round2(
                    top100_median
                ),
                "spy_return_pct": round2(
                    spy_return
                ),
                "mean_excess_pct": round2(
                    mean_excess
                ),
                "beat_spy_pct": round2(
                    beat_spy_pct
                ),
            }
        )

        all_top100_returns.extend(
            stock_returns
        )
        all_excess_returns.extend(
            excess_returns
        )
        all_beats.extend(
            value > spy_return
            for value in stock_returns
        )
        annual_spy_returns.append(
            spy_return
        )

    # -----------------------------------------------------------------------
    # Overall benchmark
    #
    # Top-100 mean/median:
    #     all usable stock-year observations pooled together.
    #
    # SPY:
    #     arithmetic mean of the annual five-year SPY returns.
    #
    # Mean excess / beat rate:
    #     every stock is compared with the SPY return for its own buy year.
    # -----------------------------------------------------------------------

    overall_top100_mean = statistics.fmean(
        all_top100_returns
    )

    overall_top100_median = statistics.median(
        all_top100_returns
    )

    overall_spy_mean = statistics.fmean(
        annual_spy_returns
    )

    overall_mean_excess = statistics.fmean(
        all_excess_returns
    )

    overall_beat_spy_pct = (
        sum(all_beats)
        / len(all_beats)
        * 100
    )

    output_rows.append(
        {
            "period": "Overall",
            "n": len(all_top100_returns),
            "top100_mean_pct": round2(
                overall_top100_mean
            ),
            "top100_median_pct": round2(
                overall_top100_median
            ),
            "spy_return_pct": round2(
                overall_spy_mean
            ),
            "mean_excess_pct": round2(
                overall_mean_excess
            ),
            "beat_spy_pct": round2(
                overall_beat_spy_pct
            ),
        }
    )

    # -----------------------------------------------------------------------
    # Save concise human-readable CSV
    # -----------------------------------------------------------------------

    RESULT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "period",
        "n",
        "top100_mean_pct",
        "top100_median_pct",
        "spy_return_pct",
        "mean_excess_pct",
        "beat_spy_pct",
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
        writer.writerows(output_rows)

    # -----------------------------------------------------------------------
    # Console summary
    # -----------------------------------------------------------------------

    print()
    print("Annual benchmark")
    print("-" * 86)

    for row in output_rows[:-1]:
        print(
            f"{row['period']}: "
            f"N={row['n']:3d} | "
            f"Top100 mean={row['top100_mean_pct']:+7.2f}% | "
            f"SPY={row['spy_return_pct']:+7.2f}% | "
            f"Excess={row['mean_excess_pct']:+7.2f}% | "
            f"Beat SPY={row['beat_spy_pct']:6.2f}%"
        )

    overall = output_rows[-1]

    print()
    print("Overall benchmark")
    print("-" * 86)
    print(
        f"N={overall['n']} | "
        f"Top100 mean={overall['top100_mean_pct']:+.2f}% | "
        f"Top100 median={overall['top100_median_pct']:+.2f}% | "
        f"SPY avg={overall['spy_return_pct']:+.2f}% | "
        f"Mean excess={overall['mean_excess_pct']:+.2f}% | "
        f"Beat SPY={overall['beat_spy_pct']:.2f}%"
    )

    print()

    if error_count:
        print(
            f"Warning: {error_count} Top-100 error rows "
            "were excluded."
        )

    if partial_count:
        if INCLUDE_PARTIAL_DIVIDEND:
            print(
                f"Warning: {partial_count} "
                "ok_price_partial_dividend rows were included; "
                "their total returns may be slightly understated."
            )
        else:
            print(
                f"Warning: {partial_count} "
                "ok_price_partial_dividend rows were excluded."
            )

    if excluded_status_count:
        print(
            f"Warning: {excluded_status_count} rows with other "
            "statuses were excluded."
        )

    print()
    print(
        f"Saved benchmark to: {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
