"""Run on the VDS: python3 setup_env.py. Secrets never appear in shell history."""
import getpass
import os
from pathlib import Path
import re
import sys
import warnings


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Run this script in an interactive terminal.")
    warnings.simplefilter("error", getpass.GetPassWarning)
    target = Path("/etc/premium-ai-manager.env")
    if target.exists() and input("Replace existing bot settings? Type yes: ").strip() != "yes":
        return
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Invalid Telegram token format. Nothing saved.")
    api_key = getpass.getpass("Yandex API key (hidden; Enter = scripted demo): ").strip()
    model = ""
    if api_key:
        model = input("Yandex model URI (gpt://...): ").strip()
        if not re.fullmatch(r"gpt://[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", model):
            raise SystemExit("Invalid model URI. Nothing saved.")
    values = {"TELEGRAM_BOT_TOKEN": token, "ALLOWED_CHAT_IDS": "*",
              "YANDEX_API_KEY": api_key, "YANDEX_MODEL_URI": model}
    if any(any(c.isspace() or ord(c) < 32 for c in value) for value in values.values()):
        raise SystemExit("Unexpected whitespace in a setting. Nothing saved.")
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write("".join(f"{key}={value}\n" for key, value in values.items()))
    temporary.replace(target)
    print("Settings saved. Secrets are hidden; file permissions are 600.")


if __name__ == "__main__":
    main()
