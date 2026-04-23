from __future__ import annotations

import csv
import datetime as dt
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pytz

from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfEnergy
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics

from .const import DOMAIN, STAT_ID_IMPORT, STAT_ID_EXPORT, FRIENDLY_IMPORT, FRIENDLY_EXPORT

_LOGGER = logging.getLogger(__name__)

# --- NEM12 format notes ---
# 200 record: "200,<NMI>,<RegisterId/Channel>,...,<MeterSerial>,<Unit>,<IntervalLenMins>,..."
# 300 record: "300,<YYYYMMDD>,<v1>..<vN>,<QualityFlag>"
#
# This integration:
# 1) Detects NMI/unit/interval from the first 200 record matching the desired channel (E1 by default).
# 2) Reads 300 rows for that channel.
# 3) Converts per-interval kWh deltas to hourly kWh by summing intervals per hour.
# 4) Builds an increasing cumulative `sum` and writes external statistics to HA's Energy dashboard.
#
# TODO: Automate data retrieval from myhomeenergy.ausnet.com.au instead of requiring a manual CSV
#       download. This would require:
#       - A config flow to securely store user credentials (username / password).
#       - An aiohttp-based session to authenticate and download the NEM12 file on demand.
#       - A scheduled coordinator (or on-demand service call) to pull fresh data.


def _parse_nem12(
    file_path: Path,
    desired_channel: str | None = "E1",
) -> Tuple[str, int, str, Dict[dt.date, List[float]]]:
    """Parse a NEM12 CSV file and return (nmi, interval_len, unit, {date: [values]}).

    desired_channel: "E1" (import) or "E2" (export), or None to accept the first channel found.
    """
    nmi_detected: str | None = None
    interval_len: int | None = None
    unit: str | None = None
    day_values: Dict[dt.date, List[float]] = defaultdict(list)
    current_channel: str | None = None

    with file_path.open(newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if not raw or not raw[0].strip():
                continue
            rec = raw[0].strip()

            if rec == "200":
                # NEM12 200 record layout (permissive parsing):
                #   200,<NMI>,<channel>,<channel>,<channel>,,<MeterSerial>,<Unit>,<IntervalLen>,...
                try:
                    nmi = raw[1].strip()
                except IndexError:
                    continue

                # Channel: first token in columns 2-7 that is E1 or E2
                ch = None
                for t in raw[2:8]:
                    t = (t or "").strip()
                    if t in ("E1", "E2"):
                        ch = t
                        break

                # Unit: first token anywhere in the row that looks like an energy unit
                u = None
                for t in raw:
                    if (t or "").strip().upper() in ("KWH", "WH", "MWH"):
                        u = (t or "").strip().upper()

                # Interval length (minutes): last numeric field in the row
                iv = None
                for t in reversed(raw):
                    t = (t or "").strip()
                    if t.isdigit():
                        iv = int(t)
                        break

                if desired_channel is None or ch == desired_channel:
                    current_channel = ch
                    nmi_detected = nmi
                    unit = u or unit
                    interval_len = iv or interval_len
                else:
                    current_channel = None

            elif rec == "300" and current_channel is not None:
                # 300 record: 300,YYYYMMDD,<value>,...,<QualityFlag>
                if len(raw) < 3:
                    continue
                try:
                    date_str = raw[1].strip()
                    day = dt.date(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]))
                except Exception:
                    continue

                # Values occupy columns 2..-1 (last column is the quality flag)
                vals: List[float] = []
                for t in raw[2:-1]:
                    t = (t or "").strip()
                    try:
                        vals.append(float(t) if t else 0.0)
                    except ValueError:
                        vals.append(0.0)
                day_values[day] = vals

    if nmi_detected is None or interval_len is None or unit is None:
        raise ValueError(
            f"Could not find NEM12 200/300 records for channel '{desired_channel}'. "
            "Verify the file format and channel setting."
        )

    return nmi_detected, interval_len, unit, day_values


def _to_kwh(value: float, unit: str) -> float:
    unit = unit.upper()
    if unit == "WH":
        return value / 1000.0
    if unit == "MWH":
        return value * 1000.0
    return value  # KWH or unknown — pass through


