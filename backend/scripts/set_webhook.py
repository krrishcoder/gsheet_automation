"""Register, inspect or remove the Telegram webhook. Run once per deployment, not on every start.

    python scripts/set_webhook.py set              # uses PUBLIC_BASE_URL + WEBHOOK_PATH
    python scripts/set_webhook.py set --drop-pending
    python scripts/set_webhook.py info
    python scripts/set_webhook.py delete
    python scripts/set_webhook.py generate-secret  # prints a value for TELEGRAM_WEBHOOK_SECRET
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.security import generate_webhook_secret  # noqa: E402
from app.services.telegram_service import TelegramApiError, TelegramBotApi  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["set", "info", "delete", "generate-secret"])
    parser.add_argument("--url", help="Full webhook URL (default: PUBLIC_BASE_URL + WEBHOOK_PATH)")
    parser.add_argument("--drop-pending", action="store_true", help="Discard updates queued at Telegram")
    parser.add_argument("--max-connections", type=int, default=10)
    args = parser.parse_args(argv)

    if args.action == "generate-secret":
        print(generate_webhook_secret())
        return 0

    settings = Settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    api = TelegramBotApi(settings.telegram_bot_token, base_url=settings.telegram_api_base_url)
    try:
        if args.action == "set":
            if not settings.telegram_webhook_secret:
                print("TELEGRAM_WEBHOOK_SECRET is not set (use: generate-secret)", file=sys.stderr)
                return 2
            url = args.url or settings.public_base_url.rstrip("/") + settings.webhook_path
            if not url.startswith("https://"):
                print(f"Telegram requires an HTTPS webhook URL, got: {url!r}", file=sys.stderr)
                return 2
            api.set_webhook(
                url,
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=args.drop_pending,
                max_connections=args.max_connections,
            )
            print(f"Webhook set to {url}")
            info = api.get_webhook_info()
        elif args.action == "delete":
            api.delete_webhook(drop_pending_updates=args.drop_pending)
            print("Webhook deleted")
            return 0
        else:
            info = api.get_webhook_info()
        print(json.dumps(info, indent=2))
        if info.get("last_error_message"):
            print(f"\nLast delivery error: {info['last_error_message']}", file=sys.stderr)
        return 0
    except TelegramApiError as exc:
        print(f"Telegram API error: {exc}", file=sys.stderr)
        return 1
    finally:
        api.close()


if __name__ == "__main__":
    raise SystemExit(main())
