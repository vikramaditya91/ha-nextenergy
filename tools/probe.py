"""Exercise the NextEnergy client outside Home Assistant and dump raw payloads.

Run on a machine that may talk to mijn.nextenergy.nl, with credentials in the
environment or in an env file:

    NE_USERNAME=... NE_PASSWORD=... uv run --with aiohttp tools/probe.py [YYYY-MM-DD]

or

    uv run --with aiohttp tools/probe.py --env ~/.config/nextenergy.env 2026-10-20

The output is JSON: the contract, the filter options and the usage payload for
the requested day (default: yesterday). Nothing is written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

import aiohttp  # noqa: E402
from nextenergy_nl.api import NextEnergyClient  # noqa: E402


def _load_env(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _default(o: object) -> object:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date,)):
        return o.isoformat()
    return str(o)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", nargs="?", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--env", help="file with NE_USERNAME= and NE_PASSWORD= lines")
    args = parser.parse_args()
    _load_env(args.env)
    username, password = os.environ.get("NE_USERNAME"), os.environ.get("NE_PASSWORD")
    if not username or not password:
        print("NE_USERNAME and NE_PASSWORD are required", file=sys.stderr)
        return 2
    day = date.fromisoformat(args.day)

    async with aiohttp.ClientSession() as session:
        client = NextEnergyClient(session, username, password)
        await client.login()
        print("login ok", file=sys.stderr)
        contracts = await client.get_contracts()
        contract = contracts[0]
        out = {
            "contract": contract.__dict__,
            "filters": await client.get_usage_filters(contract.contract_id, day),
            "usage_raw": await client.get_usage_raw(contract.contract_id, day),
        }
        parsed = await client.get_usage(contract.contract_id, day)
        out["usage_parsed"] = {
            "day": parsed.day,
            "status": parsed.status,
            "is_smart_meter": parsed.is_smart_meter,
            "totals": parsed.totals.__dict__,
            "electricity_points": len(parsed.electricity),
            "return_points": len(parsed.electricity_return),
            "gas_points": len(parsed.gas),
            "electricity_sum": sum(parsed.electricity.values(), Decimal(0)),
            "gas_sum": sum(parsed.gas.values(), Decimal(0)),
        }
    json.dump(out, sys.stdout, indent=2, default=_default, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
