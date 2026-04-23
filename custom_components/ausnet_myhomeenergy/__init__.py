from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from .const import DOMAIN
from .import_csv import handle_import_service

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def _svc(call: ServiceCall) -> None:
        await handle_import_service(hass, call.data)

    hass.services.async_register(
        DOMAIN,
        "import_csv",
        _svc,
    )
    _LOGGER.info("Registered %s.import_csv service", DOMAIN)
    return True
