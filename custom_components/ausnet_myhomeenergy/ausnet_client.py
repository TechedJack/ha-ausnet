"""HTTP client for the AusNet myHomeEnergy portal (myhomeenergy.com.au)."""
from __future__ import annotations

import io
import logging
import re
from datetime import date, timedelta
from typing import Optional

import aiohttp

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Portal coordinates.  The login page GET/POST target and API root are the same
# Sitecore MVC site; the Sitecore Dashboard controller handles both auth and
# data-retrieval actions.
PORTAL_BASE = "https://myhomeenergy.com.au"
_LOGIN_URL = f"{PORTAL_BASE}/en"
_API_BASE = f"{PORTAL_BASE}/api/Sitecore/Dashboard"
_CHART_URL = "https://www.ausnetservices.com.au/api/Sitecore/Dashboard/GetDataForChart"

# NEM12 download: tried in order; the first that returns valid NEM12 content wins.
_NEM12_CANDIDATES = [
    f"{_API_BASE}/GetDownloadData",
    f"{_API_BASE}/DownloadIntervalData",
    f"{_API_BASE}/GetNEM12Data",
    f"{_API_BASE}/ExportIntervalData",
    f"{_API_BASE}/GetDataForDownload",
]

# How much history to fetch on first sync (days).
DEFAULT_HISTORY_DAYS = 90


class AusNetAuthError(HomeAssistantError):
    """Raised when credentials are wrong or the session could not be established."""


class AusNetDownloadError(HomeAssistantError):
    """Raised when meter data could not be fetched from the portal."""


class AusNetClient:
    """Authenticated session wrapper for myhomeenergy.com.au.

    Callers must pass an ``aiohttp.ClientSession`` that persists cookies
    across calls (use ``aiohttp.CookieJar`` with ``unsafe=True`` so the
    .ASPXAUTH cookie is stored for the ausnetservices.com.au sub-domain).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Log in and persist the .ASPXAUTH session cookie.

        Raises AusNetAuthError on bad credentials or network failure.
        """
        # Fetch the login page to capture any ASP.NET anti-forgery token.
        try:
            async with self._session.get(_LOGIN_URL, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise AusNetAuthError(
                        f"Login page returned HTTP {resp.status}. "
                        "The portal URL may have changed."
                    )
                html = await resp.text()
        except aiohttp.ClientConnectionError as exc:
            raise AusNetAuthError(f"Cannot reach myHomeEnergy portal: {exc}") from exc

        # Extract __RequestVerificationToken if present.
        token_match = re.search(
            r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]*)"',
            html,
        ) or re.search(
            r'<input[^>]+value="([^"]*)"[^>]+name="__RequestVerificationToken"',
            html,
        )
        antiforgery = token_match.group(1) if token_match else ""

        form: dict[str, str] = {
            "Email": self._email,
            "Password": self._password,
        }
        if antiforgery:
            form["__RequestVerificationToken"] = antiforgery

        # POST credentials.
        try:
            async with self._session.post(
                _LOGIN_URL,
                data=form,
                allow_redirects=True,
                headers={"Referer": _LOGIN_URL},
            ) as resp:
                status = resp.status
                await resp.read()  # drain body
        except aiohttp.ClientError as exc:
            raise AusNetAuthError(f"Login POST failed: {exc}") from exc

        if status not in (200, 302):
            raise AusNetAuthError(
                f"Login returned unexpected HTTP {status}. Check credentials."
            )

        # Confirm the auth cookie was set.
        if not self._has_auth_cookie():
            raise AusNetAuthError(
                "Login appeared to succeed but no session cookie was returned. "
                "Your email or password may be incorrect, or the portal may have "
                "added CAPTCHA protection that blocks automated login."
            )

    def _has_auth_cookie(self) -> bool:
        cookies = self._session.cookie_jar.filter_cookies(PORTAL_BASE)
        return ".ASPXAUTH" in {c.key for c in cookies.values()}

    # ------------------------------------------------------------------
    # NEM12 download
    # ------------------------------------------------------------------

    async def download_nem12(
        self,
        nmi: str,
        start: date,
        end: date,
    ) -> Optional[str]:
        """Attempt to download a NEM12 CSV file for the given NMI and date range.

        Tries each candidate endpoint in order.  Returns the raw CSV text if a
        valid NEM12 file is found, or ``None`` if all candidates fail.
        """
        params: dict[str, str] = {
            "customerNMI": nmi,
            "startdate": start.strftime("%Y%m%d"),
            "enddate": end.strftime("%Y%m%d"),
        }

        for url in _NEM12_CANDIDATES:
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status != 200:
                        _LOGGER.debug("NEM12 candidate %s → HTTP %s", url, resp.status)
                        continue
                    text = await resp.text()
                    # NEM12 files always begin with "100,"
                    if text.lstrip().startswith("100,"):
                        _LOGGER.debug("NEM12 download succeeded via %s", url)
                        return text
                    _LOGGER.debug(
                        "NEM12 candidate %s returned 200 but content is not NEM12 "
                        "(first 40 chars: %r)",
                        url, text[:40],
                    )
            except aiohttp.ClientError as exc:
                _LOGGER.debug("NEM12 candidate %s error: %s", url, exc)

        return None

    # ------------------------------------------------------------------
    # JSON chart data (known-working fallback)
    # ------------------------------------------------------------------

    async def fetch_usage_json(
        self,
        nmi: str,
        start: date,
        end: date,
    ) -> dict:
        """Fetch aggregated usage data from the Sitecore Dashboard chart API.

        Returns the raw JSON dict as received from the server.
        Raises AusNetDownloadError on failure.
        """
        time_slices = (
            f'[{{"START_DT":"{start.strftime("%Y%m%d")}",'
            f'"END_DT":"{end.strftime("%Y%m%d")}"}}]'
        )
        params: dict[str, str] = {
            "customerNMI": nmi,
            "startdate": start.strftime("%Y%m%d"),
            "enddate": end.strftime("%Y%m%d"),
            "chartView": "W",
            "typeOfData": "netUsage",
            "timeSlicesArray": time_slices,
            "rateConFlat": "0",
        }
        try:
            async with self._session.get(_CHART_URL, params=params) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise AusNetDownloadError(f"Chart API request failed: {exc}") from exc
        except Exception as exc:
            raise AusNetDownloadError(f"Chart API response could not be parsed: {exc}") from exc
