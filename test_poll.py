import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from poll import receive_batch


class PollTest(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_only_successful_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "offset"
            with patch("poll.telegram_api", new_callable=AsyncMock, return_value=[{"update_id": 4}, {"update_id": 5}]) as api, patch("poll.process_update", new_callable=AsyncMock, side_effect=[{"ok": True}, RuntimeError("failed")]):
                with self.assertRaises(RuntimeError):
                    await receive_batch(0, state)
                self.assertEqual(state.read_text(), "5")
                self.assertEqual(api.call_args.kwargs["timeout"], 40)
            with patch("poll.telegram_api", new_callable=AsyncMock, return_value=[{"update_id": 5}]), patch("poll.process_update", new_callable=AsyncMock, return_value={"ok": True}):
                self.assertEqual(await receive_batch(5, state), 6)
                self.assertEqual(state.read_text(), "6")


if __name__ == "__main__":
    unittest.main()
