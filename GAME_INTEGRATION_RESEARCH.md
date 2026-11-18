# Door Kickers 2 Integration Research

**Reviewed:** 2026-11-18  
**Game:** Door Kickers 2: Task Force North  
**Steam app ID:** 1239080

## Findings

### Officially documented support

- KillHouse Games describes a full mission and level editor and Steam Workshop support for custom missions, units, and broader mod content on its [game page](https://inthekillhouse.com/doorkickers2/).
- KillHouse's [1.0 release announcement](https://inthekillhouse.com/door-kickers-2-task-force-north-release-announcement/) dates full release to **2025-02-10** and describes Workshop-based mod support and editor improvements.
- The [Steam store listing](https://store.steampowered.com/app/1239080/Door_Kickers_2_Task_Force_North/) identifies the game and lists both Steam Workshop and a level editor.
- The [Steam Workshop page](https://steamcommunity.com/app/1239080/workshop/) exposes community content categories such as missions, campaigns, units, and equipment.

These are supported content-authoring and distribution mechanisms. The official materials reviewed do not document an external SDK, IPC protocol, telemetry API, mission-state export API, or command interface for controlling gameplay.

### Version and compatibility

The official announcement verifies the 1.0 release date, but the reviewed official materials do not establish the current patch number or state compatibility requirements for an external integration. The version of any locally installed game was not inspected. Therefore, no particular installed build is certified as compatible.

### Unofficial code-loading project

The public [Door Kickers modloader repository](https://github.com/gabrweu/doorkickers-modloader) describes an unofficial mechanism for loading third-party code into the game. Its README says it is not affiliated with KillHouse Games and warns that loaded DLLs can run arbitrary code and may make a computer vulnerable. This is not an official integration interface, and its safety or compatibility was not independently verified.

The modloader was **not downloaded, installed, or tested**. No DLL was installed and no game files were changed.

## Integration decision

**Do not implement live game-state reads or gameplay commands at this stage.** An official, documented programmatic interface was not found in the sources reviewed. Community code-loading mechanisms do not meet the project's verification and safety requirements.

The application separately performs a read-only Windows process-list check for `DoorKickers2.exe` and can report whether that process is running, its PID, and when it stops. This check does not inspect process memory or game files and does not provide mission information. A modular `GameBridge` contract is available for a future supported reader; the live bridge always returns `mission_data_available: false` and `mission: null`. The dashboard continues to label mission information as operator-entered; it does not claim a game-state connection or actual in-game control.

`backend/fixtures/mock_game_status.json` is explicitly labeled mock data for development and tests only. It is not loaded by the live application and contains no purported mission telemetry.

Revisit mission-state reading only if KillHouse publishes a supported interface or a documented export format can be validated without modifying game files or relying on unverified DLLs. Until then, mission details must remain operator-entered.

## Sources

- [KillHouse Games — Door Kickers 2](https://inthekillhouse.com/doorkickers2/)
- [KillHouse Games — Full Release Announcement](https://inthekillhouse.com/door-kickers-2-task-force-north-release-announcement/)
- [Steam — Door Kickers 2 store listing](https://store.steampowered.com/app/1239080/Door_Kickers_2_Task_Force_North/)
- [Steam — Door Kickers 2 Workshop](https://steamcommunity.com/app/1239080/workshop/)
- [GitHub — Unofficial modloader repository](https://github.com/gabrweu/doorkickers-modloader)
