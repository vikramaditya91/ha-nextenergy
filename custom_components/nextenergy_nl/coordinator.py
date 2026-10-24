"""Fetch daily usage from NextEnergy and feed Home Assistant's long-term statistics."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
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

from .api import (
    NEXTENERGY_TZ,
    AuthenticationError,
    Contract,
    DayUsage,
    NextEnergyClient,
    NextEnergyError,
)
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
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("electricity"),
                name=f"{name} electricity",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                unit_class="energy",
            )
            meta["electricity_cost"] = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("electricity_cost"),
                name=f"{name} electricity cost",
                unit_of_measurement="EUR",
                unit_class=None,
            )
        if c.has_return:
            meta["return"] = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("return"),
                name=f"{name} electricity return",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                unit_class="energy",
            )
            meta["return_compensation"] = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("return_compensation"),
                name=f"{name} return compensation",
                unit_of_measurement="EUR",
                unit_class=None,
            )
        if c.has_gas:
            meta["gas"] = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("gas"),
                name=f"{name} gas",
                unit_of_measurement=UnitOfVolume.CUBIC_METERS,
                unit_class="volume",
            )
            meta["gas_cost"] = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                source=DOMAIN,
                statistic_id=self._stat_id("gas_cost"),
                name=f"{name} gas cost",
                unit_of_measurement="EUR",
                unit_class=None,
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
        """Import every day since the last imported one, re-checking the last few.

        Days are NextEnergy's days, i.e. Dutch local time, regardless of the
        Home Assistant time zone. Running sums are carried in memory across the
        days of one import: the recorder commits writes asynchronously, so
        reading the sum back between consecutive days could see a stale value.
        """
        meta = self._metadata()
        if not meta:
            return None
        anchor_id = next(iter(meta.values()))["statistic_id"]
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, anchor_id, True, {"sum"}
        )
        yesterday = datetime.now(NEXTENERGY_TZ).date() - timedelta(days=1)
        if last.get(anchor_id):
            last_start = dt_util.utc_from_timestamp(last[anchor_id][0]["start"])
            first_day = last_start.astimezone(NEXTENERGY_TZ).date() - timedelta(
                days=RECHECK_DAYS - 1
            )
        else:
            first_day = yesterday - timedelta(days=INITIAL_BACKFILL_DAYS - 1)
        if self.contract.start and first_day < self.contract.start:
            first_day = self.contract.start
        if first_day > yesterday:
            return None

        running = await self._sums_before(meta, _midnight(first_day))
        latest: DayUsage | None = None
        day = first_day
        while day <= yesterday:
            usage = await self.client.get_usage(self.contract.contract_id, day)
            if usage.has_data:
                self._write_day(meta, usage, running)
                latest = usage
            else:
                _LOGGER.debug("%s has no data yet (status %r)", day, usage.status)
            day += timedelta(days=1)
        return latest

    def _write_day(
        self,
        meta: dict[str, StatisticMetaData],
        usage: DayUsage,
        running: dict[str, Decimal],
    ) -> None:
        """Write one day's hourly statistics, advancing each running sum in place.

        Every hour of the day is written, with 0 for hours NextEnergy left out,
        so a re-import fully replaces what was stored before. A series with no
        data at all for the day (for example gas on a day where only electricity
        arrived) is left untouched rather than zeroed.

        Cost series carry NextEnergy's all-in day total (energy, tax and fixed
        costs), spread over the hours in proportion to the volume. When only a
        day total is known, it is booked on the first hour.
        """
        series: dict[str, tuple[dict, Decimal]] = {
            "electricity": (usage.electricity, usage.totals.electricity_kwh),
            "electricity_cost": (
                _scale(
                    usage.electricity,
                    usage.totals.electricity_kwh,
                    usage.totals.electricity_total_eur,
                ),
                usage.totals.electricity_total_eur,
            ),
            "return": (usage.electricity_return, usage.totals.return_kwh),
            "return_compensation": (
                _scale(usage.electricity_return, usage.totals.return_kwh, usage.totals.return_cost),
                usage.totals.return_cost,
            ),
            "gas": (usage.gas, usage.totals.gas_m3),
            "gas_cost": (
                _scale(usage.gas, usage.totals.gas_m3, usage.totals.gas_total_eur),
                usage.totals.gas_total_eur,
            ),
        }
        for kind, metadata in meta.items():
            points, total = series[kind]
            hourly = _per_hour(usage.day, points, total)
            if not hourly:
                continue
            stats: list[StatisticData] = []
            for start, amount in sorted(hourly.items()):
                running[kind] += amount
                stats.append(
                    StatisticData(start=start, state=float(amount), sum=float(running[kind]))
                )
            async_add_external_statistics(self.hass, metadata, stats)

    async def _sums_before(
        self, meta: dict[str, StatisticMetaData], start: datetime
    ) -> dict[str, Decimal]:
        """Each series' running sum just before ``start``.

        Re-imports only reach back a few days, so the row before ``start`` is
        recent when it exists. When nothing precedes ``start`` the series either
        starts here (sum 0) or its last row is older than the search window, in
        which case the last row overall is the right base.
        """
        ids = {m["statistic_id"] for m in meta.values()}

        def _query() -> dict[str, Decimal]:
            before = statistics_during_period(
                self.hass, start - timedelta(days=60), start, ids, "hour", None, {"sum"}
            )
            sums: dict[str, Decimal] = {}
            for kind, metadata in meta.items():
                sid = metadata["statistic_id"]
                rows = before.get(sid)
                if not rows:
                    last = get_last_statistics(self.hass, 1, sid, True, {"sum"}).get(sid)
                    if last and dt_util.utc_from_timestamp(last[0]["start"]) < start:
                        rows = last
                sums[kind] = Decimal(str(rows[-1]["sum"] or 0)) if rows else Decimal(0)
            return sums

        return await get_instance(self.hass).async_add_executor_job(_query)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=NEXTENERGY_TZ)


def _per_hour(day: date, points: dict, total: Decimal) -> dict[datetime, Decimal]:
    """Map a day's values onto the real hours of that day.

    Hours are keyed by the aware start instant, built from NextEnergy's local
    labels. On the autumn clock change the repeated 02:00 label lands on one
    instant and the values add up; on the spring change the skipped hour is
    folded into the next one. Missing hours are written as 0 so a re-import
    overwrites what was there. Without any hourly points the day total goes on
    the first hour.
    """
    if not points and not total:
        return {}
    hourly: dict[datetime, Decimal] = {}
    for hour in range(24):
        start = datetime.combine(day, time(hour), tzinfo=NEXTENERGY_TZ)
        key = start.astimezone(UTC)
        hourly.setdefault(key, Decimal(0))
    if not points:
        hourly[min(hourly)] = total
        return hourly
    for when, value in points.items():
        key = datetime.combine(day, when, tzinfo=NEXTENERGY_TZ).astimezone(UTC)
        hourly[key] = hourly.get(key, Decimal(0)) + value
    return hourly


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
