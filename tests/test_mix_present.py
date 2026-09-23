"""Offline host tests of the actual RGB presenter, with deterministic SDK/RTOS stubs.

Run: py -3.12 -B -m unittest discover -s tests -p "test_mix_present*.py" -v
No firmware build, device, flash or release artifact is touched. These tests
model the audited bounce-buffer handoff, not DMA/cache/electrical behaviour.
Single/batched rectangles, deferred mirrors and fades run at 1000/100/128 Hz
RTOS tick rates; rejected batches preserve both entire driver images and stats.
Deferred tests compare every submitted/scanning image with the complete canvas
and permit stale pixels only inside the last deferred batch on the other FB.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

from test_mix_health import HarnessDirectory, function

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32s3/main"
SDK = ROOT / ".tools/esp-idf-clean"

HEADERS = {
    "esp_err.h": """#pragma once
#include <stdint.h>
typedef int32_t esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_TIMEOUT 0x107
""",
    "esp_lcd_panel_ops.h": """#pragma once
#include "esp_err.h"
typedef struct mock_panel *esp_lcd_panel_handle_t;
esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t, int, int, int, int, const void *);
""",
    "esp_lcd_panel_rgb.h": """#pragma once
#include "esp_lcd_panel_ops.h"
esp_err_t esp_lcd_rgb_panel_get_frame_buffer(esp_lcd_panel_handle_t, uint32_t, void **, ...);
""",
    "esp_attr.h": "#pragma once\n#define IRAM_ATTR\n#define DRAM_ATTR\n",
    "sdkconfig.h": "#pragma once\n#define CONFIG_IDF_TARGET_ESP32S3 1\n",
    "esp_idf_version.h": """#pragma once
#define ESP_IDF_VERSION_VAL(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define ESP_IDF_VERSION ESP_IDF_VERSION_VAL(5,4,2)
""",
    "esp_timer.h": "#pragma once\n#include <stdint.h>\nint64_t esp_timer_get_time(void);\n",
    "freertos/FreeRTOS.h": """#pragma once
#include <stdint.h>
#include <stdbool.h>
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define pdTRUE 1
#define pdFALSE 0
#define configTICK_RATE_HZ TEST_TICK_HZ
#define configSUPPORT_STATIC_ALLOCATION 1
void mock_enter(portMUX_TYPE *, bool);
void mock_exit(portMUX_TYPE *, bool);
#define portENTER_CRITICAL(p) mock_enter(p, false)
#define portEXIT_CRITICAL(p) mock_exit(p, false)
#define portENTER_CRITICAL_ISR(p) mock_enter(p, true)
#define portEXIT_CRITICAL_ISR(p) mock_exit(p, true)
""",
    "freertos/semphr.h": """#pragma once
