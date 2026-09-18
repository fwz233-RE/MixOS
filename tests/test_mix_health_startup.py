"""Application-only early RTC takeover contract; no firmware/flash/build outputs.

Run: py -3.12 -B -m unittest discover -s tests -p "test_mix_health*.py" -v
Host stubs test policy and compiler retention, not physical watchdog timing or
Xtensa linking. Structural checks deliberately pin the audited local IDF order.
"""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import test_mix_health as health


class EarlyStartupTests(unittest.TestCase):
    def sdk_text(self, relative):
        if not health.SDK.is_dir():
            self.skipTest("local IDF checkout unavailable: startup contract not verified")
        return (health.SDK / "components" / relative).read_text(encoding="utf-8")

    def test_port_prerequisites_precede_first_core_hook_and_scheduler(self):
        port = self.sdk_text("esp_system/port/cpu_start.c")
        # This is the final definition in cpu_start.c. Its mutually exclusive
        # preprocessor branches have unmatched braces before preprocessing, so
        # the simple balanced-brace extractor is not appropriate here.
        cpu0 = port[port.index("void IRAM_ATTR call_start_cpu0("):]
        positions = [cpu0.index(x) for x in (
            "memset(&_bss_start,", "esp_psram_init()", "esp_clk_init();", "SYS_STARTUP_FN();")]
        self.assertEqual(positions, sorted(positions))
        startup = self.sdk_text("esp_system/startup.c")
        start = health.function(startup, "static void start_cpu0_default(")
        positions = [start.index(x) for x in (
            "do_core_init();", "do_global_ctors();", "do_secondary_init();", "esp_startup_start_app();")]
        self.assertEqual(positions, sorted(positions))
        core = health.function(startup, "static void do_core_init(")
        self.assertIn("do_system_init_fn(ESP_SYSTEM_INIT_STAGE_CORE);", core)
        dispatch = health.function(startup, "static void do_system_init_fn(")
        self.assertIn("p->stage == stage_num && (p->cores & BIT(core_id)) != 0", dispatch)
        self.assertIn("p < &_esp_system_init_fn_array_end; ++p", dispatch)
        order = self.sdk_text("esp_system/system_init_fn.txt")
        core_priorities = [int(x) for x in re.findall(r"^CORE:\s+(\d+):", order, re.M)]
        self.assertEqual(min(core_priorities), 1)  # app priority 0 needs no CORE services
        rtos = self.sdk_text("freertos/app_startup.c")
        scheduler = health.function(rtos, "void esp_startup_start_app(")
        self.assertIn("vTaskStartScheduler();", scheduler)
        # These early dependencies use registers/constant clock rates, not an
        # initialized heap, newlib, esp_timer, eFuse driver or scheduler.
        clock = self.sdk_text("esp_hw_support/port/esp32s3/rtc_clk.c")
        hz = health.function(clock, "uint32_t rtc_clk_slow_freq_get_hz(")
        self.assertIn("switch (rtc_clk_slow_src_get())", hz)
        self.assertIn("return SOC_CLK_RC_SLOW_FREQ_APPROX;", hz)
        ll = self.sdk_text("hal/esp32s3/include/hal/rwdt_ll.h")
        stage = health.function(ll, "void rwdt_ll_config_stage(")
        self.assertIn("hw->wdt_config1 = timeout_ticks >> 1;", stage)
        self.assertIn("hw->wdt_config2 = timeout_ticks;", stage)

    def test_app_clock_already_reprograms_boot_watchdog_before_hook(self):
        clock = self.sdk_text("esp_system/port/soc/esp32s3/clk.c")
        init = health.function(clock, "void esp_clk_init(")
        self.assertIn("#ifdef CONFIG_BOOTLOADER_WDT_ENABLE", init)
        self.assertIn("1600ULL * rtc_clk_slow_freq_get_hz()", init)
        selected = init.index("select_rtc_slow_clk(SLOW_CLK_RTC);")
        restored = init.index("(uint64_t)CONFIG_BOOTLOADER_WDT_TIME_MS")
        self.assertLess(selected, restored)
        self.assertIn("wdt_hal_feed(&rtc_wdt_ctx);", init[restored:])
        self.assertIn("WDT_STAGE_ACTION_RESET_RTC", init[restored:])
        # Important boundary: the app value doesn't retroactively change the
        # old bootloader; PSRAM/XIP setup occurs before even this clock code.
        source = (health.MAIN / "mix_health.c").read_text(encoding="utf-8")
        self.assertIn("ESP_SYSTEM_INIT_FN(mix_health_early_rtc, CORE, BIT(0), 0)", source)

    def test_disable_semantics_and_archive_linker_retention_contract(self):
        funcs = self.sdk_text("esp_system/startup_funcs.c")
        self.assertRegex(funcs, r"#ifndef CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE\s+"
                         r"ESP_SYSTEM_INIT_FN\(init_disable_rtc_wdt, SECONDARY, BIT\(0\), 999\)")
        header = self.sdk_text("esp_system/include/esp_private/startup_internal.h")
        self.assertIn("static __attribute__((used)) _SECTION_ATTR_IMPL(\".esp_system_init_fn\", priority)", header)
        self.assertIn(".fn = ( __esp_system_init_fn_##f)", header)
        linker = self.sdk_text("esp_system/ld/esp32s3/sections.ld.in")
        self.assertIn("KEEP (*(SORT_BY_INIT_PRIORITY(.esp_system_init_fn.*)))", linker)
        main = (health.MAIN / "main.c").read_text(encoding="utf-8")
        self.assertIn("mix_health_init()", health.function(main, "void app_main("))
        cmake = (health.MAIN / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn('"mix_health.c"', cmake)
        # The referenced public function and hook are in the SAME archive object.
        source = (health.MAIN / "mix_health.c").read_text(encoding="utf-8")
        self.assertIn("esp_err_t mix_health_init(void)", source)
        self.assertIn("ESP_SYSTEM_INIT_FN(mix_health_early_rtc,", source)
        for name in ("sdkconfig", "sdkconfig.defaults"):
            config = (health.MAIN.parent / name).read_text(encoding="utf-8")
            self.assertIn("CONFIG_BOOTLOADER_WDT_ENABLE=y", config)
            self.assertIn("CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE=y", config)

    def test_one_budget_no_disabled_gap_no_early_services(self):
        source = (health.MAIN / "mix_health.c").read_text(encoding="utf-8")
        # Remove comments so the allowlist covers actual calls, not documentation.
        source = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)
        hook = health.function(source, "ESP_SYSTEM_INIT_FN(mix_health_early_rtc,")
        body = hook[hook.index("{"):]
        self.assertEqual(set(re.findall(r"\b([a-zA-Z_]\w*)\s*\(", body)), {
            "if", "rtc_clk_slow_freq_get_hz", "RWDT_HAL_CONTEXT_DEFAULT",
            "wdt_hal_write_protect_disable", "wdt_hal_config_stage",
            "wdt_hal_enable", "wdt_hal_write_protect_enable"})
        self.assertEqual(source.count("wdt_hal_enable("), 1)
        self.assertEqual(source.count("wdt_hal_config_stage("), 2)
        self.assertNotIn("wdt_hal_feed(", source)
        self.assertNotIn("wdt_hal_init(", source)  # init would disable the hardware
        self.assertIn("if (startup_armed) return ESP_ERR_INVALID_STATE;", body)
        self.assertIn("if (ticks<2 || ticks>UINT32_MAX) return ESP_ERR_INVALID_STATE;", body)
        app_init = health.function(source, "esp_err_t mix_health_init(")
        self.assertIn("if (!startup_armed || initialized) return ESP_ERR_INVALID_STATE;", app_init)
        self.assertNotIn("wdt_hal_", app_init)
        self.assertEqual(source.count("wdt_hal_disable("), 1)
        handoff = health.function(source, "static void disable_startup_watchdog(")
        self.assertIn("wdt_hal_disable(&rtc);", handoff)
        hal = self.sdk_text("hal/wdt_hal_iram.c")
        enable = health.function(hal, "void wdt_hal_enable(")
        # Count the one implicit feed honestly; absence of explicit feed is not
        # evidence of zero hardware feeds.
        self.assertLess(enable.index("rwdt_ll_feed("), enable.index("rwdt_ll_enable("))

    def compile_ir(self, overrides=None):
        clang = shutil.which("clang")
        if not clang:
            self.skipTest("clang unavailable")
        headers = {**health.HEADERS,
                   "esp_private/startup_internal.h": self.sdk_text("esp_system/include/esp_private/startup_internal.h"),
                   "esp_idf_version.h": self.sdk_text("esp_common/include/esp_idf_version.h"),
                   **(overrides or {})}
        with health.HarnessDirectory(prefix="mix-health-startup-") as folder:
            folder = Path(folder)
            for name, value in headers.items():
                dest = folder / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(value, encoding="utf-8")
            output = folder / "health.ll"
            built = subprocess.run([clang, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
                                    "-S", "-emit-llvm", "-I", str(folder), "-I", str(health.MAIN),
                                    str(health.MAIN / "mix_health.c"), "-o", str(output)],
                                   capture_output=True, text=True, timeout=60)
            ir = output.read_text(encoding="utf-8") if built.returncode == 0 else ""
            return built, ir

    def test_actual_idf_macro_retains_optimized_descriptor_and_function(self):
        built, ir = self.compile_ir()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        descriptor = "@esp_system_init_fn_mix_health_early_rtc"
        callback = "@__esp_system_init_fn_mix_health_early_rtc"
        self.assertRegex(ir, re.escape(descriptor) + r" = internal global .*" + re.escape(callback))
        self.assertIn('section ".esp_system_init_fn.0"', ir)
        self.assertRegex(ir, r"@llvm\.(?:compiler\.)?used = .*" + re.escape(descriptor))
        self.assertRegex(ir, r"define internal .*" + re.escape(callback) + r"\(")

    def test_invalid_watchdog_target_or_idf_configuration_fails_compile(self):
        config = health.HEADERS["sdkconfig.h"]
        for key in ("CONFIG_BOOTLOADER_WDT_ENABLE", "CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE",
                    "CONFIG_IDF_TARGET_ESP32S3"):
            with self.subTest(config=key):
                built, _ = self.compile_ir({"sdkconfig.h": config.replace(f"#define {key} 1", "")})
                self.assertNotEqual(built.returncode, 0)
                self.assertIn("error:", built.stderr)
                self.assertIn("RTC", built.stderr)
        with self.subTest(config="unaudited IDF version"):
            built, _ = self.compile_ir({"esp_idf_version.h": health.HEADERS["esp_idf_version.h"].replace(
                "ESP_IDF_VERSION_VAL(5,4,2)", "ESP_IDF_VERSION_VAL(5,5,0)")})
            self.assertNotEqual(built.returncode, 0)
            self.assertIn("Re-audit early RTC watchdog startup ordering", built.stderr)


if __name__ == "__main__":
    unittest.main()
