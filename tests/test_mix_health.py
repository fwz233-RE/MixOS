"""Narrow host fault-injection tests; no firmware build, USB or network access.
Run with: py -3.12 -m unittest discover -s tests -p test_mix_health.py -v
Stubs exercise application policy, not physical hardware or the IDF binary ABI.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from test_preview_ui import HEADERS as UI_HEADERS

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32s3/main"
SDK = ROOT / ".tools/esp-idf-clean"
HEADERS = {
    **UI_HEADERS,
    "esp_err.h": """#pragma once
#include <stdint.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 1
#define ESP_ERR_INVALID_STATE 2
#define ESP_ERR_NO_MEM 3
#define ESP_ERR_TIMEOUT 4
const char *esp_err_to_name(esp_err_t);
""",
    "freertos/FreeRTOS.h": """#pragma once
#include <stddef.h>
#include <stdint.h>
typedef unsigned portMUX_TYPE;
#define portNUM_PROCESSORS 2
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(p) ((void)(p))
#define portEXIT_CRITICAL(p) ((void)(p))
#define portENTER_CRITICAL_ISR(p) ((void)(p))
#define portEXIT_CRITICAL_ISR(p) ((void)(p))
#define pdMS_TO_TICKS(n) (n)
""",
    "freertos/task.h": """#pragma once
#include "freertos/FreeRTOS.h"
typedef void *TaskHandle_t;
TaskHandle_t xTaskGetCurrentTaskHandle(void);
""",
    "esp_attr.h": """#pragma once
#define RTC_NOINIT_ATTR
#define _COUNTER_STRINGIFY(n) #n
#define _SECTION_ATTR_IMPL(s,n) __attribute__((section(s "." _COUNTER_STRINGIFY(n))))
""",
    "sdkconfig.h": """#pragma once
#define CONFIG_IDF_TARGET_ESP32S3 1
#define CONFIG_BOOTLOADER_WDT_ENABLE 1
#define CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE 1
""",
    "esp_idf_version.h": """#pragma once
#define ESP_IDF_VERSION_VAL(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define ESP_IDF_VERSION ESP_IDF_VERSION_VAL(5,4,2)
""",
    "esp_bit_defs.h": "#pragma once\n#define BIT(n) (1u<<(n))\n",
    "esp_cpu.h": "#pragma once\n",
    "soc/soc_caps.h": "#pragma once\n#define SOC_CPU_CORES_NUM 2\n",
    # A fallback when the optional IDF checkout is absent. When present the C
    # health harness below compiles the REAL startup header/macro instead.
    "esp_private/startup_internal.h": """#pragma once
#include "sdkconfig.h"
#include "esp_bit_defs.h"
#include "esp_attr.h"
#include "esp_err.h"
#define ESP_SYSTEM_INIT_STAGE_CORE 0
#define ESP_SYSTEM_INIT_STAGE_SECONDARY 1
typedef struct { esp_err_t (*fn)(void); uint16_t cores, stage; } esp_system_init_fn_t;
#define ESP_SYSTEM_INIT_FN(f,s,c,p,...) \\
    static esp_err_t __esp_system_init_fn_##f(void); \\
    static __attribute__((used)) _SECTION_ATTR_IMPL(".esp_system_init_fn",p) \\
        esp_system_init_fn_t esp_system_init_fn_##f = { \\
            .fn=__esp_system_init_fn_##f,.cores=(c),.stage=ESP_SYSTEM_INIT_STAGE_##s }; \\
    static esp_err_t __esp_system_init_fn_##f(void)
""",
    "esp_log.h": """#pragma once
#define ESP_LOGW(tag,...) do {(void)(tag);} while(0)
#define ESP_LOGE(tag,...) do {(void)(tag);} while(0)
#define ESP_LOGI(tag,...) do {(void)(tag);} while(0)
""",
    "esp_system.h": """#pragma once
