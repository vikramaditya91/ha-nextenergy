"""Constants for the NextEnergy integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "nextenergy_nl"

CONF_CONTRACT_ID = "contract_id"
CONF_CONTRACT_NUMBER = "contract_number"

# NextEnergy receives smart-meter intervals from the grid operator once a day,
# so polling more often than this only finds the same data again.
UPDATE_INTERVAL = timedelta(hours=6)
# How far back to import on the first run.
INITIAL_BACKFILL_DAYS = 30
# How many days back to re-check on every run: days can arrive late or be
# re-issued by the grid operator with corrected values.
RECHECK_DAYS = 7
