import argparse
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from pricehunter.core.container import Container
from pricehunter.db.models import FeedSyncState, MerchantFeedItem, MerchantProgram
from pricehunter.domain.feeds import FeedError, MerchantProgramInput
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.services.policy_resolver import PolicyResolver

COMMANDS = {
    "merchant-programs",
    "merchant-program-import",
    "merchant-program-check",
    "feed-status",
    "feed-sync",
    "feed-diagnostics",
    "feed-search",
    "feed-list",
}


def add_commands(commands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    commands.add_parser("merchant-programs", help="List programs without secrets")
    onboarding = commands.add_parser(
        "merchant-program-import", help="Import a reviewed JSON program (no secrets)"
    )
    onboarding.add_argument("file", type=Path)
    feeds = commands.add_parser(
        "feed-list", help="List account-visible feeds; does not onboard them"
    )
    feeds.add_argument("network", choices=["awin", "tradedoubler"])
    for name in (
        "merchant-program-check",
        "feed-status",
        "feed-sync",
        "feed-diagnostics",
        "feed-search",
    ):
        command = commands.add_parser(name)
        command.add_argument("program", type=UUID)
        if name == "feed-sync":
            command.add_argument("--dry-run", action="store_true")
        if name == "feed-search":
            command.add_argument("query")


async def run(container: Container, args: argparse.Namespace) -> None:
    output: object
    if args.command == "merchant-program-import":
        if args.file.stat().st_size > 16384:
            raise ValueError("Merchant program file exceeds 16KB")
        program = await container.merchant_programs.save(
            MerchantProgramInput.model_validate_json(args.file.read_text())
        )
        output = {"id": str(program.id), "network": program.network, "active": program.active}
    elif args.command == "merchant-programs":
        async with container.sessions() as session:
            rows = await session.scalars(
                select(MerchantProgram)
                .order_by(MerchantProgram.network, MerchantProgram.external_merchant_id)
                .limit(1000)
            )
            output = [
                {
                    "id": str(p.id),
                    "network": p.network,
                    "merchant": p.display_name,
                    "market": p.market_country,
                    "active": p.active,
                    "approved": p.approved,
                }
                for p in rows
            ]
    elif args.command == "feed-list":
        source = container.feed_sources.get(args.network)
        if source is None:
            raise FeedError("network_disabled")
        output = [asdict(r) for r in await source.discover_feeds()]
    else:
        program = await container.merchant_programs.get(args.program)
        if args.command in {"feed-status", "feed-diagnostics"}:
            async with container.sessions() as session:
                state = await session.get(FeedSyncState, program.id)
                active = await session.scalar(
                    select(func.count())
                    .select_from(MerchantFeedItem)
                    .where(
                        MerchantFeedItem.merchant_program_id == program.id,
                        MerchantFeedItem.active.is_(True),
                    )
                )
                output = {
                    "program_id": str(program.id),
                    "active_rows": active,
                    **(
                        {
                            key: getattr(state, key)
                            for key in (
                                "status",
                                "generation",
                                "row_count",
                                "last_success_at",
                                "next_sync_at",
                                "failure_count",
                                "error_code",
                                "report",
                            )
                        }
                        if state
                        else {}
                    ),
                }
        elif args.command == "feed-search":
            results = await FeedStoreProvider(
                program.network, container.sessions, container.settings
            ).search(
                args.query,
                country=program.market_country,
                currency=program.currency,
                program_id=program.id,
            )
            output = [
                {
                    "external_id": r.external_id,
                    "title": r.title,
                    "price": str(r.price),
                    "currency": r.currency,
                    "variant": r.variant,
                }
                for r in results
                if r.merchant_program_id == program.id
            ]
        else:
            source = container.feed_sources.get(program.network)
            if source is None:
                raise FeedError("network_disabled")
            if args.command == "merchant-program-check":
                policy = PolicyResolver(container.settings).program(program)
                policy.require("catalog_persistence_allowed")
                output = {
                    "program_id": str(program.id),
                    "network": program.network,
                    "market": program.market_country,
                    "source_access": bool(await source.source_version(program)),
                    "policy": policy.model_dump(),
                }
            else:
                output = (
                    await container.feed_sync.run(program.id, source, dry_run=args.dry_run)
                ).model_dump(mode="json")
    print(json.dumps(output, default=str, ensure_ascii=False, indent=2))
