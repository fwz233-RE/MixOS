# MixOS UI implementation and integration

## Scope and design

This workflow adds `firmware/esp32s3/main/mix_ui.[ch]` and a standalone preview. It does not compile, import, or call legacy `ui.c` symbols. It does not edit the parent-owned main loop, device service, USB link, terminal parser, font driver, or CMake files. Original repositories are untouched.

The new layout uses 32px screen gutters, 16–24px internal spacing, rounded cards, restrained headings and a graphite/mint default palette. The four palettes are Graphite mint, Paper forest, Midnight blue and Warm ember. English and Chinese preferences are stored under the `mixui` NVS namespace (`theme`, `lang`). Preview preferences use a separate browser-local `mixui-preview` key.

| Page | Implemented content |
| --- | --- |
| Home | Valid/unknown battery charge, USB/battery/unknown source, voltage and load power, brightness, terminal entry, Linux CPU/temperature/memory/uptime when connected. |
| Power | Battery and USB voltage/current/power; existing RAM-backed one-hour voltage/current/charge history; capacity and runtime explicitly unavailable unless calibration and relevant values are valid. |
| Terminal | 80 columns × 28 rows, 12×24px cells, two-column CJK, 100-line parser history, older/newer/live touch controls, explicit session open/close; output remains in the parent-owned terminal while offscreen. |
| Device | Shared sensor-present/checked bit masks, headphone status, normalized microphone levels, keyboard connection/resync count, free PSRAM, single-contact local touch test and confirmed input reset. |
| Settings | Four themes, Chinese/English, brightness actions, keyboard backlight action, MixOS/protocol versions, explicit unreported Linux/keyboard versions, maintenance/cancel/update confirmation. |

Sensor mask names are not specified by `mix_view.h`. Firmware intentionally displays indices 00–15 rather than inventing a mapping. The preview's named devices are explicitly illustrative. Microphone levels are expected to be normalized 0–1; brightness is displayed as percent. The parent must keep these model units consistent.

## Parent integration contract

- Call `mix_ui_init(panel)` on the application main task after NVS, the parent-owned panel framebuffer and font initialization. The return type is `esp_err_t`; handle allocation/init failure before sending UI input.
- UI init only allocates its framebuffer and opens preferences. It does not initialize NVS flash, erase storage, probe sensors, switch a hardware multiplexer, write I2C, initialize fonts or start sampling.
- Pass the current shared `mix_view_t` and unsigned monotonic milliseconds to `mix_ui_tick` from the same main task. Touch, local keyboard input, terminal feed/read operations and queued-action draining also belong to this task.
- **Determine `mix_ui_terminal_visible()` before handling each local key.** A modal makes it false. Deliver modal keys to `mix_ui_key`, and never forward the same confirmation/rejection key to the remote PTY. If the parent calls `mix_ui_key` first and then queries visibility, Enter can close the modal and accidentally become remote input. Sample visibility before dispatch, or otherwise explicitly consume the modal key.
- `mix_ui_key` only accepts local keyboard input. USB/terminal bytes go exclusively to `mix_terminal_feed`. `mix_ui_notice` sanitizes ASCII and cannot queue or approve actions.
- Drain `mix_ui_take_action` after input/tick. UI queues bounded `mix_action_t` requests; the parent remains responsible for connectivity checks, command allowlists, update verification, hardware writes and action completion. Queue overflow is reported rather than overwriting pending actions.
- Changing pages does not enqueue terminal close. Opening/closing a session is explicit. The Home shortcut preserves the previous page. On nonterminal pages, local digit keys 1–5 also navigate. Ordinary terminal keys remain the parent's transport responsibility.
- `update_pending` is a trusted parent state flag: its rising state opens an independent confirmation dialog. Returning it to false dismisses an obsolete dialog. A still-pending rejected request is not repeatedly reopened, but Settings can review it again. Output containing escape codes, `YES`, Enter, or confirmation-like text has no UI control path.

Maintenance start/cancel and input reset use an independent modal. The legacy UI also retains a pending-update modal, but current firmware never requests it: valid host update commands now receive an automatic one-use maintenance grant, without screen confirmation (see `ESP_HOST_UPDATE.md`). For the remaining modal operations, local touch of the separate confirmation button or an exact local Enter byte confirms; Escape rejects. Navigation and unrelated controls are blocked while a modal is active. The dormant update-reject UI action remains for compatibility.

