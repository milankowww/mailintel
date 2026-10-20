# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from ..config.backup import export_config, import_config
from ..auth import get_or_create_api_token
from ..db import connect_database, init_database
from ..logging_config import configure_logging
from ..repositories import Repository
from ..secrets import PasswordEncryptor
from ..settings import get_settings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailintel")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the local PostgreSQL database")

    auth = subparsers.add_parser("auth", help="Manage local API authentication")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_subparsers.add_parser("token", help="Print the generated local API token")

    config = subparsers.add_parser("config", help="Export or import encrypted local configuration")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    export_parser = config_subparsers.add_parser("export", help="Export encrypted configuration backup")
    export_parser.add_argument("path", help="Backup file to write")
    export_parser.add_argument("--passphrase", default=None, help="Backup encryption passphrase")
    import_parser = config_subparsers.add_parser("import", help="Import encrypted configuration backup")
    import_parser.add_argument("path", help="Backup file to read")
    import_parser.add_argument("--passphrase", default=None, help="Backup encryption passphrase")

    serve = subparsers.add_parser("serve", help="Run the FastAPI server")
    serve.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    serve.add_argument("--host", default=None, help="Bind host. Defaults to MAILINTEL_HOST or 127.0.0.1")
    serve.add_argument("--port", type=int, default=None, help="Bind port. Defaults to MAILINTEL_PORT or 8000")
    serve.add_argument("--paused", action="store_true", help="Start with AI operations paused")

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "auth":
        if args.auth_command == "token":
            print(get_or_create_api_token(settings))
            return 0

    connection = connect_database(settings.database_url)
    init_database(connection)

    if args.command == "init-db":
        connection.close()
        print("Initialized database")
        return 0

    if args.command == "config":
        passphrase = args.passphrase or os.environ.get("MAILINTEL_CONFIG_PASSPHRASE")
        if not passphrase:
            passphrase = getpass.getpass("Config backup passphrase: ")
        repository = Repository(connection)
        encryptor = PasswordEncryptor(settings.encryption_key_file)
        if args.config_command == "export":
            result = export_config(repository, encryptor, Path(args.path), passphrase)
            connection.close()
            print(f"Exported config to {args.path}: {result}")
            return 0
        if args.config_command == "import":
            result = import_config(repository, encryptor, Path(args.path), passphrase)
            connection.close()
            print(f"Imported config from {args.path}: {result}")
            return 0

    if args.command == "serve":
        connection.close()
        if args.paused:
            os.environ["MAILINTEL_LLM_PAUSED"] = "1"
        try:
            import uvicorn
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
            raise RuntimeError("uvicorn must be installed to run the API server") from exc
        uvicorn.run(
            "mailintel.app:create_app",
            factory=True,
            reload=args.reload,
            host=args.host or settings.host,
            port=args.port or settings.port,
        )
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess tests
    raise SystemExit(main())
