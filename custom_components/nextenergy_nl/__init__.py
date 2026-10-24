"""NextEnergy (NL) integration: day-old smart-meter usage from the NextEnergy app backend."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AuthenticationError, NextEnergyClient, NextEnergyError
from .const import CONF_CONTRACT_ID
from .coordinator import NextEnergyConfigEntry, NextEnergyCoordinator, contract_from_entry

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: NextEnergyConfigEntry) -> bool:
    """Log in, verify the contract and start the daily import."""
    session = async_create_clientsession(hass)
    client = NextEnergyClient(
        session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], entry.entry_id
    )
    try:
        await client.login()
        contracts = await client.get_contracts()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except NextEnergyError as err:
        raise ConfigEntryNotReady from err

    contract = next(
        (c for c in contracts if c.contract_id == entry.data[CONF_CONTRACT_ID]),
        None,
    ) or contract_from_entry(entry.data["contract"])

    coordinator = NextEnergyCoordinator(hass, entry, client, contract)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NextEnergyConfigEntry) -> bool:
    """Stop the import; statistics already written stay in the recorder."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
