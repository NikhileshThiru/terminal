"""One-time: re-grade outcomes written by the old trade-close method.

    docker compose exec backend python -m app.portfolio.regrade

See the OutcomeResolver docstring for why. Idempotent: rows already graded
by the window method are left alone; previous values are kept in `notes`.
"""

from __future__ import annotations

import asyncio
import json

from app.core.logging import configure_logging
from app.portfolio.resolver import get_outcome_resolver


async def main() -> dict[str, int]:
    configure_logging()
    counts = await get_outcome_resolver().regrade_legacy_outcomes()
    print(json.dumps(counts))
    return counts


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
