"""Local operator-reviewed retailer reconciliation; never a customer API."""

import argparse
from pathlib import Path
from uuid import UUID

from pricehunter.apps.feed_admin import emit, read_document
from pricehunter.core.container import Container
from pricehunter.domain.merchants import MerchantInput
from pricehunter.services.merchants import merchant_view

MUTATIONS = {
    "merchant-create",
    "merchant-metadata",
    "merchant-disable",
    "merchant-enable",
    "merchant-source-link",
    "merchant-source-unlink",
}
COMMANDS = MUTATIONS | {
    "merchant-list",
    "merchant-show",
    "merchant-history",
    "merchant-link-preview",
    "merchant-duplicate-candidates",
}


def add_commands(commands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    for name in sorted(COMMANDS):
        command = commands.add_parser(name)
        if name in MUTATIONS:
            command.add_argument("--expected-version", type=int, required=True)
            command.add_argument("--reason", required=True)
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--dry-run", action="store_true")
            mode.add_argument("--confirm", action="store_true")
        if name == "merchant-create":
            command.add_argument("file", type=Path)
        elif name in {"merchant-source-link", "merchant-source-unlink", "merchant-link-preview"}:
            command.add_argument("store_id", type=UUID)
            if name != "merchant-source-unlink":
                command.add_argument("merchant_id", type=UUID)
            if name in MUTATIONS:
                command.add_argument("--expected-source-merchant-id", type=UUID, required=True)
                if name == "merchant-source-link":
                    command.add_argument("--expected-source-version", type=int, required=True)
        elif name in {
            "merchant-show",
            "merchant-history",
            "merchant-metadata",
            "merchant-disable",
            "merchant-enable",
        }:
            command.add_argument("merchant_id", type=UUID)
        if name in {"merchant-list", "merchant-history", "merchant-duplicate-candidates"}:
            command.add_argument("--limit", type=int, default=100)
        if name == "merchant-metadata":
            command.add_argument("--display-name")
            domain = command.add_mutually_exclusive_group()
            domain.add_argument("--primary-domain")
            domain.add_argument("--clear-domain", action="store_true")


async def run(container: Container, args: argparse.Namespace) -> None:
    service = container.merchants
    name = args.command
    if name == "merchant-list":
        emit(await service.list_merchants(args.limit))
        return
    if name == "merchant-show":
        emit(await service.show(args.merchant_id))
        return
    if name == "merchant-history":
        emit(await service.history(args.merchant_id, args.limit))
        return
    if name == "merchant-duplicate-candidates":
        emit(await container.coverage.duplicate_merchants(args.limit))
        return
    if name == "merchant-link-preview":
        emit(await service.preview(args.store_id, args.merchant_id))
        return
    options = dict(
        expected_version=args.expected_version,
        reason=args.reason,
        dry_run=args.dry_run,
        confirm=args.confirm,
    )
    if name == "merchant-create":
        data = MerchantInput.model_validate_json(read_document(args.file))
        emit({"planned": data.model_dump(), "dry_run": args.dry_run})
        result = await service.create(data, **options)
    elif name in {"merchant-source-link", "merchant-source-unlink"}:
        if name == "merchant-source-link":
            emit(await service.preview(args.store_id, args.merchant_id))
            target, version = args.merchant_id, args.expected_source_version
        else:
            target, version = None, args.expected_version
            options["expected_version"] = 0
            emit({"store_id": args.store_id, "planned": "detach_to_new_distinct_merchant"})
        result = await service.reassign(
            args.store_id,
            target,
            expected_source_merchant_id=args.expected_source_merchant_id,
            expected_source_version=version,
            **options,
        )
    else:
        changes = {}
        if name in {"merchant-enable", "merchant-disable"}:
            changes["active"] = name == "merchant-enable"
        else:
            if args.display_name is not None:
                changes["display_name"] = args.display_name
            if args.primary_domain is not None or args.clear_domain:
                changes["primary_domain"] = None if args.clear_domain else args.primary_domain
        emit({"before": merchant_view(await service.get(args.merchant_id)), "proposed": changes})
        result = await service.change(args.merchant_id, changes=changes, **options)
    emit({"applied": not args.dry_run, "merchant": merchant_view(result)})
