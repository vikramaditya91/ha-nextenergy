"""Client for the NextEnergy app backend.

NextEnergy has no public API. The mobile app is an OutSystems application whose
backend is reachable at https://mijn.nextenergy.nl. This module speaks the same
"screenservices" JSON protocol the app uses. It has no Home Assistant imports so
it can be exercised from a plain Python script (see tools/probe.py).

Every request carries ``versionInfo`` with the module version (fetched at login)
and an ``apiVersion`` that is specific to each server action. Those hashes are
copied from the app bundles and change whenever NextEnergy redeploys; when a
request starts failing with an "incompatible version" error, refresh them from
``scripts/<Flow>.<Screen>.mvc.js`` under the base URL.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://mijn.nextenergy.nl/Mobile_EnergyNext"
LOGIN_PAGE = f"{BASE_URL}/Login"
MODULE_VERSION_URL = f"{BASE_URL}/moduleservices/moduleversioninfo"
SCREENSERVICES = f"{BASE_URL}/screenservices"

# Server action / data action endpoints with the apiVersion the app sends for each.
ACTION_LOGIN = (
    f"{SCREENSERVICES}/Mobile_EnergyNext/EntryFlow/Login_New/ActionDoLogin",
    "6RXNTWQg6FlgienWK8L57A",
    "EntryFlow.Login_New",
)
ACTION_CONTRACT_SYNC = (
    f"{SCREENSERVICES}/Mobile_LocalStorage_BL/ActionGetContractAndAccountDataToSync",
    "qUSD+Sc+GuaGYdDHGUX1aQ",
    "MainFlow.Usage",
)
ACTION_USAGE_FILTERS = (
    f"{SCREENSERVICES}/Mobile_EnergyNext_CW/WidgetFlow/Usage_QuarterlyPrices_v2"
    "/DataActionGetFilterLabels",
    "f3e+A7njgzN7aOxxZWr+cA",
    "MainFlow.Usage",
)
ACTION_USAGE_DATA = (
    f"{SCREENSERVICES}/Mobile_EnergyNext_CW/WidgetFlow/Usage_QuarterlyPrices_v2"
    "/DataActionGetUsageData",
    "fRPUGOfMLbxgQk2uUb+xeg",
    "MainFlow.Usage",
)

SESSION_COOKIE = "nr2Users_Customers"
_CSRF_RE = re.compile(r"crf=([^;]+)")
_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


class NextEnergyError(Exception):
    """Base error."""


class AuthenticationError(NextEnergyError):
    """Username or password rejected."""


class ApiVersionError(NextEnergyError):
    """The backend no longer accepts the action versions this client sends."""


@dataclass(frozen=True)
class Contract:
    """An energy contract on the account."""

    contract_id: int
    contract_number: str
    status: str
    address: str
    start: date | None
    end: date | None
    has_electricity: bool
    has_gas: bool
    has_return: bool


@dataclass(frozen=True)
class Totals:
    """Volumes and costs for one day, as reported by NextEnergy."""

    electricity_kwh: Decimal
    electricity_cost: Decimal
    return_kwh: Decimal
    return_cost: Decimal
    gas_m3: Decimal
    gas_cost: Decimal
    electricity_total_eur: Decimal
    gas_total_eur: Decimal


@dataclass(frozen=True)
class DayUsage:
    """Usage for one calendar day."""

    day: date
    status: str
    is_smart_meter: bool
    totals: Totals
    # Interval values keyed by local start time, each a kWh or m³ amount.
    electricity: dict[dtime, Decimal] = field(default_factory=dict)
    electricity_return: dict[dtime, Decimal] = field(default_factory=dict)
    gas: dict[dtime, Decimal] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def complete(self) -> bool:
        """True when NextEnergy marks the day as final rather than missing/estimated."""
        return self.status.lower().startswith("complete")


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def _date(value: Any) -> date | None:
    if not value or str(value).startswith("1900-01-01"):
        return None
    return date.fromisoformat(str(value)[:10])


class NextEnergyClient:
    """Async client. One instance per account; call ``login`` before anything else."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        device_id: str | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        # The app sends its install id; any stable string works. A fixed value
        # per config entry keeps NextEnergy from seeing a new "device" each run.
        self._device_id = device_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, username))
        self._module_version: str | None = None
        self._csrf: str | None = None
        self._lock = asyncio.Lock()

    # ----- plumbing -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "OutSystems-locale": "nl-NL",
        }
        if self._csrf:
            headers["X-CSRFToken"] = self._csrf
        return headers

    async def _post(self, action: tuple[str, str, str], body: dict[str, Any]) -> dict[str, Any]:
        url, api_version, view_name = action
        payload = {
            "versionInfo": {"moduleVersion": self._module_version, "apiVersion": api_version},
            "viewName": view_name,
            **body,
        }
        async with self._session.post(url, json=payload, headers=self._headers()) as resp:
            if resp.status != 200:
                raise NextEnergyError(f"{url} returned HTTP {resp.status}")
            data = await resp.json(content_type=None)
        exc = data.get("exception") or {}
        if exc:
            message = exc.get("message", "unknown error")
            if "version" in message.lower():
                raise ApiVersionError(message)
            if "login" in message.lower() or exc.get("name") == "NotRegistered":
                raise AuthenticationError(message)
            raise NextEnergyError(message)
        return data

    async def _refresh_module_version(self) -> None:
        async with self._session.get(LOGIN_PAGE) as resp:
            if resp.status != 200:
                raise NextEnergyError(f"login page returned HTTP {resp.status}")
        params = {str(int(time.time() * 1000)): ""}
        async with self._session.get(MODULE_VERSION_URL, params=params) as resp:
            data = await resp.json(content_type=None)
        self._module_version = data.get("versionToken")
        if not self._module_version:
            raise NextEnergyError("module version token missing")

    def _read_csrf(self) -> None:
        for cookie in self._session.cookie_jar:
            if cookie.key == SESSION_COOKIE:
                match = _CSRF_RE.search(unquote(cookie.value))
                if match:
                    self._csrf = match.group(1)
                    return
        raise AuthenticationError("login did not set a session cookie")

    # ----- public API -----------------------------------------------------

    async def login(self) -> None:
        """Authenticate. Raises AuthenticationError on bad credentials."""
        async with self._lock:
            await self._refresh_module_version()
            await self._post(
                ACTION_LOGIN,
                {
                    "inputParameters": {
                        "Username": self._username,
                        "Password": self._password,
                        "DeviceId": self._device_id,
                    }
                },
            )
            self._read_csrf()

    async def _call(self, action: tuple[str, str, str], body: dict[str, Any]) -> dict[str, Any]:
        """POST with one transparent re-login when the session has expired."""
        if self._csrf is None:
            await self.login()
        try:
            return await self._post(action, body)
        except AuthenticationError:
            self._csrf = None
            await self.login()
            return await self._post(action, body)

    async def get_contracts(self) -> list[Contract]:
        """Return the contracts on this account.

        The app syncs one contract at a time, so this returns a single-item list
        for most accounts; it is a list to leave room for multi-contract accounts.
        """
        data = await self._call(ACTION_CONTRACT_SYNC, {"inputParameters": {}})
        if not data.get("data", {}).get("IsSuccess", True):
            raise NextEnergyError("contract sync reported failure")
        c = data["data"]["SyncContractData"]
        lines = data["data"].get("SyncContractlineIntervals", {})
        props = data["data"].get("SyncContractProperties", {})
        return [
            Contract(
                contract_id=int(c["ContractId"]),
                contract_number=str(c.get("ContractNumber", "")),
                status=str(c.get("ContractStatusLabel", "")),
                address=" ".join(
                    s
                    for s in (
                        c.get("DeliveryAddress"),
                        c.get("DeliveryPostalCode"),
                        c.get("DeliveryCity"),
                    )
                    if s
                ),
                start=_date(c.get("StartDate")),
                end=_date(c.get("EndDate")),
                has_electricity=bool(lines.get("HasElectricity", True)),
                has_gas=bool(lines.get("HasGas", False)),
                has_return=bool(props.get("IsWithReturn", False)),
            )
        ]

    def _usage_body(self, contract_id: int, day: date) -> dict[str, Any]:
        return {
            "screenData": {
                "variables": {
                    "In_ContractId": contract_id,
                    "_in_ContractIdInDataFetchStatus": 1,
                    "In_DateTime": f"{day.isoformat()}T00:00:00Z",
                    "_in_DateTimeInDataFetchStatus": 1,
                }
            },
            "clientVariables": {
                "UsageDate": day.isoformat(),
                "UsageYear": day.year,
            },
        }

    async def get_usage_filters(self, contract_id: int, day: date) -> dict[str, Any]:
        """Return the raw filter options the app offers (utility types, granularity)."""
        data = await self._call(ACTION_USAGE_FILTERS, self._usage_body(contract_id, day))
        return data.get("data", {})

    async def get_usage_raw(self, contract_id: int, day: date) -> dict[str, Any]:
        """Return the raw usage payload for one day."""
        data = await self._call(ACTION_USAGE_DATA, self._usage_body(contract_id, day))
        return data.get("data", {})

    async def get_usage(self, contract_id: int, day: date) -> DayUsage:
        """Return parsed usage for one day."""
        return parse_day(day, await self.get_usage_raw(contract_id, day))


