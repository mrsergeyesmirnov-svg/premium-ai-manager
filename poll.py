"""Single-replica Telegram Business worker. No inbound ports or domain needed."""
import asyncio
import logging
import os
from pathlib import Path

from app import allowed_chat_ids, process_update, telegram_api
from florist import ai_enabled

log = logging.getLogger("poll")


async def receive_batch(offset: int, state_file: Path) -> int:
    updates = await telegram_api("getUpdates", {
        "offset": offset, "timeout": 25, "limit": 10,
        "allowed_updates": ["business_connection", "business_message"],
    }, timeout=40)
    for update in updates:
        await process_update(update)
        # Confirm only after successful processing; keep the cursor across restarts.
        next_offset = update["update_id"] + 1
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(str(next_offset))
        temporary.replace(state_file)
        offset = next_offset
    return offset


async def main():
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    if bool(os.getenv("YANDEX_API_KEY")) != bool(os.getenv("YANDEX_MODEL_URI")):
        raise RuntimeError("Set both YANDEX_API_KEY and YANDEX_MODEL_URI, or neither")
    allowed_chat_ids()
    state_file = Path(os.getenv("POLL_STATE_FILE", "/data/telegram-offset"))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    offset = int(state_file.read_text()) if state_file.exists() else 0
    await telegram_api("getMe", {})
    # Retain pending messages during migration from Railway.
    await telegram_api("deleteWebhook", {"drop_pending_updates": False})
    log.warning("Polling started. Dialogue mode: %s", "yandex (credentials not yet tested)" if ai_enabled() else "scripted")
    while True:
        try:
            offset = await receive_batch(offset, state_file)
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            # Never log requests, tokens, message bodies or HTTP exception details.
            log.warning("Telegram processing failed; retrying in 5 seconds. Check credentials, network and other running bot instances.")
            if state_file.exists():
                offset = int(state_file.read_text())
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.error("Startup failed. Check environment variables, Telegram access and writable /data volume.")
        raise SystemExit(1) from None
