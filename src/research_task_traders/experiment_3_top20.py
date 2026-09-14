import csv
import statistics
from pathlib import Path


# ---------------------------------------------------------------------------
# Experiment 3: Top 20 only
# ---------------------------------------------------------------------------

TOP_N = 20

TOP100_FILE = Path("data/top100_5y_returns.csv")
SPY_FILE = Path("data/spy_5y_returns.csv")

RESULT_DIR = Path("experiment_results")
OUTPUT_FILE = RESULT_DIR / "experiment_3_top20_benchmark.csv"


# Keep the same policy as Experiments 1 and 2.
INCLUDE_PARTIAL_DIVIDEND = True

VALID_STATUSES = {
    "ok",
    "delisted_-100pct_rule",
}

if INCLUDE_PARTIAL_DIVIDEND:
    VALID_STATUSES.add(
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


def parse_int(value):
    if value is None:
        return None

    try:
        return int(str(value).strip())
    except ValueError:
        return None


def round2(value):
    return round(value, 2)


def main():
    top100_rows = load_csv(TOP100_FILE)
    spy_rows = load_csv(SPY_FILE)

    # -----------------------------------------------------------------------
    # Load SPY benchmark returns
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

        spy_return = parse_float(
            row.get("total_return_pct")
        )

        if spy_return is None:
            spy_return = parse_float(
                row.get("return_pct")
            )

        if spy_return is not None:
            spy_by_year[year] = spy_return

    if not spy_by_year:
        raise RuntimeError(
            "No usable SPY returns found."
        )

    # -----------------------------------------------------------------------
    # Keep only original market-cap ranks 1-20
    # -----------------------------------------------------------------------

    top20_by_year = {}

    error_count = 0
    partial_count = 0
    ignored_lower_rank = 0
    excluded_status_count = 0

    for row in top100_rows:
        rank = parse_int(
            row.get("rank")
        )

        if rank is None:
            continue

        if rank > TOP_N:
            ignored_lower_rank += 1
            continue

        status = (
            row.get("status", "")
            or ""
        ).strip()

        if status == "error":
            error_count += 1
            continue

        if status == "ok_price_partial_dividend":
            partial_count += 1

        if status not in VALID_STATUSES:
            excluded_status_count += 1
            continue

        year_text = (
            row.get("year", "")
            or ""
        ).strip()

        total_return = parse_float(
            row.get("total_return_pct")
        )

        if not year_text or total_return is None:
            continue

        year = int(year_text)

        top20_by_year.setdefault(
            year,
            [],
        ).append(total_return)

    if not top20_by_year:
        raise RuntimeError(
            "No usable Top-20 returns found."
        )

    # -----------------------------------------------------------------------
    # Annual benchmark
    # -----------------------------------------------------------------------

    output_rows = []

    all_top20_returns = []
    all_excess_returns = []
    all_beats = []
    annual_spy_returns = []

    years = sorted(
        set(top20_by_year)
        & set(spy_by_year)
    )

    if not years:
        raise RuntimeError(
            "Top-20 and SPY data have no matching years."
        )

    for year in years:
        stock_returns = top20_by_year[year]
        spy_return = spy_by_year[year]

        top20_mean = statistics.fmean(
            stock_returns
        )

        top20_median = statistics.median(
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
                "top20_mean_pct": round2(
                    top20_mean
                ),
                "top20_median_pct": round2(
                    top20_median
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

        all_top20_returns.extend(
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
    # -----------------------------------------------------------------------

    overall_top20_mean = statistics.fmean(
        all_top20_returns
    )

    overall_top20_median = statistics.median(
        all_top20_returns
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
            "n": len(all_top20_returns),
            "top20_mean_pct": round2(
                overall_top20_mean
            ),
            "top20_median_pct": round2(
                overall_top20_median
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
    # Save CSV
    # -----------------------------------------------------------------------

    RESULT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "period",
        "n",
        "top20_mean_pct",
        "top20_median_pct",
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
    print("Experiment 3 - Top 20 vs SPY")
    print("-" * 90)

    for row in output_rows[:-1]:
        print(
            f"{row['period']}: "
            f"N={row['n']:2d} | "
            f"Top20 mean={row['top20_mean_pct']:+7.2f}% | "
            f"SPY={row['spy_return_pct']:+7.2f}% | "
            f"Excess={row['mean_excess_pct']:+7.2f}% | "
            f"Beat SPY={row['beat_spy_pct']:6.2f}%"
        )

    overall = output_rows[-1]

    print()
    print("Overall")
    print("-" * 90)

    print(
        f"N={overall['n']} | "
        f"Top20 mean={overall['top20_mean_pct']:+.2f}% | "
        f"Top20 median={overall['top20_median_pct']:+.2f}% | "
        f"SPY avg={overall['spy_return_pct']:+.2f}% | "
        f"Mean excess={overall['mean_excess_pct']:+.2f}% | "
        f"Beat SPY={overall['beat_spy_pct']:.2f}%"
    )

    print()
    print(
        f"Ignored rank 21-100 rows: "
        f"{ignored_lower_rank:,}"
    )

    if error_count:
        print(
            f"Warning: {error_count} Top-20 error rows "
            "were excluded."
        )

    if partial_count:
        if INCLUDE_PARTIAL_DIVIDEND:
            print(
                f"Warning: {partial_count} "
                "ok_price_partial_dividend rows were included."
            )
        else:
            print(
                f"Warning: {partial_count} "
                "partial-dividend rows were excluded."
            )

    if excluded_status_count:
        print(
            f"Warning: {excluded_status_count} rows with other "
            "statuses were excluded."
        )

    print()
    print(
        f"Saved Experiment 3 to: {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
