"""AusNet myHomeEnergy integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import AusNetCoordinator
from .import_csv import handle_import_service

_LOGGER = logging.getLogger(__name__)

AusNetConfigEntry = ConfigEntry  # runtime type alias


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the manual import_csv service (YAML-config / service-call path)."""
    async def _svc(call: ServiceCall) -> None:
        await handle_import_service(hass, call.data)

    hass.services.async_register(DOMAIN, "import_csv", _svc)
    _LOGGER.info("Registered %s.import_csv service", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AusNetConfigEntry) -> bool:
    """Set up the coordinator for an auto-download config entry."""
    coordinator = AusNetCoordinator(hass, dict(entry.data))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AusNetConfigEntry) -> bool:
    """Unload a config entry (nothing platform-specific to unload)."""
    return True