# ----- parsing ------------------------------------------------------------


def _series_bucket(name: str) -> str | None:
    n = name.lower()
    if "gas" in n:
        return "gas"
    if "terug" in n or "return" in n or "productie" in n or "opwek" in n:
        return "return"
    if any(k in n for k in ("verbruik", "stroom", "elek", "elec", "levering", "usage")):
        return "electricity"
    return None


def _label_time(label: str) -> dtime | None:
    match = _TIME_RE.search(label)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24:
        return None
    return dtime(hour, minute)


def parse_day(day: date, data: dict[str, Any]) -> DayUsage:
    """Turn the GetUsageData payload into a DayUsage.

    Interval values come from ``DataPoints`` (Label/Value/SeriesName records,
    which is what the app's chart plots). Day totals come from
    ``TotalsPerPeriod`` and are authoritative; intervals are only used to spread
    the day over the hours in Home Assistant's statistics.
    """
    totals_raw = data.get("TotalsPerPeriod") or {}
    elec = totals_raw.get("Elec") or {}
    gas = totals_raw.get("Gas") or {}
    totals = Totals(
        electricity_kwh=_dec(elec.get("DeliveryVolume")),
        electricity_cost=_dec(elec.get("DeliveryCosts")),
        return_kwh=_dec(elec.get("ReturnVolume")),
        return_cost=_dec(elec.get("ReturnCosts")),
        gas_m3=_dec(gas.get("DeliveryVolume")),
        gas_cost=_dec(gas.get("DeliveryCosts")),
        electricity_total_eur=_dec(totals_raw.get("TotalElecEuro")),
        gas_total_eur=_dec(totals_raw.get("TotalGasEuro")),
    )
    buckets: dict[str, dict[dtime, Decimal]] = {"electricity": {}, "return": {}, "gas": {}}
    for point in data.get("DataPoints") or []:
        when = _label_time(str(point.get("Label", "")))
        bucket = _series_bucket(str(point.get("SeriesName", "")))
        if when is None or bucket is None:
            continue
        buckets[bucket][when] = buckets[bucket].get(when, Decimal(0)) + _dec(point.get("Value"))
    return DayUsage(
        day=day,
        status=str(data.get("DayStatusId") or ""),
        is_smart_meter=bool(data.get("Out_IsSmartMeter", False)),
        totals=totals,
        electricity=buckets["electricity"],
        electricity_return=buckets["return"],
        gas=buckets["gas"],
        raw=data,
    )


def local_midnight(day: date) -> datetime:
    """Naive midnight for ``day``; callers attach the Home Assistant time zone."""
    return datetime.combine(day, dtime.min)
