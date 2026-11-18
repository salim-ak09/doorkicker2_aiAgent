import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.game_bridge import GAME_EXECUTABLE, WindowsProcessGameBridge


class GameBridgeTests(unittest.TestCase):
    def test_running_game_reports_pid_without_mission_data(self) -> None:
        output = f'"{GAME_EXECUTABLE}","1234","Console","1","1,234 K"\r\n'
        with (
            patch("backend.game_bridge.sys.platform", "win32"),
            patch(
                "backend.game_bridge.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tasklist"], returncode=0, stdout=output
                ),
            ) as run,
        ):
            status = WindowsProcessGameBridge().get_status()

        self.assertEqual(status["source"], "windows_process_detection")
        self.assertEqual(status["game"], "Door Kickers 2")
        self.assertEqual(status["running"], True)
        self.assertEqual(status["process_id"], 1234)
        self.assertEqual(status["status"], "process_detected_only")
        self.assertFalse(status["mission_data_available"])
        self.assertIsNone(status["mission"])
        run.assert_called_once()

    def test_game_closure_is_reported_as_not_running(self) -> None:
        stopped = subprocess.CompletedProcess(
            args=["tasklist"],
            returncode=0,
            stdout="INFO: No tasks are running which match the specified criteria.\r\n",
        )
        with (
            patch("backend.game_bridge.sys.platform", "win32"),
            patch("backend.game_bridge.subprocess.run", return_value=stopped),
        ):
            status = WindowsProcessGameBridge().get_status()

        self.assertEqual(status["status"], "game_not_running")
        self.assertIs(status["running"], False)
        self.assertIsNone(status["process_id"])
        self.assertIsNone(status["mission"])

    def test_process_query_failures_report_unknown(self) -> None:
        with (
            patch("backend.game_bridge.sys.platform", "win32"),
            patch(
                "backend.game_bridge.subprocess.run",
                side_effect=subprocess.TimeoutExpired("tasklist", timeout=5),
            ),
        ):
            status = WindowsProcessGameBridge().get_status()

        self.assertEqual(status["status"], "detection_unavailable")
        self.assertIsNone(status["running"])
        self.assertFalse(status["mission_data_available"])

    def test_non_windows_platform_reports_unknown(self) -> None:
        with patch("backend.game_bridge.sys.platform", "linux"):
            status = WindowsProcessGameBridge().get_status()

        self.assertEqual(status["status"], "detection_unavailable")
        self.assertIsNone(status["running"])
        self.assertIsNone(status["process_id"])

    def test_mock_fixture_is_explicitly_not_live_mission_data(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "mock_game_status.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertIn("MOCK DATA", fixture["notice"])
        self.assertEqual(fixture["source"], "development_mock_fixture")
        self.assertEqual(fixture["status"], "mock_only")
        self.assertFalse(fixture["mission_data_available"])
        self.assertIsNone(fixture["mission"])


if __name__ == "__main__":
    unittest.main()
