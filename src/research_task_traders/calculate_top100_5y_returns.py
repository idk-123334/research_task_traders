import csv
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from massive import RESTClient


# ===========================================================================
# Configuration
# ===========================================================================

INPUT_FILE = Path("data/top100_market_cap_2010_2021.csv")
OUTPUT_FILE = Path("data/top100_5y_returns.csv")
TEMP_FILE = OUTPUT_FILE.with_suffix(".tmp")

HOLD_YEARS = 5


METHOD_VERSION = "2"


MAX_WORKERS = 12
MAX_RETRIES = 6

START_BAR_SEARCH_DAYS = 7
END_BAR_SEARCH_DAYS = 10

_thread_local = threading.local()


# ===========================================================================
# Generic helpers
# ===========================================================================

def get_worker_client(api_key: str) -> RESTClient:
    client = getattr(_thread_local, "client", None)

    if client is None:
        client = RESTClient(api_key=api_key)
        _thread_local.client = client

    return client


def retry_call(func, *args, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise

            delay = min(2 ** attempt, 30) + random.uniform(0, 1)
            time.sleep(delay)


def retry_list(factory):
    """
    Retry the whole iterator consumption, not just iterator construction.
    Massive pagination/network errors can occur while list(...) is consuming.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return list(factory())
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise

            delay = min(2 ** attempt, 30) + random.uniform(0, 1)
            time.sleep(delay)

    return []


def safe_get(obj, name, default=None):
    if obj is None:
        return default

    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


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

    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Feb 29 -> Feb 28
        return d.replace(
            year=d.year + years,
            month=2,
            day=28,
        )


def timestamp_to_date(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(
        timestamp_ms / 1000,
        tz=timezone.utc,
    ).date()


def finite_float(value):
    if value is None:
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(value):
        return None

    return value


def fmt_float(value, digits=10):
    if value is None:
        return ""

    return f"{value:.{digits}f}"


def add_warning(warnings, message):
    if message and message not in warnings:
        warnings.append(message)


# ===========================================================================
# Ticker identity / rename resolution
# ===========================================================================

def get_formation_identity(
    client: RESTClient,
    ticker: str,
    formation_date: date,
    fallback_cik: str,
):
    """
    Ticker Details is useful, but it is NOT allowed to make the entire row fail.
    If it fails, we keep going using the CIK already stored in the Top-100 CSV.
    """
    identity = {
        "cik": (fallback_cik or "").strip(),
        "composite_figi": "",
        "share_class_figi": "",
        "primary_exchange": "",
    }

    details = retry_call(
        client.get_ticker_details,
        ticker=ticker,
        date=formation_date.isoformat(),
    )

    identity["cik"] = (
        str(safe_get(details, "cik", "") or "").strip()
        or identity["cik"]
    )
    identity["composite_figi"] = str(
        safe_get(details, "composite_figi", "") or ""
    ).strip()
    identity["share_class_figi"] = str(
        safe_get(details, "share_class_figi", "") or ""
    ).strip()
    identity["primary_exchange"] = str(
        safe_get(details, "primary_exchange", "") or ""
    ).strip()

    return identity


def list_tickers_for_cik(
    client: RESTClient,
    cik: str,
    *,
    active=None,
    at_date=None,
):
    if not cik:
        return []

    kwargs = {
        "cik": cik,
        "market": "stocks",
        "limit": 1000,
    }

    if active is not None:
        kwargs["active"] = active

    if at_date is not None:
        kwargs["date"] = at_date.isoformat()

    return retry_list(
        lambda: client.list_tickers(**kwargs)
    )


def identity_score(
    item,
    identity,
    preferred_tickers,
):
    """
    Higher score = more likely to be the same share class.

    share_class_figi is the best discriminator when a CIK has multiple listed
    share classes (GOOG/GOOGL, BRK.A/BRK.B, etc.).
    """
    score = 0

    ticker = str(safe_get(item, "ticker", "") or "")
    share_class_figi = str(
        safe_get(item, "share_class_figi", "") or ""
    )
    composite_figi = str(
        safe_get(item, "composite_figi", "") or ""
    )
    primary_exchange = str(
        safe_get(item, "primary_exchange", "") or ""
    )

    if (
        identity.get("share_class_figi")
        and share_class_figi == identity["share_class_figi"]
    ):
        score += 1000

    if (
        identity.get("composite_figi")
        and composite_figi == identity["composite_figi"]
    ):
        score += 500

    if ticker in preferred_tickers:
        score += 200

    if (
        identity.get("primary_exchange")
        and primary_exchange == identity["primary_exchange"]
    ):
        score += 20

    return score


def choose_identity_ticker(
    items,
    identity,
    preferred_tickers,
):
    if not items:
        return None

    scored = [
        (
            identity_score(
                item,
                identity,
                preferred_tickers,
            ),
            str(safe_get(item, "ticker", "") or ""),
            item,
        )
        for item in items
        if safe_get(item, "ticker")
    ]

    if not scored:
        return None

    scored.sort(
        key=lambda x: (x[0], x[1]),
        reverse=True,
    )

    best_score, _, best_item = scored[0]

    # If we have absolutely no identifying information and several possible
    if (
        len(scored) > 1
        and best_score == 0
    ):
        return None

    return best_item


def try_get_ticker_events(
    client: RESTClient,
    identifier: str,
    warnings,
):
    """
    Ticker Events is experimental. It is useful supplementary information,
    never a hard dependency.
    """
    if not identifier:
        return []

    try:
        response = retry_call(
            client.get_ticker_events,
            identifier,
        )
    except Exception as exc:
        add_warning(
            warnings,
            "ticker_events_unavailable:"
            f"{type(exc).__name__}",
        )
        return []

    results = safe_get(response, "results", response)
    events = safe_get(results, "events", []) or []

    parsed = []

    for event in events:
        if safe_get(event, "type") != "ticker_change":
            continue

        event_date = parse_date(
            safe_get(event, "date")
        )
        change = safe_get(
            event,
            "ticker_change",
        )
        new_ticker = str(
            safe_get(change, "ticker", "") or ""
        ).strip()

        if event_date and new_ticker:
            parsed.append(
                {
                    "date": event_date,
                    "ticker": new_ticker,
                }
            )

    parsed.sort(key=lambda x: x["date"])
    return parsed


def ticker_at_date_from_events(
    original_ticker: str,
    events,
    target_date: date,
):
    ticker = original_ticker

    for event in events:
        if event["date"] <= target_date:
            ticker = event["ticker"]
        else:
            break

    return ticker


def aliases_from_identity_records(
    active_records,
    inactive_records,
    identity,
    original_ticker,
    event_tickers,
):
    preferred = set(event_tickers)
    preferred.add(original_ticker)

    aliases = {original_ticker}

    for item in active_records + inactive_records:
        ticker = str(
            safe_get(item, "ticker", "") or ""
        ).strip()

        if not ticker:
            continue

        score = identity_score(
            item,
            identity,
            preferred,
        )

        # Stable FIGI match: confidently same share class.
        if score >= 500:
            aliases.add(ticker)
            continue

        # If FIGIs were unavailable, an event-confirmed ticker is still useful.
        if ticker in preferred:
            aliases.add(ticker)

    aliases.update(t for t in event_tickers if t)

    return aliases


# ===========================================================================
# Delisting
# ===========================================================================

def find_final_delisting(
    inactive_records,
    identity,
    aliases,
    formation_date,
    target_end_date,
):
    """
    Old ticker symbols can have a delisted_utc simply because the symbol was
    renamed. Therefore we do NOT take the first inactive symbol.

    For the same share class, the latest delisted_utc is the relevant final
    cessation of trading. If another alias is active at the target date, this
    function is not used for the -100% rule.
    """
    candidates = []

    preferred = set(aliases)

    for item in inactive_records:
        ticker = str(
            safe_get(item, "ticker", "") or ""
        ).strip()

        d = parse_date(
            safe_get(item, "delisted_utc")
        )

        if not ticker or d is None:
            continue

        score = identity_score(
            item,
            identity,
            preferred,
        )

        same_identity = (
            score >= 500
            or ticker in preferred
        )

        if not same_identity:
            continue

        if formation_date < d <= target_end_date:
            candidates.append((d, ticker))

    if not candidates:
        return None

    candidates.sort()
    return candidates[-1]


# ===========================================================================
# Price data
# ===========================================================================

def fetch_bars(
    client: RESTClient,
    ticker: str,
    from_date: date,
    to_date: date,
):
    return retry_list(
        lambda: client.list_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="day",
            from_=from_date.isoformat(),
            to=to_date.isoformat(),
            adjusted=True,
            sort="asc",
            limit=50000,
        )
    )


def get_start_price(
    client: RESTClient,
    ticker: str,
    formation_date: date,
):
    bars = fetch_bars(
        client,
        ticker,
        formation_date,
        formation_date + timedelta(
            days=START_BAR_SEARCH_DAYS
        ),
    )

    candidates = []

    for bar in bars:
        ts = safe_get(bar, "timestamp")
        close = finite_float(
            safe_get(bar, "close")
        )

        if ts is None or close is None or close <= 0:
            continue

        d = timestamp_to_date(ts)

        if d >= formation_date:
            candidates.append((d, close))

    if not candidates:
        raise RuntimeError(
            f"No usable start price for "
            f"{ticker} on/after {formation_date}"
        )

    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def get_end_price(
    client: RESTClient,
    ticker: str,
    target_end_date: date,
):
    bars = fetch_bars(
        client,
        ticker,
        target_end_date - timedelta(
            days=END_BAR_SEARCH_DAYS
        ),
        target_end_date,
    )

    candidates = []

    for bar in bars:
        ts = safe_get(bar, "timestamp")
        close = finite_float(
            safe_get(bar, "close")
        )

        if ts is None or close is None or close <= 0:
            continue

        d = timestamp_to_date(ts)

        if d <= target_end_date:
            candidates.append((d, close))

    if not candidates:
        raise RuntimeError(
            f"No usable end price for "
            f"{ticker} on/before {target_end_date}"
        )

    candidates.sort(key=lambda x: x[0])
    return candidates[-1]


def get_end_price_from_candidates(
    client: RESTClient,
    candidate_tickers,
    target_end_date: date,
):
    errors = []

    for ticker in candidate_tickers:
        if not ticker:
            continue

        try:
            d, price = get_end_price(
                client,
                ticker,
                target_end_date,
            )
            return ticker, d, price
        except Exception as exc:
            errors.append(
                f"{ticker}:{type(exc).__name__}"
            )

    raise RuntimeError(
        "No end price found for candidate tickers: "
        + ", ".join(errors)
    )


# ===========================================================================
# Dividend / split data
# ===========================================================================

def list_dividends_for_ticker(
    client: RESTClient,
    ticker: str,
):
    """
    Prefer the new /stocks/v1/dividends endpoint.

    Fallback to the legacy SDK method if the installed massive package does
    not yet expose list_stocks_dividends().
    """
    new_method = getattr(
        client,
        "list_stocks_dividends",
        None,
    )

    if new_method is not None:
        return retry_list(
            lambda: new_method(
                ticker=ticker,
                limit=5000,
                sort="ex_dividend_date.asc",
            )
        )

    old_method = getattr(
        client,
        "list_dividends",
        None,
    )

    if old_method is None:
        raise RuntimeError(
            "Installed massive SDK has neither "
            "list_stocks_dividends nor list_dividends"
        )

    return retry_list(
        lambda: old_method(
            ticker=ticker,
            order="asc",
            limit=1000,
            sort="ex_dividend_date",
        )
    )


def list_splits_for_ticker(
    client: RESTClient,
    ticker: str,
):
    new_method = getattr(
        client,
        "list_stocks_splits",
        None,
    )

    if new_method is not None:
        return retry_list(
            lambda: new_method(
                ticker=ticker,
                limit=5000,
                sort="execution_date.asc",
            )
        )

    old_method = getattr(
        client,
        "list_splits",
        None,
    )

    if old_method is None:
        raise RuntimeError(
            "Installed massive SDK has neither "
            "list_stocks_splits nor list_splits"
        )

    return retry_list(
        lambda: old_method(
            ticker=ticker,
            order="asc",
            limit=1000,
            sort="execution_date",
        )
    )


def collect_all_splits(
    client: RESTClient,
    aliases,
    warnings,
):
    seen = set()
    rows = []

    for ticker in sorted(aliases):
        try:
            splits = list_splits_for_ticker(
                client,
                ticker,
            )
        except Exception as exc:
            add_warning(
                warnings,
                f"splits_failed:{ticker}:"
                f"{type(exc).__name__}",
            )
            continue

        for item in splits:
            execution_date = parse_date(
                safe_get(item, "execution_date")
            )

            if execution_date is None:
                continue

            split_from = finite_float(
                safe_get(item, "split_from")
            )
            split_to = finite_float(
                safe_get(item, "split_to")
            )

            if (
                split_from is None
                or split_to is None
                or split_from <= 0
                or split_to <= 0
            ):
                continue

            event_id = str(
                safe_get(item, "id", "") or ""
            )

            if event_id:
                key = f"id:{event_id}"
            else:
                key = (
                    f"{execution_date}|"
                    f"{split_from}|{split_to}"
                )

            if key in seen:
                continue

            seen.add(key)

            rows.append(
                {
                    "date": execution_date,
                    "split_from": split_from,
                    "split_to": split_to,
                }
            )

    rows.sort(key=lambda x: x["date"])
    return rows


def split_adjustment_factor_after(
    dividend_ex_date: date,
    splits,
):
    """
    Convert a historical raw per-share cash dividend onto the same present-day
    split-adjusted share basis used by adjusted aggregate prices.

    For each later split:
        old-share cash amount * (split_from / split_to)

    Example: 4-for-1 => multiply old dividend by 1/4.
    """
    factor = 1.0

    for split in splits:
        if split["date"] <= dividend_ex_date:
            continue

        factor *= (
            split["split_from"]
            / split["split_to"]
        )

    return factor


def collect_dividends(
    client: RESTClient,
    aliases,
    actual_start_date: date,
    actual_end_date: date,
    warnings,
):
    """
    Fetch dividends across every known ticker alias and deduplicate by event ID.

    IMPORTANT BUG FIX:
    split_adjusted_cash_amount is optional in Massive's schema. Missing values
    no longer cause the whole stock to fail. We fall back to raw cash_amount
    plus split history.
    """
    seen = set()
    raw_rows = []

    for ticker in sorted(aliases):
        try:
            dividends = list_dividends_for_ticker(
                client,
                ticker,
            )
        except Exception as exc:
            add_warning(
                warnings,
                f"dividends_failed:{ticker}:"
                f"{type(exc).__name__}",
            )
            continue

        for item in dividends:
            ex_date = parse_date(
                safe_get(item, "ex_dividend_date")
            )

            if ex_date is None:
                continue

            # Bought at formation-date close:
            # ex-date equal to the buy date was not earned.
            if not (
                actual_start_date
                < ex_date
                <= actual_end_date
            ):
                continue

            event_id = str(
                safe_get(item, "id", "") or ""
            )

            raw_cash = finite_float(
                safe_get(item, "cash_amount")
            )
            split_adjusted = finite_float(
                safe_get(
                    item,
                    "split_adjusted_cash_amount",
                )
            )

            currency = str(
                safe_get(item, "currency", "") or ""
            ).upper()

            if event_id:
                key = f"id:{event_id}"
            else:
                key = (
                    f"{ticker}|{ex_date}|"
                    f"{raw_cash}|"
                    f"{safe_get(item, 'pay_date', '')}"
                )

            if key in seen:
                continue

            seen.add(key)

            raw_rows.append(
                {
                    "ticker": ticker,
                    "ex_date": ex_date,
                    "raw_cash": raw_cash,
                    "split_adjusted_cash": (
                        split_adjusted
                    ),
                    "currency": currency,
                }
            )

    # Only fetch split history if at least one dividend needs a fallback.
    needs_split_fallback = any(
        row["split_adjusted_cash"] is None
        and row["raw_cash"] is not None
        for row in raw_rows
    )

    splits = []

    if needs_split_fallback:
        splits = collect_all_splits(
            client,
            aliases,
            warnings,
        )

    dividend_rows = []
    skipped_non_usd = 0

    for row in raw_rows:
        currency = row["currency"]

        # Do not mix foreign-currency cash directly with a USD share price.
        if currency and currency != "USD":
            skipped_non_usd += 1
            continue

        amount = row["split_adjusted_cash"]

        if amount is None:
            raw_cash = row["raw_cash"]

            if raw_cash is None:
                add_warning(
                    warnings,
                    "dividend_missing_cash_amount:"
                    f"{row['ticker']}:{row['ex_date']}",
                )
                continue

            factor = split_adjustment_factor_after(
                row["ex_date"],
                splits,
            )

            amount = raw_cash * factor

            add_warning(
                warnings,
                "used_manual_split_adjustment_for_dividend",
            )

        dividend_rows.append(
            {
                "ticker": row["ticker"],
                "ex_date": row["ex_date"],
                "amount": amount,
            }
        )

    if skipped_non_usd:
        add_warning(
            warnings,
            f"skipped_non_usd_dividends:"
            f"{skipped_non_usd}",
        )

    dividend_rows.sort(
        key=lambda x: (
            x["ex_date"],
            x["ticker"],
        )
    )

    return dividend_rows, skipped_non_usd


# ===========================================================================
# Per-company calculation
# ===========================================================================

def process_company(
    api_key: str,
    input_row: dict,
):
    client = get_worker_client(api_key)

    original_ticker = input_row[
        "ticker"
    ].strip()

    formation_date = date.fromisoformat(
        input_row["date"].strip()
    )

    target_end_date = add_years(
        formation_date,
        HOLD_YEARS,
    )

    result = dict(input_row)
    warnings = []

    result.update(
        {
            "method_version": METHOD_VERSION,
            "target_end_date": (
                target_end_date.isoformat()
            ),
            "actual_start_date": "",
            "actual_end_date": "",
            "start_ticker": original_ticker,
            "end_ticker": "",
            "ticker_aliases": "",
            "ticker_change_count": "",
            "identity_cik": "",
            "identity_share_class_figi": "",
            "identity_composite_figi": "",
            "delisted_within_holding_period": "",
            "delisted_date": "",
            "start_price": "",
            "end_price": "",
            "dividend_count": "",
            "dividend_cash_per_start_share": "",
            "non_usd_dividend_count_skipped": "0",
            "price_return": "",
            "dividend_return": "",
            "total_return": "",
            "price_return_pct": "",
            "dividend_return_pct": "",
            "total_return_pct": "",
            "status": "",
            "warnings": "",
            "error": "",
        }
    )

    try:
        fallback_cik = str(
            input_row.get("cik", "") or ""
        ).strip()

        # ---------------------------------------------------------------
        # 1) Identity
        # ---------------------------------------------------------------

        try:
            identity = get_formation_identity(
                client,
                original_ticker,
                formation_date,
                fallback_cik,
            )
        except Exception as exc:
            identity = {
                "cik": fallback_cik,
                "composite_figi": "",
                "share_class_figi": "",
                "primary_exchange": str(
                    input_row.get(
                        "primary_exchange",
                        "",
                    )
                    or ""
                ),
            }
            add_warning(
                warnings,
                "ticker_details_unavailable:"
                f"{type(exc).__name__}",
            )

        result["identity_cik"] = (
            identity["cik"]
        )
        result["identity_share_class_figi"] = (
            identity["share_class_figi"]
        )
        result["identity_composite_figi"] = (
            identity["composite_figi"]
        )

        # Experimental endpoint: optional only.
        event_identifier = (
            identity["composite_figi"]
            or original_ticker
        )

        events = try_get_ticker_events(
            client,
            event_identifier,
            warnings,
        )

        event_tickers = [
            event["ticker"]
            for event in events
        ]

        event_end_ticker = (
            ticker_at_date_from_events(
                original_ticker,
                events,
                target_end_date,
            )
        )

        preferred_tickers = {
            original_ticker,
            event_end_ticker,
            *event_tickers,
        }

        # ---------------------------------------------------------------
        # 2) Resolve ticker at five-year endpoint using CIK + share FIGI
        # ---------------------------------------------------------------

        active_at_end = []
        all_active_now = []
        all_inactive = []

        if identity["cik"]:
            active_at_end = (
                list_tickers_for_cik(
                    client,
                    identity["cik"],
                    active=True,
                    at_date=target_end_date,
                )
            )

            # These current active/inactive identity records give us aliases
            # and delisting dates. They are NOT individually treated as a
            # company failure.
            all_active_now = (
                list_tickers_for_cik(
                    client,
                    identity["cik"],
                    active=True,
                )
            )

            all_inactive = (
                list_tickers_for_cik(
                    client,
                    identity["cik"],
                    active=False,
                )
            )

        chosen_end_record = (
            choose_identity_ticker(
                active_at_end,
                identity,
                preferred_tickers,
            )
        )

        resolved_end_ticker = ""

        if chosen_end_record is not None:
            resolved_end_ticker = str(
                safe_get(
                    chosen_end_record,
                    "ticker",
                    "",
                )
                or ""
            ).strip()

        if not resolved_end_ticker:
            resolved_end_ticker = (
                event_end_ticker
                or original_ticker
            )

        aliases = aliases_from_identity_records(
            all_active_now,
            all_inactive,
            identity,
            original_ticker,
            event_tickers,
        )

        aliases.add(resolved_end_ticker)

        result["ticker_aliases"] = "|".join(
            sorted(aliases)
        )

        result["ticker_change_count"] = str(
            max(0, len(aliases) - 1)
        )

        # ---------------------------------------------------------------
        # 3) Delisting rule
        # ---------------------------------------------------------------

        # If the same share class is active at the target date, an old alias
        # being inactive is just a rename and must NOT trigger -100%.
        final_delisting = None

        # Only infer a final delisting when Massive reports NO active ticker
        # for this CIK at the target date. If active records exist but the
        # share class is ambiguous, do not falsely convert that ambiguity into
        # a -100% delisting.
        if not active_at_end:
            final_delisting = (
                find_final_delisting(
                    all_inactive,
                    identity,
                    aliases,
                    formation_date,
                    target_end_date,
                )
            )

        if final_delisting is not None:
            delisted_date, delisted_ticker = (
                final_delisting
            )

            result["end_ticker"] = (
                delisted_ticker
            )
            result["delisted_within_holding_period"] = (
                "true"
            )
            result["delisted_date"] = (
                delisted_date.isoformat()
            )

            # delisted during holding period => total return = -100%.
            result["price_return"] = (
                "-1.0000000000"
            )
            result["dividend_return"] = (
                "0.0000000000"
            )
            result["total_return"] = (
                "-1.0000000000"
            )

            result["price_return_pct"] = (
                "-100.000000"
            )
            result["dividend_return_pct"] = (
                "0.000000"
            )
            result["total_return_pct"] = (
                "-100.000000"
            )

            result["dividend_count"] = "0"
            result[
                "dividend_cash_per_start_share"
            ] = "0.0000000000"

            result["status"] = (
                "delisted_-100pct_rule"
            )
            result["warnings"] = ";".join(
                warnings
            )

            return result

        result["delisted_within_holding_period"] = (
            "false"
        )

        # ---------------------------------------------------------------
        # 4) Price return
        # ---------------------------------------------------------------

        (
            actual_start_date,
            start_price,
        ) = get_start_price(
            client,
            original_ticker,
            formation_date,
        )

        end_candidates = []

        for ticker in (
            resolved_end_ticker,
            event_end_ticker,
            original_ticker,
            *sorted(aliases),
        ):
            if (
                ticker
                and ticker not in end_candidates
            ):
                end_candidates.append(ticker)

        (
            actual_end_ticker,
            actual_end_date,
            end_price,
        ) = get_end_price_from_candidates(
            client,
            end_candidates,
            target_end_date,
        )

        result["end_ticker"] = (
            actual_end_ticker
        )
        result["actual_start_date"] = (
            actual_start_date.isoformat()
        )
        result["actual_end_date"] = (
            actual_end_date.isoformat()
        )
        result["start_price"] = (
            fmt_float(start_price)
        )
        result["end_price"] = (
            fmt_float(end_price)
        )

        price_return = (
            end_price / start_price - 1.0
        )

        # ---------------------------------------------------------------
        # 5) Dividend return
        # ---------------------------------------------------------------

        dividends, skipped_non_usd = (
            collect_dividends(
                client,
                aliases,
                actual_start_date,
                actual_end_date,
                warnings,
            )
        )

        total_dividends = sum(
            row["amount"]
            for row in dividends
        )

        dividend_return = (
            total_dividends / start_price
        )

        total_return = (
            price_return + dividend_return
        )

        result["dividend_count"] = str(
            len(dividends)
        )
        result[
            "non_usd_dividend_count_skipped"
        ] = str(skipped_non_usd)

        result[
            "dividend_cash_per_start_share"
        ] = fmt_float(total_dividends)

        result["price_return"] = (
            fmt_float(price_return)
        )
        result["dividend_return"] = (
            fmt_float(dividend_return)
        )
        result["total_return"] = (
            fmt_float(total_return)
        )

        result["price_return_pct"] = (
            f"{price_return * 100:.6f}"
        )
        result["dividend_return_pct"] = (
            f"{dividend_return * 100:.6f}"
        )
        result["total_return_pct"] = (
            f"{total_return * 100:.6f}"
        )

        # If foreign-currency dividends existed, the price return is valid,
        # but the total return is incomplete until FX conversion is added.
        if skipped_non_usd:
            result["status"] = (
                "ok_price_partial_dividend"
            )
        else:
            result["status"] = "ok"

        result["warnings"] = ";".join(
            warnings
        )

        return result

    except Exception as exc:
        result["status"] = "error"
        result["warnings"] = ";".join(
            warnings
        )
        result["error"] = (
            f"{type(exc).__name__}: {exc}"
        )
        return result


# ===========================================================================
# CSV persistence / resume
# ===========================================================================

ADDED_FIELDS = [
    "method_version",
    "target_end_date",
    "actual_start_date",
    "actual_end_date",
    "start_ticker",
    "end_ticker",
    "ticker_aliases",
    "ticker_change_count",
    "identity_cik",
    "identity_share_class_figi",
    "identity_composite_figi",
    "delisted_within_holding_period",
    "delisted_date",
    "start_price",
    "end_price",
    "dividend_count",
    "dividend_cash_per_start_share",
    "non_usd_dividend_count_skipped",
    "price_return",
    "dividend_return",
    "total_return",
    "price_return_pct",
    "dividend_return_pct",
    "total_return_pct",
    "status",
    "warnings",
    "error",
]


def row_key(row):
    return (
        str(row.get("year", "")),
        str(row.get("date", "")),
        str(row.get("rank", "")),
        str(row.get("ticker", "")),
    )


def load_csv(path: Path):
    with path.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as f:
        reader = csv.DictReader(f)
        return (
            reader.fieldnames or [],
            list(reader),
        )


def write_results_atomic(
    input_fieldnames,
    rows,
):
    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = list(input_fieldnames)

    for field in ADDED_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    try:
        with TEMP_FILE.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )

            writer.writeheader()

            for row in rows:
                writer.writerow(row)

        os.replace(
            TEMP_FILE,
            OUTPUT_FILE,
        )

    finally:
        if TEMP_FILE.exists():
            try:
                TEMP_FILE.unlink()
            except OSError:
                pass


# ===========================================================================
# Main
# ===========================================================================

def main():
    load_dotenv()

    api_key = os.getenv(
        "MASSIVE_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "MASSIVE_API_KEY was not found in .env"
        )

    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input CSV not found: {INPUT_FILE}"
        )

    (
        input_fieldnames,
        input_rows,
    ) = load_csv(INPUT_FILE)

    required = {
        "year",
        "date",
        "rank",
        "ticker",
    }

    missing = required - set(
        input_fieldnames
    )

    if missing:
        raise RuntimeError(
            "Input CSV is missing columns: "
            + ", ".join(sorted(missing))
        )


    existing_by_key = {}

    if OUTPUT_FILE.exists():
        _, old_rows = load_csv(
            OUTPUT_FILE
        )

        for row in old_rows:
            if (
                row.get("method_version")
                == METHOD_VERSION
                and row.get("status")
                in {
                    "ok",
                    "ok_price_partial_dividend",
                    "delisted_-100pct_rule",
                }
            ):
                existing_by_key[
                    row_key(row)
                ] = row

    final_by_key = dict(
        existing_by_key
    )

    pending = [
        row
        for row in input_rows
        if row_key(row)
        not in existing_by_key
    ]

    total = len(input_rows)
    already_done = (
        total - len(pending)
    )

    print(
        f"Method version: "
        f"{METHOD_VERSION}"
    )
    print(
        f"Input rows: {total:,}"
    )
    print(
        f"Already completed: "
        f"{already_done:,}"
    )
    print(
        f"Pending: {len(pending):,}"
    )
    print(
        f"Workers: {MAX_WORKERS}"
    )
    print()

    if not pending:
        print("Nothing to do.")
        return

    executor = ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    )

    futures = {}

    try:
        futures = {
            executor.submit(
                process_company,
                api_key,
                row,
            ): row_key(row)
            for row in pending
        }

        for completed, future in enumerate(
            as_completed(futures),
            start=1,
        ):
            key = futures[future]

            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "year": key[0],
                    "date": key[1],
                    "rank": key[2],
                    "ticker": key[3],
                    "method_version": (
                        METHOD_VERSION
                    ),
                    "status": "error",
                    "error": (
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    ),
                }

            final_by_key[key] = result

            print(
                f"[{already_done + completed:4d}"
                f"/{total:4d}] "
                f"{result.get('year', '')} "
                f"{result.get('ticker', ''):<8} "
                f"{result.get('status', '')}"
            )

            if result.get("error"):
                print(
                    "    ERROR: "
                    + result["error"]
                )

            if result.get("warnings"):
                print(
                    "    WARN: "
                    + result["warnings"]
                )

            ordered_rows = [
                final_by_key[
                    row_key(input_row)
                ]
                for input_row in input_rows
                if row_key(input_row)
                in final_by_key
            ]

            write_results_atomic(
                input_fieldnames,
                ordered_rows,
            )

    except KeyboardInterrupt:
        print()
        print("Interrupted by user.")
        print(
            "Cancelling pending requests..."
        )

        for future in futures:
            future.cancel()

        executor.shutdown(
            wait=False,
            cancel_futures=True,
        )

        print(
            "Completed rows remain "
            f"safely stored in {OUTPUT_FILE}"
        )
        return

    else:
        executor.shutdown(wait=True)

    ordered_rows = [
        final_by_key[row_key(row)]
        for row in input_rows
        if row_key(row) in final_by_key
    ]

    counts = {}

    for row in ordered_rows:
        status = row.get(
            "status",
            "",
        )
        counts[status] = (
            counts.get(status, 0) + 1
        )

    print()
    print("Finished.")

    for status, count in sorted(
        counts.items()
    ):
        print(
            f"{status}: {count:,}"
        )

    print(
        f"Results saved to: "
        f"{OUTPUT_FILE}"
    )

    if counts.get("error", 0):
        print(
            "Error rows will be retried "
            "automatically on the next run."
        )


if __name__ == "__main__":
    main()
