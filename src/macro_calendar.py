"""
Macro Event Calendar — suppress signals on days when scheduled macro data
releases dominate price action (CPI, FOMC, NFP).

flow_divergence drops from 73.0% → 43.8% on event days (29pp degradation).
By flagging these days, strategies can skip unreliable signals.

Usage:
    from macro_calendar import get_event_dates, is_macro_day, add_macro_flag

    # Check a single date
    is_macro_day("2026-03-11")  # True (CPI release)

    # Add flag to dataset
    df = add_macro_flag(df)  # adds 'is_macro_event' boolean column
"""

import pandas as pd
from datetime import timedelta


# ---------------------------------------------------------------------------
# Scheduled macro events (US, affects BTC most)
# ---------------------------------------------------------------------------

# CPI releases (Bureau of Labor Statistics, ~10th-15th of each month, 8:30am ET)
CPI_DATES = [
    # 2022
    "2022-01-12", "2022-02-10", "2022-03-10", "2022-04-12", "2022-05-11",
    "2022-06-10", "2022-07-13", "2022-08-10", "2022-09-13", "2022-10-13",
    "2022-11-10", "2022-12-13",
    # 2023
    "2023-01-12", "2023-02-14", "2023-03-14", "2023-04-12", "2023-05-10",
    "2023-06-13", "2023-07-12", "2023-08-10", "2023-09-13", "2023-10-12",
    "2023-11-14", "2023-12-12",
    # 2024
    "2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15",
    "2024-06-12", "2024-07-11", "2024-08-14", "2024-09-11", "2024-10-10",
    "2024-11-13", "2024-12-11",
    # 2025
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13",
    "2025-06-11", "2025-07-11", "2025-08-12", "2025-09-10", "2025-10-14",
    "2025-11-12", "2025-12-10",
    # 2026
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-14", "2026-05-12",
    "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-16", "2026-10-13",
    "2026-11-12", "2026-12-10",
]

# FOMC rate decisions (8 per year, 2:00pm ET)
FOMC_DATES = [
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27",
    "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26",
    "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-17",
    # 2026
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-11-04", "2026-12-16",
]

# Non-Farm Payrolls (first Friday of each month, 8:30am ET)
# Computed programmatically to avoid hardcoded date errors
def _compute_first_fridays(start_year=2022, end_year=2027):
    """Compute the first Friday of every month in the given range."""
    from datetime import date
    dates = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # Find the first Friday: day 1 of the month, advance to Friday
            d = date(year, month, 1)
            # weekday(): Monday=0, Friday=4
            days_until_friday = (4 - d.weekday()) % 7
            first_friday = d.replace(day=1 + days_until_friday)
            dates.append(first_friday.isoformat())
    return dates

NFP_DATES = _compute_first_fridays(2022, 2027)


def get_event_dates(include_next_day=True):
    """
    Return set of dates that are macro event days.
    include_next_day: also flag the day after (reaction/follow-through day).
    """
    dates = set()
    for d_str in CPI_DATES + FOMC_DATES + NFP_DATES:
        d = pd.Timestamp(d_str).date()
        dates.add(d)
        if include_next_day:
            # Skip to next weekday (Mon-Fri) for the follow-through day
            next_d = d + timedelta(days=1)
            while next_d.weekday() >= 5:  # Saturday=5, Sunday=6
                next_d += timedelta(days=1)
            dates.add(next_d)
    return dates


def is_macro_day(date_str):
    """Check if a date string is a macro event day."""
    d = pd.Timestamp(date_str).date()
    return d in get_event_dates()


def get_event_type(date_str):
    """Return the type of macro event for a given date, or None."""
    d = pd.Timestamp(date_str).date()
    d_str = str(d)
    prev_d_str = str(d - timedelta(days=1))

    for check in [d_str, prev_d_str]:
        if check in CPI_DATES:
            return "CPI"
        if check in FOMC_DATES:
            return "FOMC"
        if check in NFP_DATES:
            return "NFP"
    return None


def add_macro_flag(df, date_col="trade_date", include_next_day=True):
    """
    Add 'is_macro_event' and 'macro_event_type' columns to a DataFrame.
    """
    events = get_event_dates(include_next_day)
    is_macro = df[date_col].dt.date.isin(events)

    def _get_type(dt):
        return get_event_type(str(dt.date())) if hasattr(dt, 'date') else None
    event_type = df[date_col].apply(_get_type)

    import pandas as _pd
    macro_cols = {"is_macro_event": is_macro, "macro_event_type": event_type}
    existing = [c for c in df.columns if c in macro_cols]
    if existing:
        df = df.drop(columns=existing)
    df = _pd.concat([df, _pd.DataFrame(macro_cols, index=df.index)], axis=1)

    n_events = is_macro.sum()
    pct = n_events / len(df) * 100
    print(f"[macro] Flagged {n_events}/{len(df)} days as macro events ({pct:.0f}%)")
    return df
