#!/usr/bin/env python3
"""Verify pinned public upstream call order; read-only, no downloaded files.

Run from any directory. Network access required. This is source verification,
NOT a QMK build or hardware test. Do not silently change PIN to a newer release.
"""
import hashlib
import http.client
import urllib.request

PIN = "a63fd7f01cdabd9ce85bb09ae2b573fd3b8e60aa"
BASE = f"https://raw.githubusercontent.com/qmk/qmk_firmware/{PIN}/"
FILES = ["quantum/keyboard.c", "quantum/action.c", "quantum/quantum.c", "quantum/matrix.c", "tmk_core/protocol/chibios/chibios.c", "quantum/bootmagic/bootmagic.c", "quantum/backlight/backlight.c"]
HASHES = [
    "384fdbbe84ad1e24d2e9241a75ccd7788a5481cd252b68cdea2af339d8878717",
    "b6c6089653acdfeb30e2fa42cb4ecd2e9150161cf1e2149c4ccee35459149fef",
    "8c9c49f6f61fc950c121b8ae9988359cadab6945eeec70b38536e1833c6e2366",
    "754c2888fe139502d9f520de6e9c5db110e938c107cfd9e79698d53610775a12",
    "a942db23db03f4dcda691ad12db5414431ade05ae53eedc2474c0a5c59f0efa4",
    "10b8ff73ce5bae917e79f0f958c982be82f1e9ebb6b9372005cfeb1927ee6eaa",
    "865d663c9d708df11ea514b445b26a64164023298b21a5e3ad0fca19dbf3361d",
]

def ordered(text, *needles):
    previous = -1
    for needle in needles:
        position = text.find(needle, previous + 1)
        if position < 0:
            raise AssertionError(f"Missing/out-of-order upstream fragment: {needle}")
        previous = position

def main():
    source = {}
    for path, expected_hash in zip(FILES, HASHES):
        for attempt in range(3):
            try:
                with urllib.request.urlopen(BASE + path, timeout=30) as response:
                    raw = response.read()
                break
            except (OSError, http.client.HTTPException):
                if attempt == 2:
                    raise
        actual_hash = hashlib.sha256(raw).hexdigest()
        assert actual_hash == expected_hash, f"Upstream content mismatch: {path}"
        source[path] = raw.decode("utf-8")
        print(actual_hash, path, flush=True)
    keyboard = source[FILES[0]]
    ordered(keyboard[keyboard.index("static bool matrix_task(void)"):],
            "matrix_scan();", "has_ghost_in_row(row, current_row)", "continue;", "action_exec(MAKE_KEYEVENT(row, col, key_pressed));")
    ordered(keyboard, "static matrix_row_t get_real_keys", "keycode_at_keymap_location(0, row, col)")
    action = source[FILES[1]]
    ordered(action[action.index("void action_exec(keyevent_t event)"):],
            "pre_process_record_quantum(&record)", "action_tapping_process(record)")
    ordered(action[action.index("#else\n    if (IS_NOEVENT(record.event)"):],
            "pre_process_record_quantum(&record)", "process_record(&record)")
    quantum = source[FILES[2]]
    ordered(quantum[quantum.index("bool pre_process_record_quantum"):],
            "pre_process_record_modules", "pre_process_record_kb", "process_combo")
    ordered(quantum[quantum.index("bool process_record_quantum"):],
            "process_record_kb", "process_backlight", "process_action_kb")
    matrix = source[FILES[3]]
    ordered(matrix[matrix.index("uint8_t matrix_scan(void)"):], "debounce(", "matrix_scan_kb();")
    assert "#if !defined(NO_USB_STARTUP_CHECK)" in source[FILES[4]]
    ordered(keyboard[keyboard.index("void quantum_init(void)"):], "bootmagic();", "void keyboard_init(void)")
    assert "bootloader_jump();" in source[FILES[5]]
    assert "get_backlight_level(void)" in source[FILES[6]]
    print("PASS: debounce -> ghost filter -> action_exec -> prehook; prehook precedes tapping, user processing, backlight and HID actions.")
    print("PASS: blank keys excluded from ghost matrix; Bootmagic separate; NO_USB_STARTUP_CHECK bypasses USB suspend loop.")
    print("LIMIT: this verifier checks sources only; target build/memory evidence is recorded separately. Electrical IRQ and USB capture require hardware.")

if __name__ == "__main__":
    main()
