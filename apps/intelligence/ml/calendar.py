"""Calendar features relevant to Kigali retail demand."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

# Fixed-date public holidays in Rwanda. Moveable feasts (Good Friday, Easter Monday,
# Eid al-Fitr, Eid al-Adha) should be added per year through EXTRA_HOLIDAYS when known.
FIXED_HOLIDAYS = [
    (1, 1), (1, 2),   # New Year
    (2, 1),           # National Heroes' Day
    (4, 7),           # Genocide against the Tutsi Memorial Day
    (5, 1),           # Labour Day
    (7, 1),           # Independence Day
    (7, 4),           # Liberation Day
    (8, 15),          # Assumption Day
    (12, 25), (12, 26),
]
EXTRA_HOLIDAYS: set[date] = set()


def _umuganura(year: int) -> date:
    """Umuganura (harvest day): first Friday of August."""
    d = date(year, 8, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


@lru_cache(maxsize=64)
def holidays_for_year(year: int) -> frozenset:
    days = {date(year, m, d) for m, d in FIXED_HOLIDAYS}
    days.add(_umuganura(year))
    days |= {d for d in EXTRA_HOLIDAYS if d.year == year}
    return frozenset(days)


def is_holiday(d: date) -> bool:
    return d in holidays_for_year(d.year)


def calendar_features(dates: pd.DatetimeIndex, origin: pd.Timestamp) -> pd.DataFrame:
    """Deterministic, leakage-free features known in advance for any future date."""
    dow = dates.dayofweek
    feats = {f"dow_{i}": (dow == i).astype(float) for i in range(7)}
    dom = dates.day
    feats["payday"] = ((dom >= 25) | (dom <= 5)).astype(float)
    feats["holiday"] = np.array([is_holiday(d.date()) for d in dates], dtype=float)
    feats["pre_holiday"] = np.array([is_holiday((d + pd.Timedelta(days=1)).date()) for d in dates], dtype=float)
    feats["month_sin"] = np.sin(2 * np.pi * dates.month / 12)
    feats["month_cos"] = np.cos(2 * np.pi * dates.month / 12)
    feats["trend"] = ((dates - origin).days / 365.0).astype(float)
    return pd.DataFrame(feats, index=dates)
