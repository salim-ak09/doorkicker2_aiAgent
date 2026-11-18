"""Read-only game bridge contract and Windows process detector."""

import csv
import io
import subprocess
import sys
from typing import Protocol, TypedDict

GAME_EXECUTABLE = "DoorKickers2.exe"
GAME_NAME = "Door Kickers 2"


class GameStatus(TypedDict):
    source: str
    game: str
    running: bool | None
    process_id: int | None
    mission_data_available: bool
    mission: dict[str, object] | None
    status: str
    detail: str


class GameBridge(Protocol):
    def get_status(self) -> GameStatus:
        """Return game-process status and any verified mission data."""
        ...


def _status(
    running: bool | None,
    process_id: int | None,
    status: str,
    detail: str,
) -> GameStatus:
    return {
        "source": "windows_process_detection",
        "game": GAME_NAME,
        "running": running,
        "process_id": process_id,
        "mission_data_available": False,
        "mission": None,
        "status": status,
        "detail": detail,
    }


class WindowsProcessGameBridge:
    """Detect the executable without reading or modifying game state."""

    def get_status(self) -> GameStatus:
        if sys.platform != "win32":
            return _status(
                None,
                None,
                "detection_unavailable",
                "Process detection is only supported on Windows.",
            )

        try:
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"IMAGENAME eq {GAME_EXECUTABLE}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return _status(
                None,
                None,
                "detection_unavailable",
                f"Could not query the Windows process list: {error}",
            )

        if result.returncode != 0:
            return _status(
                None,
                None,
                "detection_unavailable",
                "Windows process detection returned an error.",
            )

        process_ids: list[int] = []
        for row in csv.reader(io.StringIO(result.stdout)):
            if not row or row[0].strip().casefold() != GAME_EXECUTABLE.casefold():
                continue
            try:
                process_id = int(row[1].strip())
            except (IndexError, ValueError):
                return _status(
                    None,
                    None,
                    "detection_unavailable",
                    "Windows returned a matching process without a valid process ID.",
                )
            if process_id <= 0:
                return _status(
                    None,
                    None,
                    "detection_unavailable",
                    "Windows returned an invalid process ID.",
                )
            process_ids.append(process_id)

        if not process_ids:
            return _status(
                False,
                None,
                "game_not_running",
                f"{GAME_EXECUTABLE} was not found in the Windows process list.",
            )
        return _status(
            True,
            process_ids[0],
            "process_detected_only",
            f"{GAME_EXECUTABLE} is running. Mission data is not available.",
        )
