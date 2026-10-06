"""Safe operator entrypoint. Connection secrets are accepted only through environment."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from pricehunter.operations.errors import RecoveryError
from pricehunter.operations.recovery import Recovery, manifest_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("backup-create", "backup-verify", "restore-rehearsal"):
        command = commands.add_parser(name)
        command.add_argument("--backup", type=Path, required=True)
        command.add_argument("--source-env", default="DATABASE_URL")
        if name == "backup-create":
            command.add_argument("--revision")
        if name == "restore-rehearsal":
            command.add_argument("--target-env", default="RESTORE_DATABASE_URL")
            command.add_argument("--confirm-disposable", action="store_true")
    return result


async def run(arguments: argparse.Namespace) -> None:
    source = os.environ.get(arguments.source_env)
    if not source and arguments.command == "backup-verify":
        source = "postgresql+asyncpg://unused@localhost/unused"
    if not source:
        raise RecoveryError("source_environment_missing")
    recovery = Recovery(source)
    if arguments.command == "restore-rehearsal":
        target = os.environ.get(arguments.target_env)
        if not target:
            raise RecoveryError("target_environment_missing")
        report = await recovery.restore(
            arguments.backup, target, confirm_disposable=arguments.confirm_disposable
        )
        print(report.model_dump_json(indent=2))
        return
    manifest = (
        await recovery.create(arguments.backup, revision=arguments.revision)
        if arguments.command == "backup-create"
        else await recovery.verify(arguments.backup)
    )
    print(
        json.dumps(
            {
                "backup": str(arguments.backup),
                "manifest": str(manifest_path(arguments.backup)),
                "size_bytes": manifest.dump_size_bytes,
                "schema_head": manifest.expected_alembic_head,
                "tables": {name: value.rows for name, value in manifest.tables.items()},
                "checksum_verified": True,
            },
            indent=2,
        )
    )


def main() -> None:
    arguments = parser().parse_args()
    try:
        asyncio.run(run(arguments))
    except RecoveryError as exc:
        print(json.dumps({"error": exc.code}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": "recovery_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
