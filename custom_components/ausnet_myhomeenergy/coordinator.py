"""DataUpdateCoordinator for AusNet myHomeEnergy automatic data retrieval."""
from __future__ import annotations

import datetime as dt
import functools
import logging
from typing import Any

import aiohttp
import pytz

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .ausnet_client import AusNetAuthError, AusNetClient, AusNetDownloadError
from .const import (
    CONF_EMAIL,
    CONF_NMI,
    CONF_PASSWORD,
    CONF_SESSION_COOKIE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    FRIENDLY_EXPORT,
    FRIENDLY_IMPORT,
    STAT_ID_EXPORT,
    STAT_ID_IMPORT,
    UPDATE_INTERVAL_HOURS,
)
from .import_csv import _hourly_aggregate, _localize_safe, _parse_nem12_text

_LOGGER = logging.getLogger(__name__)

_TZ_NAME = "Australia/Melbourne"
_TZ = pytz.timezone(_TZ_NAME)

_CHANNELS = ("E1", "E2")


class AusNetCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that periodically downloads NEM12 data from myHomeEnergy.

    Each refresh:
    1. Authenticates (or reuses an existing valid session cookie).
    2. Determines the date range to fetch per channel based on the last statistic.
    3. Downloads NEM12 CSV for each channel and writes hourly statistics to HA.
    """

    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any]) -> None:
        self._email: str = entry_data[CONF_EMAIL]
        self._password: str = entry_data[CONF_PASSWORD]
        self._nmi: str = entry_data.get(CONF_NMI, "").strip()
        self._session_cookie: str = entry_data.get(CONF_SESSION_COOKIE, "").strip()

        # Dedicated session with unsafe cookie jar so the .ASPXAUTH cookie
        # is stored and forwarded correctly.
        self._session = async_create_clientsession(
            hass,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        self._client = AusNetClient(self._session, self._email, self._password)
        self._authenticated = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=dt.timedelta(hours=UPDATE_INTERVAL_HOURS),
        )

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _ensure_authenticated(self) -> None:
        if self._authenticated:
            return
        if self._session_cookie:
            await self._client.authenticate_with_cookie(self._session_cookie)
        else:
            await self._client.authenticate()
        self._authenticated = True

        if not self._nmi:
            discovered = await self._client.discover_nmi()
            if discovered:
                self._nmi = discovered
                _LOGGER.info("AusNet: auto-discovered NMI %s from portal", discovered)
            else:
                _LOGGER.warning(
                    "AusNet: NMI not configured and could not be auto-discovered. "
                    "Set it manually via the integration's Configure option."
                )

    # ------------------------------------------------------------------
    # Statistics helpers
    # ------------------------------------------------------------------

    async def _last_stat_date(self, statistic_id: str) -> dt.date | None:
        """Return the date of the most recent recorded sample, or None."""
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not last or statistic_id not in last:
            return None
        ts = last[statistic_id][0].get("start")
        if ts is None:
            return None
        return dt_util.utc_from_timestamp(float(ts)).astimezone(_TZ).date()

    async def _fetch_date_range(self, statistic_id: str) -> tuple[dt.date, dt.date]:
        """Return (start, end) date range to request for a channel.

        The portal lags by roughly one day so we cap end at yesterday.
        We overlap by 2 days so a partially-recorded previous day is filled.
        """
        end = dt.date.today() - dt.timedelta(days=1)
        last = await self._last_stat_date(statistic_id)
        if last is None:
            start = end - dt.timedelta(days=DEFAULT_HISTORY_DAYS)
        else:
            start = last - dt.timedelta(days=2)
        return min(start, end), end

    async def _last_cumulative_sum(self, statistic_id: str) -> float:
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not last or statistic_id not in last:
            return 0.0
        return float(last[statistic_id][0].get("sum") or 0.0)

    async def _write_stats_for_channel(
        self, nem12_text: str, channel: str, nmi_hint: str
    ) -> str:
        """Parse NEM12 text and write statistics.  Returns the resolved NMI."""
        nmi, interval_len, unit, day_values = await self.hass.async_add_executor_job(
            functools.partial(_parse_nem12_text, nem12_text, desired_channel=channel)
        )
        if nmi_hint:
            nmi = nmi_hint

        if channel == "E2":
            stat_id = STAT_ID_EXPORT.format(nmi=nmi)
            friendly = FRIENDLY_EXPORT.format(nmi=nmi)
        else:
            stat_id = STAT_ID_IMPORT.format(nmi=nmi)
            friendly = FRIENDLY_IMPORT.format(nmi=nmi)

        hourly = await self.hass.async_add_executor_job(
            functools.partial(_hourly_aggregate, day_values, unit=unit, interval_len=interval_len)
        )

        running = await self._last_cumulative_sum(stat_id)

        samples: list[StatisticData] = []
        for local_hour, kwh in sorted(hourly.items()):
            running += kwh
            local_dt = _localize_safe(local_hour, _TZ, _TZ_NAME)
            samples.append(StatisticData(start=dt_util.as_utc(local_dt), sum=running))

        if samples:
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=friendly,
                source=DOMAIN,
                statistic_id=stat_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, samples)
            _LOGGER.info(
                "AusNet: wrote %d hourly samples for NMI %s (%s) → %s",
                len(samples), nmi, channel, stat_id,
            )

        return nmi

    # ------------------------------------------------------------------
    # DataUpdateCoordinator contract
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Authenticate, download NEM12 data, write statistics."""
        try:
            await self._ensure_authenticated()
        except AusNetAuthError as exc:
            self._authenticated = False
            raise UpdateFailed(f"Portal authentication failed: {exc}") from exc

        nmi = self._nmi
        results: dict[str, Any] = {}

        for channel in _CHANNELS:
            placeholder_nmi = nmi or "unknown"
            stat_id = (
                STAT_ID_EXPORT.format(nmi=placeholder_nmi)
                if channel == "E2"
                else STAT_ID_IMPORT.format(nmi=placeholder_nmi)
            )
            start, end = await self._fetch_date_range(stat_id)
            _LOGGER.debug("AusNet: fetching %s for %s → %s", channel, start, end)

            # Download NEM12 CSV; re-authenticate once if the session expired.
            nem12_text: str | None = None
            try:
                nem12_text = await self._client.download_nem12(nmi, start, end, channel=channel)
            except AusNetAuthError:
                _LOGGER.debug("Session expired during download; re-authenticating")
                self._authenticated = False
                try:
                    await self._ensure_authenticated()
                    nem12_text = await self._client.download_nem12(nmi, start, end, channel=channel)
                except (AusNetAuthError, AusNetDownloadError) as exc:
                    raise UpdateFailed(
                        f"Re-auth after session expiry failed: {exc}"
                    ) from exc
            except AusNetDownloadError as exc:
                _LOGGER.warning("AusNet download error (%s): %s", channel, exc)

            if not nem12_text:
                _LOGGER.info(
                    "AusNet: NEM12 not available for channel %s "
                    "(download endpoint not yet confirmed for this account).",
                    channel,
                )
                results[channel] = "unavailable"
                continue

            try:
                resolved_nmi = await self._write_stats_for_channel(nem12_text, channel, nmi)
                # If we detected the NMI from the file and didn't have it yet, cache it.
                if not self._nmi and resolved_nmi:
                    self._nmi = resolved_nmi
                results[channel] = "ok"
            except ValueError as exc:
                _LOGGER.debug(
                    "AusNet: channel %s not in downloaded NEM12: %s", channel, exc
                )
                results[channel] = "not_found"
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("AusNet: stat write failed for %s: %s", channel, exc)
                results[channel] = "error"

        return results
