"""Which ``llm_spend`` rows count against the scoring caps (specs/017 "Spend cap").

Packet drafting has its own cap (``[apply] daily_cap_usd``) and logs under its own tiers:
``packet`` (API spend, charged) and ``packet-cli`` (subscription calls, ``cost_usd = 0``).
Neither may ever count against ``[scoring] daily_cap_usd`` / ``weekly_cap_usd``, or a packet
written mid-application could stop the nightly run (and a nightly run could block a packet).
Every scoring-cap read uses :data:`SCORING_ONLY` so the rule lives in one place.
"""

from __future__ import annotations

PACKET_TIER = "packet"
PACKET_CLI_TIER = "packet-cli"
PACKET_TIERS = (PACKET_TIER, PACKET_CLI_TIER)

# A WHERE fragment over ``llm_spend``: every row that is scoring spend.
SCORING_ONLY = f"tier NOT IN ('{PACKET_TIER}', '{PACKET_CLI_TIER}')"
