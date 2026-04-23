from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pytz

from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfEnergy
from homeassistant.util import dt as dt_util

# external stats writer
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)

from .const import DOMAIN, STAT_ID_IMPORT, FRIENDLY_IMPORT

# --- NEM12 quick notes ---
# 200 record: "200,<NMI>,<RegisterId/Channel>,...,<Meter Serial>,<Unit>,<IntervalLenMins>,..."
# 300 record: "300,<YYYYMMDD>,<v1>..<v48>,<QualityFlag>"
#
# We will:
# 1) detect NMI/unit/interval on first 200 matching desired channel (E1 by default),
# 2) read 300 rows for that channel,
# 3) convert 30-min kWh deltas to HOURLY kWh by summing pairs,
# 4) build an increasing cumulative `sum` and write external statistics.

def _parse_nem12(
    file_path: Path,
    desired_channel: str | None = "E1",
) -> Tuple[str, int, str, Dict[dt.date, List[float]]]:
    """
    Returns (nmi, interval_len, unit, {date: [48 values]})
    - desired_channel: "E1" (import) or "E2" (export) typically.
    """
    nmi_detected: str | None = None
    interval_len: int | None = None
    unit: str | None = None

    # Each date -> list of floats (48 values if 30-min intervals)
    day_values: Dict[dt.date, List[float]] = defaultdict(list)

    current_channel: str | None = None

    with file_path.open(newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if not raw or not raw[0].strip():
                continue
            rec = raw[0].strip()
            if rec == "200":
                # NEM12 200: NMI in col2, channel in col3 (often), unit near end, interval at the end
                # Your sample looked like: 200,6306004387,E1,E1,E1,,4794345,KWH,30,.... (lots of blanks)
                # We will be permissive and extract known bits safely.
                try:
                    nmi = raw[1].strip()
                except IndexError:
                    continue
                # find first token that resembles channel E1/E2 in the next few fields
                ch = None
                for t in raw[2:8]:
                    t = (t or "").strip()
                    if t in ("E1", "E2"):
                        ch = t
                        break
                # find unit
                u = None
                for t in raw:
                    if (t or "").strip().upper() in ("KWH", "WH", "MWH"):
                        u = (t or "").strip().upper()
                # find interval length (last numeric field usually)
                iv = None
                for t in reversed(raw):
                    t = (t or "").strip()
                    if t.isdigit():
                        iv = int(t)
                        break

                # If this 200 matches the desired channel, set current_channel so following 300s are captured
                if desired_channel is None or ch == desired_channel:
                    current_channel = ch
                    nmi_detected = nmi
                    unit = u or unit
                    interval_len = iv or interval_len
                else:
                    current_channel = None

            elif rec == "300" and current_channel is not None:
                # 300,YYYYMMDD,<48 vals>,<Q>
                if len(raw) < 3:
                    continue
                try:
                    date_str = raw[1].strip()
                    y = int(date_str[0:4])
                    m = int(date_str[4:6])
                    d = int(date_str[6:8])
                    day = dt.date(y, m, d)
                except Exception:
                    continue

                # values are columns 2..49 for 30-min, with the last col being quality
                vals = []
                for t in raw[2:-1]:
                    t = (t or "").strip()
                    if t == "":
                        vals.append(0.0)
                    else:
                        try:
                            vals.append(float(t))
                        except ValueError:
                            vals.append(0.0)
                day_values[day] = vals

    if nmi_detected is None or interval_len is None or unit is None:
        raise ValueError("Could not parse NEM12 200/300 records for the requested channel.")

    return nmi_detected, interval_len, unit, day_values


def _to_kwh(value: float, unit: str) -> float:
    unit = unit.upper()
    if unit == "KWH":
        return value
    if unit == "WH":
        return value / 1000.0
    if unit == "MWH":
        return value * 1000.0
    # assume already kWh
    return value


def _hourly_aggregate_from_30min(
    date_vals: Dict[dt.date, List[float]],
    unit: str,
    interval_len: int,
) -> Dict[dt.datetime, float]:
    """
    Convert per-interval (e.g., 30-min) kWh to hourly kWh.
    Returns dict of local-hour-start -> kWh for that hour.
    """
    result: Dict[dt.datetime, float] = {}
    if interval_len not in (15, 30, 60):
        raise ValueError(f"Unsupported interval length: {interval_len}")

    for day, vals in sorted(date_vals.items()):
        if not vals:
            continue
        # number of intervals per hour
        per_hour = 60 // interval_len
        total_intervals = len(vals)
        hours_in_day = total_intervals // per_hour
        for h in range(hours_in_day):
            start_index = h * per_hour
            hour_sum_kwh = sum(_to_kwh(v, unit) for v in vals[start_index:start_index + per_hour])
            result[dt.datetime(day.year, day.month, day.day, h, 0, 0)] = hour_sum_kwh
    return result


async def handle_import_service(hass: HomeAssistant, data: dict) -> None:
    """
    Expected data:
      file_path: str
      timezone:  str  (default Australia/Melbourne)
      nmi_override: Optional[str]
      channel: "E1" (import) or "E2" (export)
    """
    file_path = Path(data["file_path"])
    tz_name = data.get("timezone") or "Australia/Melbourne"
    desired_channel = data.get("channel", "E1")

    tz = pytz.timezone(tz_name)

    nmi, interval_len, unit, day_values = _parse_nem12(file_path, desired_channel=desired_channel)
    if data.get("nmi_override"):
        nmi = data["nmi_override"]

    hourly = _hourly_aggregate_from_30min(day_values, unit=unit, interval_len=interval_len)

    # Build cumulative sum series (Energy dashboard consumes 'sum' increasing)
    samples = []
    running = 0.0

    # Sort by local time
    for local_hour, kwh in sorted(hourly.items()):
        running += kwh
        # local -> aware -> UTC
        local_dt = tz.localize(local_hour, is_dst=None)
        utc_start = dt_util.as_utc(local_dt)
        samples.append({
            "start": utc_start,
            "sum": running,
        })

    statistic_id = STAT_ID_IMPORT.format(nmi=nmi)
    metadata = {
        "has_mean": False,
        "has_sum": True,
        "name": FRIENDLY_IMPORT.format(nmi=nmi),
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    }

    # Write stats
    async_add_external_statistics(hass, metadata, samples)
