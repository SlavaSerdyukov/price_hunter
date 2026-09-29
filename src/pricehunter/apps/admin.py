"""Local operator commands. Never expose this interface over HTTP."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update

from pricehunter.apps import feed_admin
from pricehunter.core.config import get_settings
from pricehunter.core.container import Container
from pricehunter.core.security import new_api_key, token_digest
from pricehunter.db.base import utcnow
from pricehunter.db.models import APIKey, NotificationEvent, User
from pricehunter.domain.feeds import FeedError
from pricehunter.services.catalog_diagnostics import CatalogDiagnostics


async def run(args: argparse.Namespace) -> None:
    container = Container(get_settings())
    try:
        if args.command in feed_admin.COMMANDS:
            try:
                await feed_admin.run(container, args)
            except FeedError as exc:
                raise SystemExit(f"Feed operation failed: {exc.code}") from None
            return
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
        elif args.command == "subscription":
            print((await container.subscriptions.status(args.user_id)).model_dump_json(indent=2))
        elif args.command == "refund-stars":
            applied = await container.billing.refund_stars(args.telegram_user_id, args.charge_id)
            print("Refund recorded" if applied else "Refund already recorded")
        elif args.command == "cancel-stars":
            await container.billing.cancel_renewal(args.user_id, args.subscription_id)
            print("Future renewal cancelled; paid access retained")
        elif args.command == "reconcile-stars":
            report = await container.reconciliation.scan(
                offset=args.offset, max_pages=args.max_pages, apply_refunds=args.apply_refunds
            )
            print(json.dumps(asdict(report), indent=2))
        elif args.command == "stars-balance":
            print((await container.payment_provider.balance()).model_dump_json())
        elif args.command == "billing-retry":
            print(f"Processed {await container.billing_intake.retry_pending()} pending updates")
        elif args.command == "product-diagnostics":
            product_report = await CatalogDiagnostics(
                container.sessions, container.settings
            ).product(args.product_id, args.compare_with)
            print(json.dumps(product_report, default=str, ensure_ascii=False, indent=2))
        elif args.command == "duplicate-candidates":
            duplicate_report = await CatalogDiagnostics(
                container.sessions, container.settings
            ).duplicates(args.limit)
            print(json.dumps(duplicate_report, default=str, ensure_ascii=False, indent=2))
        elif args.command == "catalog-backfill":
            async with container.sessions.begin() as session:
                count = await container.products.resolver.backfill(session)
            print(f"Indexed {count} legacy products; rerun until zero")
        elif args.command == "prune-history":
            days = container.settings.history_retention_days
            if days <= 0:
                raise SystemExit("Set HISTORY_RETENTION_DAYS > 0 to enable explicit pruning")
            count = await container.price_checks.prune_history(
                before=utcnow() - timedelta(days=days)
            )
            best_count = await container.best_prices.prune()
            print(
                f"Removed {count} old observations and {best_count} best-price transitions; "
                "rerun for the next bounded batch"
            )
    finally:
        await container.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    feed_admin.add_commands(commands)
    create = commands.add_parser("create-api-user", help="Create an API-only account and key")
    create.add_argument("--label", default="local")
    issue = commands.add_parser("issue-api-key", help="Issue a key for an existing Telegram user")
    issue.add_argument("--telegram-user-id", type=int, required=True)
    issue.add_argument("--label", default="local")
    revoke = commands.add_parser("revoke-api-key")
    revoke.add_argument("key_id", type=UUID)
    commands.add_parser("outbox", help="List failed/uncertain deliveries for reconciliation")
    commands.add_parser("prune-history", help="Delete one bounded batch under configured retention")
    subscription = commands.add_parser(
        "subscription", help="Show effective subscription for an internal user UUID"
    )
    subscription.add_argument("user_id", type=UUID)
    refund = commands.add_parser(
        "refund-stars", help="Refund an explicit charge and stop future renewal"
    )
    refund.add_argument("--telegram-user-id", type=int, required=True)
    refund.add_argument("--charge-id", required=True)
    cancel = commands.add_parser("cancel-stars", help="Cancel renewal while preserving paid access")
    cancel.add_argument("--user-id", type=UUID, required=True)
    cancel.add_argument("--subscription-id", type=UUID, required=True)
    reconcile = commands.add_parser(
        "reconcile-stars", help="Dry-run ledger reconciliation by default"
    )
    reconcile.add_argument("--offset", type=int, default=0)
    reconcile.add_argument("--max-pages", type=int, default=10)
    reconcile.add_argument(
        "--apply-refunds",
        action="store_true",
        help="Append only fully matched remote refunds; never grant purchases",
    )
    commands.add_parser("stars-balance", help="Show bot Stars balance (operator only)")
    commands.add_parser("billing-retry", help="Retry durable pending billing updates")
    commands.add_parser(
        "catalog-backfill", help="Index one bounded batch of legacy canonical products"
    )
    diagnostics = commands.add_parser(
        "product-diagnostics", help="Read-only canonical identity and freshness report"
    )
    diagnostics.add_argument("product_id", type=UUID)
    diagnostics.add_argument("--compare-with", type=UUID)
    duplicates = commands.add_parser(
        "duplicate-candidates", help="Read-only duplicate candidates; never merge automatically"
    )
    duplicates.add_argument("--limit", type=int, default=20)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
