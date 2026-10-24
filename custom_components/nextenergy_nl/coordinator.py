"""Fetch daily usage from NextEnergy and feed Home Assistant's long-term statistics."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import AuthenticationError, Contract, DayUsage, NextEnergyClient, NextEnergyError
from .const import DOMAIN, INITIAL_BACKFILL_DAYS, RECHECK_DAYS, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type NextEnergyConfigEntry = ConfigEntry[NextEnergyCoordinator]


class NextEnergyData:
    """What the coordinator hands to entities."""

    def __init__(self, contract: Contract, latest: DayUsage | None) -> None:
        self.contract = contract
        self.latest = latest


class NextEnergyCoordinator(DataUpdateCoordinator[NextEnergyData]):
    """Poll a few times a day; NextEnergy only has new data once a day anyway."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NextEnergyConfigEntry,
        client: NextEnergyClient,
        contract: Contract,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {contract.contract_number}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.contract = contract

    # ----- statistics ids -------------------------------------------------

    def _stat_id(self, kind: str) -> str:
        return f"{DOMAIN}:{self.contract.contract_id}_{kind}"

    def _metadata(self) -> dict[str, StatisticMetaData]:
        c = self.contract
        name = f"NextEnergy {c.contract_number}".strip()
        meta: dict[str, StatisticMetaData] = {}
        if c.has_electricity:
            meta["electricity"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("electricity"),
                name=f"{name} electricity",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            meta["electricity_cost"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("electricity_cost"),
                name=f"{name} electricity cost",
                unit_of_measurement="EUR",
            )
        if c.has_return:
            meta["return"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("return"),
                name=f"{name} electricity return",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            meta["return_compensation"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("return_compensation"),
                name=f"{name} return compensation",
                unit_of_measurement="EUR",
            )
        if c.has_gas:
            meta["gas"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("gas"),
                name=f"{name} gas",
                unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            )
            meta["gas_cost"] = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("gas_cost"),
                name=f"{name} gas cost",
                unit_of_measurement="EUR",
            )
        return meta

    # ----- update ---------------------------------------------------------

    async def _async_update_data(self) -> NextEnergyData:
        try:
            latest = await self._import_new_days()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NextEnergyError as err:
            raise UpdateFailed(str(err)) from err
        return NextEnergyData(self.contract, latest)

    async def _import_new_days(self) -> DayUsage | None:
        """Import every day since the last imported one, re-checking the last few."""
        meta = self._metadata()
        if not meta:
            return None
        anchor_id = next(iter(meta.values()))["statistic_id"]
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, anchor_id, True, {"sum"}
        )
        yesterday = dt_util.now().date() - timedelta(days=1)
        if last.get(anchor_id):
            last_start = dt_util.utc_from_timestamp(last[anchor_id][0]["start"])
            first_day = dt_util.as_local(last_start).date() - timedelta(days=RECHECK_DAYS - 1)
        else:
            first_day = yesterday - timedelta(days=INITIAL_BACKFILL_DAYS - 1)
        if self.contract.start and first_day < self.contract.start:
            first_day = self.contract.start

        latest: DayUsage | None = None
        day = first_day
        while day <= yesterday:
            usage = await self.client.get_usage(self.contract.contract_id, day)
            if usage.complete or usage.totals.electricity_kwh or usage.totals.gas_m3:
                await self._write_day(meta, usage)
                latest = usage
            else:
                _LOGGER.debug("%s has no data yet (status %r)", day, usage.status)
            day += timedelta(days=1)
        return latest

    async def _write_day(self, meta: dict[str, StatisticMetaData], usage: DayUsage) -> None:
        """Write one day's hourly statistics, continuing each running sum.

        Interval values are grouped per hour. When NextEnergy only gives a day
        total (non-smart meter, or data points missing), the total is booked on
        the day's first hour so the Energy dashboard still shows the right daily
        amount.
        """
        tz = dt_util.get_default_time_zone()
        start_of_day = datetime.combine(usage.day, datetime.min.time(), tzinfo=tz)
        series: dict[str, tuple[dict, Decimal]] = {
            "electricity": (usage.electricity, usage.totals.electricity_kwh),
            "electricity_cost": (
                _scale(
                    usage.electricity, usage.totals.electricity_kwh, usage.totals.electricity_cost
                ),
                usage.totals.electricity_cost,
            ),
            "return": (usage.electricity_return, usage.totals.return_kwh),
            "return_compensation": (
                _scale(usage.electricity_return, usage.totals.return_kwh, usage.totals.return_cost),
                usage.totals.return_cost,
            ),
            "gas": (usage.gas, usage.totals.gas_m3),
            "gas_cost": (
                _scale(usage.gas, usage.totals.gas_m3, usage.totals.gas_cost),
                usage.totals.gas_cost,
            ),
        }
        for kind, metadata in meta.items():
            points, total = series[kind]
            hourly = _per_hour(points, total)
            if not hourly:
                continue
            base = await self._sum_before(metadata["statistic_id"], start_of_day)
            stats: list[StatisticData] = []
            running = base
            for hour in range(24):
                amount = hourly.get(hour)
                if amount is None:
                    continue
                running += amount
                stats.append(
                    StatisticData(
                        start=start_of_day + timedelta(hours=hour),
                        state=float(amount),
                        sum=float(running),
                    )
                )
            async_add_external_statistics(self.hass, metadata, stats)

    async def _sum_before(self, statistic_id: str, start: datetime) -> Decimal:
        """Running sum just before ``start``, so re-imported days do not double count."""

        def _query() -> Decimal:
            rows = statistics_during_period(
                self.hass,
                start - timedelta(days=400),
                start,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            ).get(statistic_id)
            if not rows:
                return Decimal(0)
            return Decimal(str(rows[-1]["sum"] or 0))

        return await get_instance(self.hass).async_add_executor_job(_query)


def _per_hour(points: dict, total: Decimal) -> dict[int, Decimal]:
    """Collapse interval values into hours; fall back to the day total on hour 0."""
    if points:
        hourly: dict[int, Decimal] = {}
        for when, value in points.items():
            hourly[when.hour] = hourly.get(when.hour, Decimal(0)) + value
        return hourly
    if total:
        return {0: total}
    return {}


def _scale(points: dict, volume: Decimal, cost: Decimal) -> dict:
    """Spread a day's cost over its intervals in proportion to volume."""
    if not points or not volume:
        return {}
    factor = cost / volume
    return {when: value * factor for when, value in points.items()}


def contract_from_entry(data: dict[str, Any]) -> Contract:
    """Rebuild the Contract stored in the config entry."""
    return Contract(**{**data, "start": _d(data.get("start")), "end": _d(data.get("end"))})


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
