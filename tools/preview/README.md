# MixOS offline UI preview

Double-click `index.html`, or open it from your browser. Everything is embedded in this one file; no internet, dependencies or server are required.

- Start with **Offline / unknown**, then select **Connected / MOCK data** to try simulated telemetry and a terminal session.
- Use the bottom navigation for Home, Power, Terminal, Device and Settings.
- Settings has English/Chinese, four persisted themes, mock brightness/backlight and explicit local maintenance confirmation.
- In the mock terminal, try `help`, `status`, `中文` and `history`. The grid is 80×28; scrollback is capped at 100 lines. Shift+PageUp/Down scrolls locally.
- Device has a single-pointer touch test, including pointer-release/cancel handling.
- Maintenance and update requests require local confirmation. Escape rejects; Enter confirms. No command is actually executed, and terminal output cannot confirm anything.
- Capacity/runtime stay unavailable because calibration is not verified. Named preview sensors are illustrative, not a statement of real hardware presence.

Run the dependency-free interaction checks with `node tests/test_preview.cjs` from the MixOS root. See `docs/ui-implementation.md` for firmware integration, memory use, test details and known limitations.
