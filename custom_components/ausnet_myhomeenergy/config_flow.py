"""Config flow for AusNet myHomeEnergy."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .ausnet_client import AusNetAuthError, AusNetClient
from .const import (
    CONF_EMAIL,
    CONF_NMI,
    CONF_PASSWORD,
    CONF_SESSION_COOKIE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_NMI, default=""): str,
        vol.Optional(CONF_SESSION_COOKIE, default=""): str,
    }
)


class AusNetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AusNet myHomeEnergy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step where the user enters their credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email: str = user_input[CONF_EMAIL].strip()
            password: str = user_input[CONF_PASSWORD]
            nmi: str = user_input.get(CONF_NMI, "").strip()
            session_cookie: str = user_input.get(CONF_SESSION_COOKIE, "").strip()

            # Prevent duplicate config entries for the same account.
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            session = async_create_clientsession(
                self.hass,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
            client = AusNetClient(session, email, password)
            try:
                if session_cookie:
                    await client.authenticate_with_cookie(session_cookie)
                else:
                    await client.authenticate()
            except AusNetAuthError as exc:
                _LOGGER.warning("AusNet authentication failed: %s", exc)
                errors["base"] = "invalid_cookie" if session_cookie else "invalid_auth"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during AusNet authentication")
                errors["base"] = "cannot_connect"

            if not errors:
                entry_data: dict[str, Any] = {
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    CONF_NMI: nmi,
                }
                if session_cookie:
                    entry_data[CONF_SESSION_COOKIE] = session_cookie
                return self.async_create_entry(title=email, data=entry_data)

        return self.async_show_form(
            step_id="user",
            data_schema=_STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={},
        )