#include "freertos/FreeRTOS.h"
typedef struct { int token; bool waiting; } StaticSemaphore_t;
typedef StaticSemaphore_t *SemaphoreHandle_t;
SemaphoreHandle_t xSemaphoreCreateBinaryStatic(StaticSemaphore_t *);
BaseType_t xSemaphoreGiveFromISR(SemaphoreHandle_t, BaseType_t *);
BaseType_t xSemaphoreTake(SemaphoreHandle_t, TickType_t);
""",
}


class PresenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang")
        if not compiler:
            raise unittest.SkipTest("clang unavailable")
        cls.temp = HarnessDirectory(prefix="mix-present-")
        cls.addClassCleanup(cls.temp.cleanup)
        folder = Path(cls.temp.name)
        for name, source in HEADERS.items():
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        cls.binaries = []
        for hz in (1000, 100, 128):
            output = folder / f"present-{hz}.exe"
            built = subprocess.run([
                compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
                f"-DTEST_TICK_HZ={hz}", "-I", str(folder), "-I", str(MAIN),
                str(ROOT / "tests/test_mix_present_host.c"), str(MAIN / "mix_present.c"),
                "-o", str(output),
            ], capture_output=True, text=True, timeout=60)
            if built.returncode:
                raise AssertionError(built.stdout + built.stderr)
            cls.binaries.append((hz, output))

    def run_scenario(self, name):
        for hz, binary in self.binaries:
            with self.subTest(tick_hz=hz):
                result = subprocess.run([str(binary), name], capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"PASS {name}", result.stdout)

    def test_input_hook_samples_during_wait_without_touching_front(self):
        self.run_scenario('hook_sample')

    def test_input_hook_frame_arrival_during_sampling_is_not_lost(self):
        self.run_scenario('hook_race')

    def test_input_hook_can_be_disabled_and_keeps_original_deadline(self):
        self.run_scenario('hook_disable')
        self.run_scenario('hook_timeout')

    def test_init_failures_early_isr_and_atomic_publication(self):
        self.run_scenario("init")

    def test_real_ack_preserves_old_front_until_handoff(self):
        self.run_scenario("full")

    def test_delayed_ack_and_duration_stats(self):
        self.run_scenario("delayed")

    def test_many_partial_updates_preserve_both_full_images_and_stride(self):
        self.run_scenario("partial")

    def test_invalid_rectangles_aliases_and_partial_first_frame(self):
        self.run_scenario("invalid")

    def test_no_ack_times_out_and_permanently_disables_writes(self):
        self.run_scenario("timeout")

    def test_callbacks_before_and_during_submit_cannot_ack_it(self):
        self.run_scenario("early")

    def test_stale_token_from_previous_present_cannot_ack_next(self):
        self.run_scenario("stale")

    def test_duplicate_callbacks_are_conservatively_ignored_before_baseline(self):
        self.run_scenario("duplicate")

    def test_future_callback_between_baseline_and_wait_is_not_lost(self):
        self.run_scenario("baseline_race")

    def test_future_callback_just_before_semaphore_take_is_not_lost(self):
        self.run_scenario("take_race")

    def test_spurious_wakes_never_restart_absolute_deadline(self):
        self.run_scenario("spurious")

    def test_ack_exactly_at_deadline_is_rejected(self):
        self.run_scenario("deadline")

    def test_late_ack_does_not_unlock(self):
        self.run_scenario("late")

    def test_draw_error_locks_without_wait_or_mirror(self):
        self.run_scenario("draw_error")

    def test_draw_error_after_driver_switch_still_locks(self):
        self.run_scenario("draw_error_switched")

    def test_batch_small_rectangles_share_exactly_one_submit_and_wait(self):
        self.run_scenario("batch_small")

    def test_batch_separated_rectangles_leave_gaps_and_stride_untouched(self):
        self.run_scenario("batch_separated")

    def test_batch_overlap_and_duplicates_count_every_actual_write(self):
        self.run_scenario("batch_overlap")

    def test_batch_screen_edges_and_body_navigation_boundary(self):
        self.run_scenario("batch_edges")

    def test_batch_eight_rectangles_and_repeated_front_back_swaps(self):
        self.run_scenario("batch_max")

    def test_batch_all_inputs_validate_before_any_write_or_stats(self):
        self.run_scenario("batch_invalid")

    def test_batch_first_frame_requires_exactly_one_full_screen_rectangle(self):
        self.run_scenario("batch_first")

    def test_batch_normal_or_rejected_calls_cancel_existing_fade(self):
        self.run_scenario("batch_fade_cancel")

    def test_batch_timeout_never_mirrors_and_permanently_locks(self):
        self.run_scenario("batch_timeout")

    def test_batch_callbacks_before_or_during_submit_cannot_ack(self):
        self.run_scenario("batch_early")

    def test_batch_previous_success_callback_cannot_ack_next(self):
        self.run_scenario("batch_stale")

    def test_batch_spurious_wakes_keep_one_absolute_deadline(self):
        self.run_scenario("batch_spurious")

    def test_batch_callback_at_deadline_never_mirrors(self):
        self.run_scenario("batch_deadline")

    def test_batch_late_callback_cannot_clear_fault(self):
        self.run_scenario("batch_late")

    def test_batch_draw_error_never_waits_or_mirrors(self):
        self.run_scenario("batch_draw_error")

    def test_batch_draw_error_after_driver_switch_never_mirrors(self):
        self.run_scenario("batch_draw_error_switched")

    def test_batch_future_callback_at_baseline_is_not_lost(self):
        self.run_scenario("batch_baseline_race")

    def test_batch_future_callback_at_semaphore_take_is_not_lost(self):
        self.run_scenario("batch_take_race")

    def test_deferred_first_full_then_partial_repairs_initial_stale_buffer(self):
        self.run_scenario("deferred_first")

    def test_deferred_rejected_batches_preserve_pixels_stats_and_pending_regions(self):
        self.run_scenario("deferred_invalid")

    def test_deferred_repeated_body_copies_once_without_mirroring_released_front(self):
        self.run_scenario("deferred_body")

    def test_deferred_settings_body_footer_exact_bytes_and_unchanged_sidebar(self):
        self.run_scenario("deferred_settings_regions")

    def test_deferred_disjoint_footer_repairs_body_and_replaces_pending_set(self):
        self.run_scenario("deferred_disjoint")

    def test_deferred_overlap_duplicates_and_touching_bridge_preserve_canvas_gaps(self):
        self.run_scenario("deferred_overlap")

    def test_deferred_sixteen_pending_plus_new_regions_fit_without_losing_pixels(self):
        self.run_scenario("deferred_max")

    def test_deferred_mixed_one_to_eight_region_sequences_match_complete_canvas(self):
        self.run_scenario("deferred_sequences")

    def test_deferred_pending_rejects_fade_until_normal_present_restores_both_buffers(self):
        self.run_scenario("deferred_fade")

    def test_deferred_success_and_all_rejected_input_categories_cancel_fade(self):
        self.run_scenario("deferred_fade_cancel")

    def test_deferred_future_callback_at_baseline_and_duplicate_token(self):
        self.run_scenario("deferred_baseline_race")

    def test_deferred_future_callback_at_take_and_duplicate_token(self):
        self.run_scenario("deferred_take_race")

    def run_deferred_fault(self, fault):
        # Each process starts fresh: cover the first full call AND an update
        # whose back buffer already has pending, disjoint stale regions.
        for scope in ("first_", ""):
            with self.subTest(scope=scope or "pending"):
                self.run_scenario(f"deferred_{scope}{fault}")

    def test_deferred_timeout_preserves_old_front_and_permanently_locks(self):
        self.run_deferred_fault("timeout")

    def test_deferred_callbacks_before_or_during_submit_cannot_ack(self):
        self.run_deferred_fault("early")

    def test_deferred_duplicate_early_callbacks_cannot_ack(self):
        self.run_deferred_fault("duplicate")

    def test_deferred_previous_success_or_idle_token_cannot_ack_next(self):
        self.run_deferred_fault("stale")

    def test_deferred_spurious_wakes_keep_single_absolute_deadline(self):
        self.run_deferred_fault("spurious")

    def test_deferred_ack_at_deadline_is_rejected_without_mirror(self):
        self.run_deferred_fault("deadline")

    def test_deferred_late_ack_cannot_clear_fault(self):
        self.run_deferred_fault("late")

    def test_deferred_draw_error_locks_without_wait_or_mirror(self):
        self.run_deferred_fault("draw_error")

    def test_deferred_draw_error_after_driver_switch_still_locks(self):
        self.run_deferred_fault("draw_error_switched")

    def test_fade_endpoints_and_all_opacities_black_background(self):
        self.run_scenario("fade_black")

    def test_fade_nonblack_channel_accuracy_and_bidirectional_monotonicity(self):
        self.run_scenario("fade_color")

    def test_fade_all_rgb565_colors_and_dense_worst_case(self):
        self.run_scenario("fade_dense")

    def test_outgoing_to_incoming_handoff_needs_no_full_present(self):
        self.run_scenario("fade_handoff")

    def test_fade_begin_never_writes_and_empty_body_never_switches(self):
        self.run_scenario("fade_empty")

    def test_fade_invalid_inputs_alias_alignment_and_first_full(self):
        self.run_scenario("fade_invalid")

    def test_fade_end_and_normal_present_cancel_even_on_invalid_input(self):
        self.run_scenario("fade_cancel")

    def test_fade_many_spans_single_submit_and_bottom_nav_untouched(self):
        self.run_scenario("fade_spans")

    def test_fade_first_last_body_rows_exclude_entire_bottom_nav(self):
        self.run_scenario("fade_body_edges")

    def test_fade_begin_end_without_steps_never_writes_or_switches(self):
        self.run_scenario("fade_begin_end")

    def test_fade_callback_before_or_inside_submit_does_not_ack(self):
        self.run_scenario("fade_early")

    def test_fade_previous_step_token_cannot_ack_next(self):
        self.run_scenario("fade_stale")

    def test_fade_future_callback_at_baseline_is_not_lost(self):
        self.run_scenario("fade_baseline_race")

    def test_fade_future_callback_at_take_is_not_lost(self):
        self.run_scenario("fade_take_race")

    def test_fade_spurious_wakes_keep_absolute_deadline(self):
        self.run_scenario("fade_spurious")

    def test_fade_ack_at_deadline_is_rejected(self):
        self.run_scenario("fade_deadline")

    def test_fade_late_ack_does_not_unlock(self):
        self.run_scenario("fade_late")

    def test_fade_timeout_locks_and_clears_borrowed_canvas(self):
        self.run_scenario("fade_timeout")

    def test_fade_draw_error_locks_before_mirror(self):
        self.run_scenario("fade_draw_error")

    def test_fade_draw_error_after_switch_locks(self):
        self.run_scenario("fade_draw_error_switched")


class DriverContractTests(unittest.TestCase):
    def test_audited_idf_bounce_handoff_and_no_copy_path(self):
        if not SDK.is_dir():
            self.skipTest("local ESP-IDF checkout unavailable; driver contract not verified")
        source = (SDK / "components/esp_lcd/rgb/esp_lcd_panel_rgb.c").read_text(encoding="utf-8")
        header = (SDK / "components/esp_lcd/rgb/include/esp_lcd_panel_rgb.h").read_text(encoding="utf-8")
        # Pin the version-specific safety assumption against the real checkout.
        self.assertIn("The callbacks are all running under ISR environment", header)
        self.assertIn("whole frame buffer is sent to the LCD DMA", header)
        self.assertIn("high priority task has been waken up", header)
        fill = function(source, "static IRAM_ATTR bool lcd_rgb_panel_fill_bounce_buffer(")
        sequence = [fill.index(text) for text in (
            "memcpy(buffer, &panel->fbs[panel->bb_fb_index]",
            "panel->bb_fb_index = panel->cur_fb_index;",
            "cb(&panel->base, NULL, panel->user_ctx)")]
        self.assertEqual(sequence, sorted(sequence))
        draw_signature = "static esp_err_t rgb_panel_draw_bitmap("
        draw = function(source[source.rindex(draw_signature):], draw_signature)
        self.assertIn("draw_buf_copy_to_fb = false;", draw)
        self.assertIn("rgb_panel->cur_fb_index = draw_buf_fb_index;", draw)
        self.assertIn("if (!rgb_panel->bb_size", draw)
        self.assertIn("rgb_panel->cur_fb_index = 0;", source)
        self.assertIn("rgb_panel->bb_fb_index = 0;", source)


if __name__ == "__main__":
    unittest.main()