typedef enum {ESP_RST_POWERON,ESP_RST_SW,ESP_RST_PANIC,ESP_RST_INT_WDT,
 ESP_RST_TASK_WDT,ESP_RST_WDT,ESP_RST_BROWNOUT} esp_reset_reason_t;
esp_reset_reason_t esp_reset_reason(void);
""",
    "esp_timer.h": "#pragma once\n#include <stdint.h>\nint64_t esp_timer_get_time(void);\n",
    "esp_task_wdt.h": """#pragma once
#include <stdbool.h>
#include "freertos/task.h"
#include "esp_err.h"
typedef struct { uint32_t timeout_ms, idle_core_mask; bool trigger_panic; } esp_task_wdt_config_t;
esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t*);
esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t*);
esp_err_t esp_task_wdt_add(TaskHandle_t);
esp_err_t esp_task_wdt_reset(void);
esp_err_t esp_task_wdt_delete(TaskHandle_t);
""",
    "hal/wdt_hal.h": """#pragma once
#include <stdint.h>
typedef struct {int unused;} wdt_hal_context_t;
#define RWDT_HAL_CONTEXT_DEFAULT() {0}
#define WDT_STAGE0 0
#define WDT_STAGE1 1
#define WDT_STAGE_ACTION_RESET_SYSTEM 2
#define WDT_STAGE_ACTION_RESET_RTC 3
void wdt_hal_write_protect_disable(wdt_hal_context_t*);
void wdt_hal_write_protect_enable(wdt_hal_context_t*);
void wdt_hal_config_stage(wdt_hal_context_t*,int,uint32_t,int);
void wdt_hal_enable(wdt_hal_context_t*);
void wdt_hal_disable(wdt_hal_context_t*);
void wdt_hal_feed(wdt_hal_context_t*);
""",
    "soc/rtc.h": "#pragma once\n#include <stdint.h>\nuint32_t rtc_clk_slow_freq_get_hz(void);\n",
}


def function(text, name):
    """Extract a complete function, preserving its actual implementation."""
    start = text.index(name)
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


class HarnessDirectory(tempfile.TemporaryDirectory):
    def cleanup(self):
        # Windows may briefly retain a just-exited executable mapping (or scan
        # it). Retry only cleanup, with a finite budget; test failures are never
        # swallowed and persistent file locks still fail the test.
        for attempt in range(11):
            try:
                super().cleanup()
                return
            except (PermissionError, NotADirectoryError):
                if attempt == 10:
                    raise
                time.sleep(0.2)


class HealthTests(unittest.TestCase):
    def run_c(self, source, extras=None, other_sources=()):
        clang = shutil.which("clang")
        if not clang:
            self.skipTest("clang unavailable")
        with HarnessDirectory(prefix="mix-health-") as tmp:
            tmp = Path(tmp)
            for name, value in {**HEADERS, **(extras or {})}.items():
                dest = tmp / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(value, encoding="utf-8")
            test = tmp / "test.c"
            test.write_text(source, encoding="utf-8")
            exe = tmp / "health.exe"
            built = subprocess.run(
                [clang, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(tmp),
                 "-I", str(MAIN), str(test), *map(str, other_sources), "-o", str(exe)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            return run.stdout

    def test_task_ownership_freshness_handoff_and_retained_failures(self):
        extras = {}
        if SDK.is_dir():
            extras = {"esp_private/startup_internal.h": (SDK / "components/esp_system/include/esp_private/startup_internal.h").read_text(encoding="utf-8"),
                      "esp_idf_version.h": (SDK / "components/esp_common/include/esp_idf_version.h").read_text(encoding="utf-8")}
        out = self.run_c((ROOT / "tests/test_mix_health_host.c").read_text(encoding="utf-8"), extras)
        self.assertIn("early RTC startup takeover passed", out)
        self.assertIn("health host fault injection passed", out)

    def test_actual_maintenance_and_scanout_functions(self):
        text = (MAIN / "main.c").read_text(encoding="utf-8")
        extracts = "\n".join(function(text, name) for name in (
            "static bool rgb_vsync(", "static bool rgb_frame_complete(",
            "static bool rgb_progress_healthy(", "static void startup_failure(",
            "static void maintenance_loop("))
        out = self.run_c((ROOT / "tests/test_mix_main_health_host.c").read_text(encoding="utf-8"),
                         {"main_health_functions.h": extracts})
        self.assertIn("main maintenance and scanout fault injection passed", out)

    def test_first_full_draw_error_and_repaint_recovery(self):
        # Reuse unchanged preview fixture stubs in a TEMPORARY translation unit.
        # No production/fixture files are rewritten and no new device ABI is invented.
        fixture = (ROOT / "tests/test_preview_ui_host.c").read_text(encoding="utf-8")
        fixture = fixture.replace('#include "../firmware/esp32s3/main/mix_ui.c"', '#include "mix_ui.c"')
        fixture = fixture.replace("static unsigned draws, full_draws, allocations;",
                                  "static esp_err_t injected_draw_result;\nstatic unsigned draws, full_draws, allocations;")
        self.assertIn("last_y0 = y0; last_y1 = y1; return ESP_OK;", fixture)
        fixture = fixture.replace("last_y0 = y0; last_y1 = y1; return ESP_OK;",
                                  "last_y0 = y0; last_y1 = y1; return injected_draw_result;")
        fixture = fixture.replace("int main(void) {", "int preview_original_main(void) {", 1)
        fixture += r'''
int main(void) {
    assert(!mix_ui_draw_healthy());
    assert(mix_ui_last_draw_error()==ESP_ERR_INVALID_STATE);
    mix_terminal_init();assert(mix_ui_init((void *)1)==ESP_OK);note_geometry();
    mix_view_t v={0};injected_draw_result=ESP_FAIL;
    mix_ui_tick(&v,step(1));assert(!mix_ui_draw_healthy()&&repaint);
    assert(mix_ui_last_draw_error()==ESP_FAIL);
    injected_draw_result=ESP_OK;mix_ui_tick(&v,step(1));
    assert(mix_ui_draw_healthy()&&!repaint&&first_frame_presented);
    injected_draw_result=ESP_ERR_INVALID_ARG;present(10,20);
    assert(!mix_ui_draw_healthy()&&repaint);
    injected_draw_result=ESP_OK;present(10,20);
    assert(!mix_ui_draw_healthy()); /* partial success never masks a failed frame */
    mix_ui_tick(&v,step(1));assert(mix_ui_draw_healthy()&&!repaint);
    unsigned count=draws;mix_ui_tick(&v,step(1));
    assert(count==draws&&mix_ui_draw_healthy()); /* static screen is valid */
    puts("UI draw failure recovery passed");return 0;
}
'''
        self.assertIn("UI draw failure recovery passed",
                      self.run_c(fixture, other_sources=(MAIN / "mix_terminal.c",)))

    def test_main_confirmation_requires_render_and_fresh_host_separately(self):
        text = (MAIN / "main.c").read_text(encoding="utf-8")
        app = function(text, "void app_main(")
        self.assertLess(app.index("mix_health_init()"), app.index("nvs_flash_init()"))
        self.assertLess(app.index("nvs_flash_init()"), app.index("mix_ota_init()"))
        self.assertLess(app.index("mix_ota_init()"), app.index("mix_ota_init_worker()"))
        self.assertLess(app.index("mix_ui_tick(&view,ms)"), app.index("mix_ota_health_tick("))
        self.assertIn("mix_ota_local_health_tick(ms,local_healthy);", app)
        self.assertIn("mix_ota_health_tick(ms,local_healthy&&mix_link_host_healthy(ms));", app)
        self.assertIn("draw&&rgb_progress_healthy(ms)&&mix_health_usb_ready()&&mix_health_progress_ok(ms)", app)
        self.assertNotIn("mix_link_usb_mounted()", app)
        maintenance = function(text, "static void maintenance_loop(")
        for forbidden in ("mix_ui_", "start_board(", "ttf_font_", "audio_start(", "lcd_jd9168s_spi_init("):
            self.assertNotIn(forbidden, maintenance)
        self.assertEqual(text.count("lcd_jd9168s_spi_init()"), 1)
        self.assertIn("if(maintenance_mode||mix_health_maintenance_required())maintenance_loop();", app)
        usb = function(text, "static void usb_start_task(")
        self.assertIn("if(with_audio){", usb)
        self.assertIn(".skip_tinyusb_init=false,.output_cb=output,.input_cb=input", usb)
        self.assertIn("MIX_HEALTH_USB_START", usb)
        self.assertIn("if(e==ESP_OK)e=uac_device_init(&c);", usb)
        self.assertIn("if(e==ESP_OK)e=mix_link_start_io();", usb)
        capture = function(text, "static esp_err_t input(")
        self.assertIn("if(!audio_callbacks_enabled()){memset(b,0,n);*got=n;return ESP_OK;}", capture)
        self.assertIn('startup_failure("audio initialization",audio_error);maintenance_loop();', app)

    def test_safe_config_keeps_xip_and_rgb_geometry(self):
        for filename in ("sdkconfig", "sdkconfig.defaults"):
            config = (MAIN.parent / filename).read_text(encoding="utf-8")
            for key in ("LCD_RGB_ISR_IRAM_SAFE", "GDMA_ISR_IRAM_SAFE", "ESP_SYSTEM_PANIC_PRINT_HALT"):
                self.assertNotIn(f"CONFIG_{key}=y", config)
                self.assertTrue(f"CONFIG_{key}=n" in config or f"# CONFIG_{key} is not set" in config)
            for key in ("SPIRAM_XIP_FROM_PSRAM", "ESP_SYSTEM_PANIC_PRINT_REBOOT", "ESP_TASK_WDT_PANIC",
                        "BOOTLOADER_WDT_ENABLE", "BOOTLOADER_WDT_DISABLE_IN_USER_CODE"):
                self.assertIn(f"CONFIG_{key}=y", config)
            self.assertIn("CONFIG_BOOTLOADER_WDT_TIME_MS=30000", config)
            self.assertIn("CONFIG_ESP_TASK_WDT_TIMEOUT_S=15", config)
        main = (MAIN / "main.c").read_text(encoding="utf-8")
        self.assertIn(".num_fbs=2,.bounce_buffer_size_px=LCD_H_RES*16", main)
        self.assertIn(".pclk_hz=LCD_PCLK_HZ,.h_res=LCD_H_RES,.v_res=LCD_V_RES", main)
        config = (MAIN.parent / "sdkconfig").read_text(encoding="utf-8")
        self.assertIn("CONFIG_SPIRAM_FETCH_INSTRUCTIONS=y", config)
        self.assertIn("CONFIG_SPIRAM_RODATA=y", config)

    def test_local_idf_api_contract(self):
        if not SDK.is_dir():
            self.skipTest("local IDF checkout unavailable")
        wdt = (SDK / "components/esp_system/include/esp_task_wdt.h").read_text(encoding="utf-8")
        for signature in ("esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t *config);",
                          "esp_err_t esp_task_wdt_add(TaskHandle_t task_handle);",
                          "esp_err_t esp_task_wdt_reset(void);",
                          "esp_err_t esp_task_wdt_delete(TaskHandle_t task_handle);"):
            self.assertIn(signature, wdt)
        rgb = (SDK / "components/esp_lcd/rgb/include/esp_lcd_panel_rgb.h").read_text(encoding="utf-8")
        self.assertIn("esp_lcd_rgb_panel_frame_buf_complete_cb_t on_frame_buf_complete", rgb)
        self.assertIn("esp_lcd_rgb_panel_vsync_cb_t on_vsync", rgb)
        health = (MAIN / "mix_health.c").read_text(encoding="utf-8")
        self.assertNotIn('#include "rtc_wdt.h"', health)
        self.assertNotIn("wdt_hal_feed(", health)


if __name__ == "__main__":
    unittest.main()
