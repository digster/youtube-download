"""CLI entry point for local serving and the existing youtube-backup login flow."""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os

from dotenv import load_dotenv

from .config import Settings


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Local YouTube playlist downloader")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Open the local HTML application")
    serve.add_argument("--port", type=int, default=int(os.getenv("YTDL_PORT", "8765")))
    auth = commands.add_parser("auth", help="Use youtube-backup account authentication")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login")
    login.add_argument("--client-secrets", required=True)
    auth_commands.add_parser("status")
    args = parser.parse_args()
    if args.command == "auth":
        from youtube_backup.auth import get_auth_status, login as account_login

        if args.auth_command == "login":
            account_login(args.client_secrets)
            print("YouTube account connected. Start the app and sync your playlists.")
        else:
            status = get_auth_status()
            print("Token found" if status["authenticated"] else "Not connected")
        return
    if args.command != "serve":
        parser.print_help()
        return
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.data_dir / "app.log", maxBytes=2_000_000, backupCount=3
    )
    logging.basicConfig(
        level=os.getenv("YTDL_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), handler],
    )
    from .app import create_app
    import uvicorn

    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)
