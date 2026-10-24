# NextEnergy for Home Assistant

Brings your [NextEnergy](https://www.nextenergy.nl) smart-meter usage into Home Assistant,
including the Energy dashboard.

NextEnergy has no public API and no web portal; everything lives in their app. This
integration talks to the same backend the app uses and imports the quarter-hour
electricity and gas readings NextEnergy receives from your grid operator, together with
the costs NextEnergy calculated for them at your tariff.

> Data arrives about one day late. That is how the Dutch smart-meter chain works: the
> grid operator collects yesterday's intervals overnight and hands them to the supplier.
> For live readings you need a P1 dongle on the meter; this integration is for history,
> cost tracking and the Energy dashboard, not for real-time automations.

## What you get

**Long-term statistics**, hourly, going back 30 days on first setup and then kept up to date:

| Statistic | Unit |
|---|---|
| Electricity consumption | kWh |
| Electricity cost | EUR |
| Electricity returned to the grid (if your contract has it) | kWh |
| Return compensation (if your contract has it) | EUR |
| Gas consumption (if your contract has it) | m³ |
| Gas cost (if your contract has it) | EUR |

Pick them in **Settings → Dashboards → Energy** as grid consumption, return and gas sources.
Costs use the "use an entity tracking the total costs" option.

**Sensors** with the figures of the most recent day NextEnergy has reported:
last reported day, electricity, electricity returned, gas, and total cost for that day.

## Installation

### HACS

1. HACS → Integrations → ⋮ → **Custom repositories**.
2. Add `https://github.com/vikramaditya91/ha-nextenergy` as an *Integration*.
3. Install **NextEnergy** and restart Home Assistant.

### Manual

Copy `custom_components/nextenergy_nl` into your `config/custom_components/` folder and
restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → NextEnergy**, then sign in with the
e-mail address and password you use in the NextEnergy app. The integration finds your
contract and starts importing. The first import covers the last 30 days and takes a minute.

If you change your password in the app, Home Assistant asks you to re-authenticate.

## How it works

The NextEnergy app is an OutSystems application. Its backend at `mijn.nextenergy.nl`
exposes the screens' data actions as JSON endpoints. This integration logs in the way the
app does, syncs the contract, and calls the usage data action for each day. It writes the
result as external statistics, the same mechanism Home Assistant's built-in Opower
integration uses for day-old utility data.

Polling happens every six hours. The last three days are always re-checked because the grid
operator sometimes delivers a day late or corrects it afterwards. Re-imports replace the
day's hourly values and recompute the running sums, so nothing is double counted.

Because the API is undocumented, NextEnergy can change it without notice. When that happens
the integration logs an error and stops importing until it is updated; statistics already
written stay. Please open an issue with the log line.

## Development

`tools/probe.py` exercises the client without Home Assistant and dumps the raw payloads:

```sh
NE_USERNAME=you@example.com NE_PASSWORD=... uv run --with aiohttp tools/probe.py 2026-10-20
```

Lint with `ruff check .`.

## Not affiliated

This is a community project and is not affiliated with or endorsed by NextEnergy B.V.
