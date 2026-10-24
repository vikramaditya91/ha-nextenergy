"""Sensors with the most recent complete day NextEnergy has reported.

The hourly history lives in long-term statistics (see coordinator.py) and shows
up in the Energy dashboard. These sensors exist so the last day's figures can be
used in automations and on dashboards without querying statistics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Contract, DayUsage
from .const import DOMAIN
from .coordinator import NextEnergyConfigEntry, NextEnergyCoordinator


@dataclass(frozen=True, kw_only=True)
class NextEnergySensorDescription(SensorEntityDescription):
    """Sensor description with a value extractor and a contract gate."""

    value_fn: Callable[[DayUsage], Decimal | date | str]
    available_fn: Callable[[Contract], bool] = lambda _: True


SENSORS: tuple[NextEnergySensorDescription, ...] = (
    NextEnergySensorDescription(
        key="last_day",
        translation_key="last_day",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda u: u.day,
    ),
    NextEnergySensorDescription(
        key="electricity_last_day",
        translation_key="electricity_last_day",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda u: u.totals.electricity_kwh,
        available_fn=lambda c: c.has_electricity,
    ),
    NextEnergySensorDescription(
        key="return_last_day",
        translation_key="return_last_day",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda u: u.totals.return_kwh,
        available_fn=lambda c: c.has_return,
    ),
    NextEnergySensorDescription(
        key="gas_last_day",
        translation_key="gas_last_day",
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=lambda u: u.totals.gas_m3,
        available_fn=lambda c: c.has_gas,
    ),
    NextEnergySensorDescription(
        key="cost_last_day",
        translation_key="cost_last_day",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda u: u.totals.electricity_total_eur + u.totals.gas_total_eur,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NextEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        NextEnergySensor(coordinator, description)
        for description in SENSORS
        if description.available_fn(coordinator.contract)
    )


class NextEnergySensor(CoordinatorEntity[NextEnergyCoordinator], SensorEntity):
    """One figure from the latest complete day."""

    entity_description: NextEnergySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: NextEnergyCoordinator, description: NextEnergySensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        contract = coordinator.contract
        self._attr_unique_id = f"{contract.contract_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(contract.contract_id))},
            name=f"NextEnergy {contract.contract_number}".strip(),
            manufacturer="NextEnergy",
            model="Energy contract",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://www.nextenergy.nl",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.latest is not None

    @property
    def native_value(self) -> Decimal | date | str | None:
        latest = self.coordinator.data.latest
        if latest is None:
            return None
        return self.entity_description.value_fn(latest)
