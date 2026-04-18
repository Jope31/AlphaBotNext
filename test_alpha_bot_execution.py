import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
import sys

# Mock modules that are missing
sys.modules["requests"] = MagicMock()
sys.modules["numpy"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# pylint: disable=wrong-import-position
import alpha_bot_execution


class TestGetCurrentET(unittest.TestCase):
    @patch("alpha_bot_execution.datetime")
    def test_get_current_et_happy_path(self, mock_datetime):
        """Test get_current_et when zoneinfo is available."""
        # Mocking ZoneInfo as well
        mock_zone_info = MagicMock()
        mock_now_et = datetime(2023, 6, 1, 8, 0, 0)

        # We need to handle the internal import and usage of ZoneInfo
        with patch("zoneinfo.ZoneInfo", return_value=mock_zone_info):
            # First call to datetime.now(timezone.utc)
            # Second call to datetime.now(ZoneInfo("America/New_York"))
            mock_datetime.now.side_effect = [
                datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
                mock_now_et,
            ]

            result = alpha_bot_execution.get_current_et()

            self.assertEqual(result, mock_now_et)
            # Verify it was called with ZoneInfo
            mock_datetime.now.assert_any_call(mock_zone_info)

    @patch("alpha_bot_execution.datetime")
    def test_get_current_et_fallback_edt(self, mock_datetime):
        """Test get_current_et fallback logic for EDT (June)."""
        mock_utc_now = datetime(2023, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = mock_utc_now

        # Simulate ZoneInfo failing
        with patch("zoneinfo.ZoneInfo", side_effect=Exception("No tzdata")):
            result = alpha_bot_execution.get_current_et()

            # Expected: 12:00 UTC - 4 hours = 8:00
            expected_with_tz = mock_utc_now - timedelta(hours=4)

            self.assertEqual(result, expected_with_tz)

    @patch("alpha_bot_execution.datetime")
    def test_get_current_et_fallback_est(self, mock_datetime):
        """Test get_current_et fallback logic for EST (January)."""
        mock_utc_now = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = mock_utc_now

        # Simulate ZoneInfo failing
        with patch("zoneinfo.ZoneInfo", side_effect=Exception("No tzdata")):
            result = alpha_bot_execution.get_current_et()

            # Expected: 12:00 UTC - 5 hours = 7:00
            expected_with_tz = mock_utc_now - timedelta(hours=5)

            self.assertEqual(result, expected_with_tz)


if __name__ == "__main__":
    unittest.main()
