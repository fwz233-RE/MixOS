"""Compile the real GT911 driver and reset sequence with host I2C fault injection."""
from pathlib import Path
import shutil
import subprocess
import unittest

from test_mix_health import HEADERS, HarnessDirectory, function

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32s3/main"


class GT911Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clang = shutil.which("clang")
        if not clang:
            raise unittest.SkipTest("clang unavailable")
        directory = HarnessDirectory(prefix="mix-gt911-")
        cls.addClassCleanup(directory.cleanup)
        tmp = Path(directory.name)
        headers = {
            **HEADERS,
            "esp_err.h": HEADERS["esp_err.h"] + "\n#define ESP_ERR_NOT_FOUND 5\n#define ESP_ERR_INVALID_RESPONSE 6\n",
            "driver/i2c_master.h": "#pragma once\ntypedef void *i2c_master_dev_handle_t;\ntypedef void *i2c_master_bus_handle_t;\n",
            "freertos/task.h": HEADERS["freertos/task.h"] + "\nvoid vTaskDelay(unsigned);\n",
            "esp_log.h": """#pragma once
#include <stdio.h>
#define ESP_LOGI(tag,...) do { (void)(tag); if(0)printf(__VA_ARGS__); } while(0)
#define ESP_LOGW ESP_LOGI
#define ESP_LOGE ESP_LOGI
""",
            "aw_touch_reset.h": function((MAIN / "aw9523.c").read_text(encoding="utf-8"),
                                          "esp_err_t aw9523_gt911_reset("),
        }
        for name, contents in headers.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        cls.exe = tmp / "gt911.exe"
        build = subprocess.run(
            [clang, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(tmp), "-I", str(MAIN),
             str(MAIN / "gt911.c"), str(ROOT / "tests/test_gt911_host.c"), "-o", str(cls.exe)],
            capture_output=True, text=True, timeout=60,
        )
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    def run_case(self, name):
        run = subprocess.run([str(self.exe), name], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(f"PASS {name}", run.stdout)

    def test_quick_poll_skips_busy_bus_and_bounds_all_transactions(self):
        self.run_case('quick')

    def test_idle_does_not_ack_or_publish_release(self):
        self.run_case("idle")

    def test_contact_and_zero_contact_frames_ack(self):
        self.run_case("frames")

    def test_status_point_and_ack_errors_never_publish(self):
        self.run_case("read_errors")

    def test_invalid_count_coordinates_and_arguments(self):
        self.run_case("invalid")

    def test_reprobe_alternate_address_and_init(self):
        self.run_case("init")

    def test_init_io_failures_remove_handle(self):
        self.run_case("init_errors")

    def test_reset_restores_pins_on_every_failure(self):
        self.run_case("reset")


if __name__ == "__main__":
    unittest.main()