## Rendering, storage and cadence

- One UI PSRAM allocation: **1,572,864 bytes = 1024×768×RGB565 = 1.5 MiB**. The parent's panel scanout framebuffer is a separate 1.5 MiB allocation, owned by the parent.
- No full-screen temporary framebuffer, animation loop, vsync dependency or panel ownership transfer.
- Full redraw happens on page/theme/language/modal changes and changed telemetry on nonterminal pages. Telemetry snapshots are refreshed at most once per second. Power redraws once per second so new history can appear even when instantaneous values are unchanged.
- Terminal updates check parser dirty rows at 50ms intervals. Adjacent dirty rows are submitted as full-width contiguous bands, avoiding a stride-mismatched partial bitmap or a second packed framebuffer. Cursor changes repaint the old/new cursor rows. Terminal status is a separate 32px header. Offscreen terminal dirty state is retained until the terminal is displayed.
- The terminal occupies x=32..991, y=32..703; the bottom navigation occupies y=704..767. TTF glyphs are drawn individually through `ttf_draw_text`, centered/compressed within fixed-width cells using a tiny cell scratch buffer. Wide continuation cells are skipped, bold/inverse colors are handled, and a local underline cursor is drawn. This fixes advances independently of the proportional source font.
- Without a usable font partition, a reduced independent ASCII fallback allows basic diagnostics; Chinese is unavailable and selecting it reports the missing font requirement. This fallback is **not** a complete case-sensitive terminal font. A valid font partition is required for production terminal fidelity, CJK coverage and final visual verification.
- The touch diagnostic repaints only its full-width screen band at up to 20Hz. The API exposes one contact; parent touch handling must select/serialize a single finger.
- `batt_log_get` copies up to 720 existing samples into a small static RAM snapshot. UI never calls `batt_log_start`, samples the sensors or calls learned-capacity helpers. Plots retain the existing fixed ranges: 3.0–4.4V, −2–2A and 0–100%. Invalid voltage samples and invalid SOC are not connected as valid values. Current samples use voltage validity because the historical record has no separate current-valid flag.

## Offline preview

Open `tools/preview/index.html` directly in a browser. The file includes all HTML, CSS and JavaScript, uses installed system fonts, and needs no package installation, CDN, server, font download or network connection. Its Content Security Policy disables network connections and external assets.

Two explicit scenarios are available: Offline/unknown (default) and Connected/MOCK. Mock numbers are never passed off as measurements. Calibration stays unverified in both scenarios; capacity and runtime remain unavailable. Controls simulate requests rather than calling hardware or executing commands.

The terminal supports mock `help`, `status`, `history`, `clear`, and `中文` commands; other input is escaped and echoed. `history` creates enough rows to exercise the 100-line history limit. Shift+PageUp/Down and touch buttons scroll locally. The maintenance page can advance a confirmed mock job in 25% steps. Its untrusted-output test injects text only and cannot approve a modal. The touch pad accepts one pointer and handles pointer cancellation/release. The browser dialog blocks underlying controls and traps keyboard focus.

## Verification and limitations

Run from `MixOS`:

- `node tests/test_preview.cjs`
- `python -B -m unittest discover -s tests -p test_preview_ui.py -v`

The Node suite executes the real inline JavaScript with a minimal DOM contract and tests all 80 page/theme/language/scenario combinations, persistence, offline behavior, grid widths, history bounds, untrusted output, local confirmation, input controls and single-pointer cancellation. It is not a browser layout test.

The Python suite uses Clang with small host-only SDK headers to check C11 syntax with `-Wall -Wextra -Werror`, ownership/safety contracts and a runnable C harness linked to the actual parent-owned terminal parser. The harness checks all 40 firmware page/theme/language combinations, guarded 1.5MiB allocation, idle redraw suppression, partial terminal row submission, CJK cell widths, modal rejection/approval isolation, touch redraw and font-failure paths.

These checks do not establish ESP-IDF integration, actual font baseline/coverage, RGB panel timing, DMA copy behavior, PSRAM throughput, physical touch mapping, real sensor data, calibration validity, USB operation or maintenance/update execution. No hardware was available. The IDE browser connection failed during this workflow, so no browser screenshots or visual-layout pass is claimed. The preview remains directly openable for human review; embedded and browser typography are intentionally not pixel-identical because the preview uses installed fonts.
