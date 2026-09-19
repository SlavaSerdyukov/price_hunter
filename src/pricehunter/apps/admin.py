"""Local operator commands. Never expose this interface over HTTP."""

import argparse
import asyncio
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update

from pricehunter.core.config import get_settings
from pricehunter.core.container import Container
from pricehunter.core.security import new_api_key, token_digest
from pricehunter.db.base import utcnow
from pricehunter.db.models import APIKey, NotificationEvent, User


async def run(args: argparse.Namespace) -> None:
    container = Container(get_settings())
    try:
        if args.command in ("create-api-user", "issue-api-key"):
            key = new_api_key()
            async with container.sessions.begin() as session:
                if args.command == "create-api-user":
                    user = User(first_name=args.label)
                    session.add(user)
                    await session.flush()
                else:
                    existing_user = await session.scalar(
                        select(User).where(
                            User.telegram_user_id == args.telegram_user_id,
                        )
                    )
                    if existing_user is None:
                        raise SystemExit("User must first start the bot")
                    user = existing_user
                row = APIKey(user_id=user.id, digest=token_digest(key), label=args.label)
                session.add(row)
                await session.flush()
                print(f"user_id={user.id}\nkey_id={row.id}")
            # Intentional local one-time disclosure. Never emitted via application logging.
            print(f"api_key={key}")
        elif args.command == "revoke-api-key":
            async with container.sessions.begin() as session:
                await session.execute(
                    update(APIKey)
                    .where(APIKey.id == args.key_id)
                    .values(
                        revoked_at=utcnow(),
                    )
                )
            print("Key revoked")
        elif args.command == "outbox":
            async with container.sessions() as session:
                rows = await session.scalars(
                    select(NotificationEvent)
                    .where(
                        NotificationEvent.status.in_(["failed", "uncertain"]),
                    )
                    .order_by(NotificationEvent.created_at.desc())
                    .limit(100)
                )
                for event in rows:
                    print(f"{event.id} {event.status} {event.event_type} attempts={event.attempts}")
        elif args.command == "prune-history":
            days = container.settings.history_retention_days
            if days <= 0:
                raise SystemExit("Set HISTORY_RETENTION_DAYS > 0 to enable explicit pruning")
            count = await container.price_checks.prune_history(
                before=utcnow() - timedelta(days=days)
            )
            print(f"Removed {count} old observations; rerun for the next bounded batch")
    finally:
        await container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-api-user", help="Create an API-only account and key")
    create.add_argument("--label", default="local")
    issue = commands.add_parser("issue-api-key", help="Issue a key for an existing Telegram user")
    issue.add_argument("--telegram-user-id", type=int, required=True)
    issue.add_argument("--label", default="local")
    revoke = commands.add_parser("revoke-api-key")
    revoke.add_argument("key_id", type=UUID)
    commands.add_parser("outbox", help="List failed/uncertain deliveries for reconciliation")
    commands.add_parser("prune-history", help="Delete one bounded batch under configured retention")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