def _hourly_aggregate(
    date_vals: Dict[dt.date, List[float]],
    unit: str,
    interval_len: int,
) -> Dict[dt.datetime, float]:
    """Aggregate per-interval readings into hourly kWh totals.

    Uses timedelta arithmetic so DST days with 23 or 25 intervals are handled
    without hitting datetime's hour-must-be-0..23 constraint.
    """
    if interval_len not in (15, 30, 60):
        raise ValueError(f"Unsupported interval length: {interval_len} minutes")

    result: Dict[dt.datetime, float] = {}
    per_hour = 60 // interval_len

    for day, vals in sorted(date_vals.items()):
        if not vals:
            continue
        day_start = dt.datetime(day.year, day.month, day.day, 0, 0, 0)
        total_hours = len(vals) // per_hour
        for h in range(total_hours):
            start_idx = h * per_hour
            hour_kwh = sum(_to_kwh(v, unit) for v in vals[start_idx : start_idx + per_hour])
            result[day_start + dt.timedelta(hours=h)] = hour_kwh

    return result


def _localize_safe(naive: dt.datetime, tz: pytz.BaseTzInfo, tz_name: str) -> dt.datetime:
    """Localize a naive datetime, handling DST edge cases gracefully."""
    try:
        return tz.localize(naive, is_dst=None)
    except pytz.AmbiguousTimeError:
        # Clocks fall back — two local times map to same UTC; pick standard time (post-transition)
        _LOGGER.debug("Ambiguous local time %s in %s (DST fall-back); using standard time", naive, tz_name)
        return tz.localize(naive, is_dst=False)
    except pytz.NonExistentTimeError:
        # Clocks spring forward — this local time was skipped; use DST side of the gap
        _LOGGER.debug("Non-existent local time %s in %s (DST spring-forward); advancing past gap", naive, tz_name)
        return tz.localize(naive, is_dst=True)


async def handle_import_service(hass: HomeAssistant, data: dict) -> None:
    """Service handler for ausnet_myhomeenergy.import_csv.

    Expected data keys:
      file_path    (str, required)  — absolute path to the NEM12 CSV
      timezone     (str, optional)  — pytz timezone name, default "Australia/Melbourne"
      nmi_override (str, optional)  — override the NMI detected from the CSV
      channel      (str, optional)  — "E1" (import, default) or "E2" (export)
    """
    file_path = Path(data["file_path"])
    tz_name: str = data.get("timezone") or "Australia/Melbourne"
    desired_channel: str = (data.get("channel") or "E1").upper()

    if desired_channel not in ("E1", "E2"):
        raise ValueError(f"Invalid channel '{desired_channel}'. Must be 'E1' or 'E2'.")

    if not file_path.exists():
        raise FileNotFoundError(f"NEM12 file not found: {file_path}")

    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError as err:
        raise ValueError(f"Unknown timezone '{tz_name}': {err}") from err

    _LOGGER.info("Parsing NEM12 file %s (channel=%s, tz=%s)", file_path, desired_channel, tz_name)

    nmi, interval_len, unit, day_values = _parse_nem12(file_path, desired_channel=desired_channel)

    if data.get("nmi_override"):
        nmi = str(data["nmi_override"]).strip()

    _LOGGER.debug("NMI=%s  interval=%d min  unit=%s  days=%d", nmi, interval_len, unit, len(day_values))

    hourly = _hourly_aggregate(day_values, unit=unit, interval_len=interval_len)

    # Build a monotonically increasing cumulative sum required by the Energy dashboard
    samples: list[StatisticData] = []
    running = 0.0
    for local_hour, kwh in sorted(hourly.items()):
        running += kwh
        local_dt = _localize_safe(local_hour, tz, tz_name)
        utc_start = dt_util.as_utc(local_dt)
        samples.append(StatisticData(start=utc_start, sum=running))

    if desired_channel == "E2":
        statistic_id = STAT_ID_EXPORT.format(nmi=nmi)
        friendly_name = FRIENDLY_EXPORT.format(nmi=nmi)
    else:
        statistic_id = STAT_ID_IMPORT.format(nmi=nmi)
        friendly_name = FRIENDLY_IMPORT.format(nmi=nmi)

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=friendly_name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )

    async_add_external_statistics(hass, metadata, samples)
    _LOGGER.info(
        "Imported %d hourly samples for NMI %s (%s) → statistic '%s'",
        len(samples),
        nmi,
        desired_channel,
        statistic_id,
    )
