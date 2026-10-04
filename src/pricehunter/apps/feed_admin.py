"""Local, explicit merchant lifecycle and read-only coverage diagnostics."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from pricehunter.core.container import Container
from pricehunter.db.models import MerchantProgram
from pricehunter.domain.feeds import (
    FEED_NETWORKS,
    FeedError,
    MerchantProgramCandidate,
    MerchantProgramInput,
)
from pricehunter.domain.provider_policy import ProviderDataPolicy
from pricehunter.providers.feeds.local import FeedStoreProvider
from pricehunter.services.feed_validation import validation_summary
from pricehunter.services.policy_resolver import PolicyResolver

MUTATIONS = {
    "merchant-program-review",
    "merchant-program-policy",
    "merchant-program-activate",
    "merchant-program-disable",
    "merchant-program-metadata",
    "merchant-program-feed-reference",
}
COMMANDS = MUTATIONS | {
    "merchant-programs",
    "merchant-program-import",
    "merchant-program-check",
    "merchant-program-history",
    "merchant-program-template",
    "merchant-candidates",
    "feed-status",
    "feed-sync",
    "feed-diagnostics",
    "feed-search",
    "feed-list",
    "coverage-report",
    "coverage-product",
    "duplicate-merchants",
    "merchant-program-validate",
    "merchant-program-validations",
    "merchant-program-validation-show",
}


def confirmation(command: argparse.ArgumentParser) -> None:
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--confirm", action="store_true", help="Apply the displayed change")


def add_commands(commands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    commands.add_parser(
        "merchant-programs", help="List programs and current versions without secrets"
    )
    onboarding = commands.add_parser(
        "merchant-program-import",
        help="Create pending JSON program; existing identities stay unchanged",
    )
    onboarding.add_argument("file", type=Path)
    confirmation(onboarding)
    for name in ("feed-list", "merchant-candidates", "merchant-program-template"):
        command = commands.add_parser(
            name, help="Read-only account visibility; never grants rights"
        )
        command.add_argument("network", choices=sorted(FEED_NETWORKS))
        if name == "merchant-program-template":
            command.add_argument("remote_id")
            command.add_argument("--feed-id", required=True)
    for name in sorted(MUTATIONS):
        command = commands.add_parser(name)
        command.add_argument("program", type=UUID)
        command.add_argument("--expected-version", type=int, required=True)
        command.add_argument("--reason", required=True)
        confirmation(command)
        if name in {"merchant-program-review", "merchant-program-policy"}:
            command.add_argument("--policy-file", type=Path, required=True)
        elif name == "merchant-program-metadata":
            command.add_argument("--display-name", required=True)
        elif name == "merchant-program-feed-reference":
            command.add_argument("--feed-id", required=True)
    for name in (
        "merchant-program-check",
        "merchant-program-history",
        "feed-status",
        "feed-sync",
        "feed-diagnostics",
        "feed-search",
        "merchant-program-validate",
        "merchant-program-validations",
    ):
        command = commands.add_parser(name)
        command.add_argument("program", type=UUID)
        if name == "feed-sync":
            confirmation(command)
            command.add_argument("--allow-shrink", action="store_true")
            command.add_argument("--reason", default="")
        elif name == "feed-search":
            command.add_argument("query")
    validation = commands.add_parser("merchant-program-validation-show")
    validation.add_argument("validation", type=UUID)
    commands.add_parser(
        "coverage-report", help="Read-only market and network coverage, no customer data"
    )
    product = commands.add_parser(
        "coverage-product", help="Bounded read-only candidate and catalog diagnostics"
    )
    product.add_argument("query")
    product.add_argument("--country", required=True)
    duplicates = commands.add_parser(
        "duplicate-merchants", help="Possible cross-network duplicates; never merges"
    )
    duplicates.add_argument("--limit", type=int, default=20)


def read_document(path: Path) -> str:
    if path.stat().st_size > 16384:
        raise ValueError("Operator document exceeds 16KB")
    return path.read_text()


def emit(output: object) -> None:
    print(json.dumps(output, default=str, ensure_ascii=False, indent=2))


def require_confirmation(args: argparse.Namespace) -> None:
    if not args.dry_run and not args.confirm:
        raise ValueError("Explicit --confirm is required; inspect with --dry-run first")


def summary(program: MerchantProgram) -> dict[str, object]:
    return {
        "id": str(program.id),
        "network": program.network,
        "merchant": program.display_name,
        "market": program.market_country,
        "active": program.active,
        "approved": program.approved,
        "version": program.version,
    }


async def change(container: Container, args: argparse.Namespace) -> None:
    program = await container.merchant_programs.get(args.program)
    emit(
        {
            "planned_action": args.command,
            "before": summary(program),
            "expected_version": args.expected_version,
            "dry_run": args.dry_run,
        }
    )
    require_confirmation(args)
    kwargs = {
        "expected_version": args.expected_version,
        "reason": args.reason,
        "dry_run": args.dry_run,
    }
    service = container.merchant_programs
    if args.command in {"merchant-program-review", "merchant-program-policy"}:
        policy = ProviderDataPolicy.model_validate_json(read_document(args.policy_file))
        emit(
            {
                "proposed_policy": policy.model_dump(),
                "review_includes_approval": args.command == "merchant-program-review",
            }
        )
        operation = (
            service.review if args.command == "merchant-program-review" else service.change_policy
        )
        program = await operation(program.id, policy, **kwargs)
    elif args.command == "merchant-program-activate":
        program = await service.activate(program.id, **kwargs)
    elif args.command == "merchant-program-disable":
        program = await service.disable(program.id, **kwargs)
    elif args.command == "merchant-program-metadata":
        emit({"proposed_display_name": args.display_name})
        program = await service.update_metadata(
            program.id, display_name=args.display_name, **kwargs
        )
    else:
        emit({"proposed_feed_id": args.feed_id, "clears_approval_and_cached_eligibility": True})
        program = await service.change_feed_reference(program.id, args.feed_id, **kwargs)
    emit({"applied": not args.dry_run, "program": summary(program)})


async def run(container: Container, args: argparse.Namespace) -> None:
    output: object
    if args.command in MUTATIONS:
        await change(container, args)
        return
    if args.command == "merchant-program-import":
        data = MerchantProgramInput.model_validate_json(read_document(args.file))
        if data.active or data.approved or data.policy.reviewed:
            raise ValueError("Import pending programs; review and activate explicitly")
        emit(
            {
                "planned_action": "create_pending_if_missing",
                "network": data.network,
                "merchant_id": data.external_merchant_id,
                "market": data.market_country,
                "active": False,
                "approved": False,
                "dry_run": args.dry_run,
            }
        )
        require_confirmation(args)
        output = (
            {"applied": False}
            if args.dry_run
            else summary(await container.merchant_programs.save(data))
        )
    elif args.command == "merchant-programs":
        async with container.sessions() as session:
            rows = await session.scalars(
                select(MerchantProgram)
                .order_by(MerchantProgram.network, MerchantProgram.external_merchant_id)
                .limit(1000)
            )
            output = [summary(p) for p in rows]
    elif args.command in {"feed-list", "merchant-candidates", "merchant-program-template"}:
        source = container.feed_sources.get(args.network) or container.create_feed_source(
            args.network
        )
        refs = await source.discover_feeds()
        if args.command == "feed-list":
            output = [asdict(r) for r in refs]
        else:
            candidates = [
                MerchantProgramCandidate(
                    network=args.network,
                    external_merchant_id=r.merchant_id,
                    external_feed_id=r.feed_id,
                    display_name=r.name,
                    market_country=r.market_country,
                    currency=r.currency,
                )
                for r in refs
            ]
            if args.command == "merchant-candidates":
                output = [c.model_dump() for c in candidates]
            else:
                matched = [
                    c
                    for c in candidates
                    if c.external_merchant_id == args.remote_id
                    and c.external_feed_id == args.feed_id
                ]
                if len(matched) != 1:
                    raise FeedError("feed_not_authorized")
                output = matched[0].template()
    elif args.command == "coverage-report":
        output = await container.coverage.report()
    elif args.command == "merchant-program-validation-show":
        output = await container.feed_validation.show(args.validation)
    elif args.command == "coverage-product":
        output = await container.coverage.product(args.query, args.country)
    elif args.command == "duplicate-merchants":
        output = await container.coverage.duplicate_merchants(args.limit)
    else:
        program = await container.merchant_programs.get(args.program)
        if args.command == "merchant-program-validate":
            source = container.feed_sources.get(program.network) or container.create_feed_source(
                program.network
            )
            evidence = await container.feed_validation.run(program.id, source)
            output = validation_summary(evidence, program)
            if evidence.status == "failed":
                emit(output)
                raise FeedError(evidence.error_code or "technical_validation_failed")
        elif args.command == "merchant-program-validations":
            output = await container.feed_validation.history(program.id)
        elif args.command == "merchant-program-history":
            output = [
                {
                    "action": a.action,
                    "previous_version": a.previous_version,
                    "new_version": a.new_version,
                    "changed_fields": a.changed_fields,
                    "reason": a.reason,
                    "created_at": a.created_at,
                }
                for a in await container.merchant_programs.history(program.id)
            ]
        elif args.command in {"feed-status", "feed-diagnostics"}:
            output = await container.coverage.program(program.id)
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
            enabled_source = container.feed_sources.get(program.network)
            if enabled_source is None:
                raise FeedError("network_disabled")
            if args.command == "merchant-program-check":
                policy = PolicyResolver(container.settings).program(program)
                policy.require("catalog_persistence_allowed")
                output = {
                    "program_id": str(program.id),
                    "network": program.network,
                    "market": program.market_country,
                    "source_access": bool(await enabled_source.source_version(program)),
                    "policy": policy.model_dump(),
                }
            else:
                emit(
                    {
                        "planned_action": "feed-sync",
                        "program_id": str(program.id),
                        "dry_run": args.dry_run,
                    }
                )
                require_confirmation(args)
                output = (
                    await container.feed_sync.run(
                        program.id,
                        enabled_source,
                        dry_run=args.dry_run,
                        allow_shrink=getattr(args, "allow_shrink", False),
                        reason=getattr(args, "reason", ""),
                        confirm=getattr(args, "confirm", False),
                    )
                ).model_dump(mode="json")
    emit(output)
