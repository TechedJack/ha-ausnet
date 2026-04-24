"""HTTP client for the AusNet myHomeEnergy portal (myhomeenergy.com.au)."""
from __future__ import annotations

import io
import logging
import re
from datetime import date, timedelta
from typing import Optional

import aiohttp
from yarl import URL

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

# Portal coordinates.  The login page GET/POST target and API root are the same
# Sitecore MVC site; the Sitecore Dashboard controller handles both auth and
# data-retrieval actions.
PORTAL_BASE = "https://myhomeenergy.com.au"
_LOGIN_URL = f"{PORTAL_BASE}/en"
_API_BASE = f"{PORTAL_BASE}/api/Sitecore/Dashboard"
_CHART_URL = "https://www.ausnetservices.com.au/api/Sitecore/Dashboard/GetDataForChart"
_MY_NMIS_URL = (
    f"{PORTAL_BASE}/AusNet-Services/Sites/AusNet-Corp-Website"
    "/Home/myHomeEnergy/Dashboard/My-NMIs"
)

# NEM12 download endpoint discovered via browser DevTools.
_START_DOWNLOAD_URL = f"{_API_BASE}/StartDownload"

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

        # Detect reCAPTCHA presence so we can give an actionable error later.
        has_recaptcha = bool(
            re.search(r"grecaptcha|g-recaptcha|recaptcha\.enterprise", html, re.I)
        )

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
            if has_recaptcha:
                raise AusNetAuthError(
                    "Login blocked by reCAPTCHA. To work around this: log in via "
                    "your browser, open Developer Tools (F12) → Application → "
                    "Cookies → myhomeenergy.com.au, copy the .ASPXAUTH cookie "
                    "value, and paste it into the 'Session cookie' field when "
                    "setting up this integration."
                )
            raise AusNetAuthError(
                "Login appeared to succeed but no session cookie was returned. "
                "Your email or password may be incorrect."
            )

    def _has_auth_cookie(self) -> bool:
        cookies = self._session.cookie_jar.filter_cookies(PORTAL_BASE)
        return ".ASPXAUTH" in {c.key for c in cookies.values()}

    async def authenticate_with_cookie(self, cookie_value: str) -> None:
        """Authenticate by injecting a pre-obtained .ASPXAUTH session cookie.

        Use this when reCAPTCHA blocks the normal email/password login flow.
        Obtain the value from your browser's Developer Tools → Application →
        Cookies → myhomeenergy.com.au after a manual login.

        Raises AusNetAuthError if the cookie is rejected by the portal.
        """
        self._session.cookie_jar.update_cookies(
            {".ASPXAUTH": cookie_value},
            URL(PORTAL_BASE),
        )
        # Verify the cookie by requesting the login page without following
        # redirects. ASP.NET Forms Authentication redirects authenticated users
        # away from the login URL; unauthenticated requests stay on the page.
        try:
            async with self._session.get(_LOGIN_URL, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    return  # Redirected away from login page → authenticated
                html = await resp.text()
                if 'name="Password"' in html:
                    raise AusNetAuthError(
                        "Session cookie is invalid or expired. "
                        "Log in via your browser, open Developer Tools (F12) → "
                        "Application → Cookies → myhomeenergy.com.au, and copy "
                        "a fresh .ASPXAUTH value."
                    )
                # 200 without the login form — assume authenticated
        except AusNetAuthError:
            raise
        except aiohttp.ClientConnectionError as exc:
            raise AusNetAuthError(f"Cannot reach myHomeEnergy portal: {exc}") from exc

    # ------------------------------------------------------------------
    # NMI discovery
    # ------------------------------------------------------------------

    async def discover_nmi(self) -> str | None:
        """Scrape the NMI from the portal's My NMIs page after authentication.

        Returns the NMI string, or None if it could not be found.
        """
        try:
            async with self._session.get(_MY_NMIS_URL) as resp:
                if resp.status != 200:
                    _LOGGER.debug("NMI discovery page returned HTTP %s", resp.status)
                    return None
                html = await resp.text()
        except aiohttp.ClientError as exc:
            _LOGGER.debug("NMI discovery request failed: %s", exc)
            return None

        # Try patterns that Sitecore/AusNet portals commonly use to embed the NMI.
        # Update these if the portal HTML structure changes.
        for pattern in [
            r'class="selectedNMI[^"]*"\s*>([A-Z0-9]{10,11})<',
            r'customerNMI["\']?\s*[:=]\s*["\']([A-Z0-9]{10,11})',
            r'data-nmi=["\']([A-Z0-9]{10,11})["\']',
            r'"NMI"\s*:\s*"([A-Z0-9]{10,11})"',
            r"'NMI'\s*:\s*'([A-Z0-9]{10,11})'",
            r'\bNMI[:\s]+([0-9]{10,11})\b',
            r'CallDownloadHandler[^)]*NMI[^)]*["\']([A-Z0-9]{10,11})["\']',
        ]:
            m = re.search(pattern, html, re.I)
            if m:
                nmi = m.group(1).upper()
                _LOGGER.debug("NMI discovery matched pattern %r → %s", pattern, nmi)
                return nmi

        _LOGGER.debug(
            "NMI discovery: no pattern matched in page (length %d chars)", len(html)
        )
        return None

    # ------------------------------------------------------------------
    # NEM12 download
    # ------------------------------------------------------------------

    async def download_nem12(
        self,
        nmi: str,
        start: date,
        end: date,
        channel: str = "E1",
    ) -> Optional[str]:
        """Attempt to download a NEM12 CSV file for the given NMI and date range.

        Returns the raw CSV text on success, or ``None`` if the endpoint fails.
        """
        try:
            primary_params: dict[str, str] = {
                "NMI": nmi,
                "fileType": "NEM12",
                "isDownload": "true",
                "channel": channel,
            }
            async with self._session.get(_START_DOWNLOAD_URL, params=primary_params) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    stripped = text.lstrip()
                    if stripped.startswith("100,") or stripped.startswith("200,"):
                        _LOGGER.debug("NEM12 download succeeded via StartDownload")
                        return text
                    _LOGGER.debug(
                        "StartDownload returned 200 but content is not NEM12 "
                        "(first 40 chars: %r)", text[:40],
                    )
                else:
                    _LOGGER.debug("StartDownload → HTTP %s", resp.status)
        except aiohttp.ClientError as exc:
            _LOGGER.debug("StartDownload error: %s", exc)

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
