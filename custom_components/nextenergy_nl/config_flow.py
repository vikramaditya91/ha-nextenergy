"""Config flow: NextEnergy app credentials, verified by logging in."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import AuthenticationError, NextEnergyClient, NextEnergyError
from .const import CONF_CONTRACT_ID, CONF_CONTRACT_NUMBER, DOMAIN

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class NextEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup and re-authentication."""

    VERSION = 1

    async def _validate(self, username: str, password: str) -> dict[str, Any]:
        session = async_create_clientsession(self.hass)
        client = NextEnergyClient(session, username, password)
        await client.login()
        contract = (await client.get_contracts())[0]
        data = asdict(contract)
        data["start"] = contract.start.isoformat() if contract.start else None
        data["end"] = contract.end.isoformat() if contract.end else None
        return {
            CONF_USERNAME: username,
            CONF_PASSWORD: password,
            CONF_CONTRACT_ID: contract.contract_id,
            CONF_CONTRACT_NUMBER: contract.contract_number,
            "contract": data,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await self._validate(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except NextEnergyError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(data[CONF_CONTRACT_ID]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"NextEnergy {data[CONF_CONTRACT_NUMBER]}".strip(), data=data
                )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                data = await self._validate(entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except NextEnergyError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )
