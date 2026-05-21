"""Command-line entry points for the identity subsystem.

Used by `make identity-bootstrap`.
"""

import asyncio
import sys

from app.gateway.identity.bootstrap import bootstrap
from app.gateway.identity.db import create_engine_and_sessionmaker
from app.gateway.identity.settings import get_identity_settings


async def _run_bootstrap() -> None:
    settings = get_identity_settings()
    engine, maker = create_engine_and_sessionmaker(settings.database_url)
    try:
        async with maker() as session:
            await bootstrap(session, bootstrap_admin_email=settings.bootstrap_admin_email)
    finally:
        await engine.dispose()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "bootstrap":
        print("usage: python -m app.gateway.identity.cli bootstrap", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run_bootstrap())


if __name__ == "__main__":
    main()
