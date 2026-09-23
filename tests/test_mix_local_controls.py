"""Host fault injection for the real local-control functions in main.c.

Run: py -3.12 -m unittest discover -s tests -p test_mix_local_controls.py -v
Only a temporary host executable is built; no firmware build, USB, or flash.
The keyboard API supplies a device-reported REQUESTED level, not a physical
PWM measurement. Peripheral stubs validate main's policy and call ordering;
they do not validate the keyboard driver's queue implementation or hardware.
"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from test_mix_health import HEADERS, HarnessDirectory, function

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32s3/main"


def extract_controls(text):
    """Preserve production bodies and control-state initializers verbatim."""
    declarations = re.search(r"static int brightness\s*=.*?;", text)
    if declarations is None:
        raise AssertionError("main brightness/volume declaration was not found")
    state_start = text.index("static bool light_feedback_pending;")
    state_end = text.index("static void adjust_brightness(", state_start)
    bodies = [function(text, name) for name in (
        "static uint32_t clock_ms(",
        "static esp_err_t apply_brightness(",
        "static void adjust_brightness(",
        "static void adjust_volume(",
        "static void adjust_keyboard_light(",
        "static void sync_local_lock(",
        "static void key_event(",
        "static void app_back(",
    )]
    return "\n".join([declarations.group(), text[state_start:state_end], *bodies])


def extract_touch_sampling(text):
    start = text.index('/* Touch sampling state:')
    end = text.index('/* End touch sampling state. */', start)
    return text[start:end]


def extract_touch_poll(text):
    """Use the actual queue drain/recovery/sample sequence from the owner loop."""
    app = function(text, 'void app_main(')
    start = app.index('        dispatch_touch();', app.index('/* Drain previous display-wait input'))
    end = app.index('        int level=gpio_get_level', start)
    return app[start:end]


def extract_draw_sync(text):
    """Run the actual draw/sync sequence, failing if the bridge is reordered."""
    app = function(text, "void app_main(")
    start = app.index("mix_ui_tick(&view,clock_ms());")
    end = app.index("sync_local_lock();", start) + len("sync_local_lock();")
    bridge = app[start:end]
    if not re.fullmatch(
        r"mix_ui_tick\(&view,clock_ms\(\)\);\s*"
        r"(?:/\*.*?\*/\s*)?sync_local_lock\(\);", bridge, re.DOTALL
    ):
        raise AssertionError("expected the main-loop draw followed immediately by lock synchronization")
    return bridge


class LocalControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clang = shutil.which("clang")
        if not clang:
            raise unittest.SkipTest("clang unavailable")
        directory = HarnessDirectory(prefix="mix-local-controls-")
        cls.addClassCleanup(directory.cleanup)
        tmp = Path(directory.name)
        text = (MAIN / "main.c").read_text(encoding="utf-8")
        headers = {
            **HEADERS,
            "esp_err.h": HEADERS["esp_err.h"] + "\n#define ESP_ERR_NOT_FOUND 5\n",
            "driver/i2c_master.h": "#pragma once\ntypedef void *i2c_master_dev_handle_t;\ntypedef void *i2c_master_bus_handle_t;\n",
            "main_local_controls.h": extract_controls(text),
            "main_touch_poll.h": extract_touch_poll(text),
            "main_touch_sampling.h": extract_touch_sampling(text),
            "main_touch_recovery.h": function(text, "static esp_err_t recover_touch("),
            "main_draw_sync.h": extract_draw_sync(text),
        }
        for name, contents in headers.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        cls.exe = tmp / "local_controls.exe"
        build = subprocess.run(
            [clang, "-std=c11", "-Wall", "-Wextra", "-Werror",
             "-I", str(tmp), "-I", str(MAIN),
             str(ROOT / "tests/test_mix_local_controls_host.c"), "-o", str(cls.exe)],
            capture_output=True, text=True, timeout=60,
        )
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    def run_case(self, name):
        # A fresh process resets sync_local_lock's function-local static state;
        # no production function is rewritten to expose private reset hooks.
        run = subprocess.run([str(self.exe), name], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, f"{name}:\n{run.stdout}{run.stderr}")
        self.assertIn(f"PASS {name}", run.stdout)

    def test_brightness_clamps_and_preserves_darkness(self):
        self.run_case("brightness_bounds")

    def test_ledc_set_failure_does_not_commit_or_update(self):
        self.run_case("brightness_set_failure")

    def test_ledc_update_failure_does_not_commit(self):
        self.run_case("brightness_update_failure")

    def test_audio_failure_does_not_commit_or_publish_volume(self):
        self.run_case("volume_failure")

    def test_volume_clamps_and_key_steps(self):
        self.run_case("volume_bounds")

    # Injected legacy states exercise the retained synchronizer defensively;
    # the product UI never enters them. The reserved-key test below covers
    # the new physical-key no-op contract.
    def test_legacy_lock_sync_darkens_and_supersedes_pending_steps(self):
        self.run_case("lock_immediate")

    def test_lock_retains_and_restores_prelock_reported_level(self):
        self.run_case("lock_restore")

    def test_lock_keeps_reconnected_keyboard_dark(self):
        self.run_case("lock_reconnect")

    def test_lock_offline_does_not_invent_restore_level(self):
        self.run_case("lock_offline")

    def test_reserved_lock_key_is_inert_without_disabling_controls(self):
        self.run_case("locked_keys")

    def test_wake_waits_for_successful_ui_draw(self):
        self.run_case("wake_draw_gate")

    def test_lock_and_wake_retry_failed_ledc_operations(self):
        self.run_case("lock_ledc_retry")

    def test_rapid_keyboard_steps_accumulate_target_and_wait_for_report(self):
        self.run_case("keyboard_rapid")

    def test_keyboard_steps_survive_writes_before_device_report_catches_up(self):
        self.run_case("keyboard_stale_report")

    def test_keyboard_target_wraps_modulo_nine(self):
        self.run_case("keyboard_wrap")

    def test_keyboard_write_success_alone_is_not_feedback(self):
        self.run_case("keyboard_report")

    def test_keyboard_unavailable_feedback(self):
        self.run_case("keyboard_offline")

    def test_keyboard_feedback_timeout_and_no_late_success(self):
        self.run_case("keyboard_timeout")

    def test_keyboard_timeout_is_relative_to_latest_step(self):
        self.run_case("keyboard_timeout_restart")

    def test_keyboard_timeout_handles_millisecond_wraparound(self):
        self.run_case("keyboard_timeout_wrap")

    def test_ime_toggle_is_scoped_to_visible_notes(self):
        self.run_case("ime_routing")

    def test_back_is_routed_only_to_the_matching_visible_app(self):
        self.run_case("app_back_routing")

    def test_text_routes_to_ui_or_terminal_and_reports_send_failure(self):
        self.run_case("text_routing")

    def test_wait_sampling_retains_swipe_and_tap_edges_without_ui_reentry(self):
        self.run_case('touch_wait_edges')

    def test_wait_sampling_defers_errors_and_hardware_recovery(self):
        self.run_case('touch_wait_fault')

    def test_wait_queue_overflow_cancels_without_fabricating_release(self):
        self.run_case('touch_wait_overflow')

    def test_dispatch_feedback_can_sample_without_replaying_current_event(self):
        self.run_case('touch_wait_reentrant')

    def test_gt911_normal_release_preserves_latest_contact(self):
        self.run_case("touch_release")

    def test_gt911_errors_cancel_without_synthesizing_release(self):
        self.run_case("touch_error")

    def test_gt911_no_frame_grace_and_poll_interval(self):
        self.run_case("touch_no_frame")

    def test_gt911_recovers_after_three_errors_but_not_idle(self):
        self.run_case("touch_recovery")

    def test_gt911_recovery_retains_handle_and_retries_failures(self):
        self.run_case("touch_recovery_failures")

    def test_gt911_retry_and_clock_wraparound(self):
        self.run_case("touch_retry_wrap")


if __name__ == "__main__":
    unittest.main()
