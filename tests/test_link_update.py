"""Run production maintenance handling with local SDK stubs; never use hardware."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/host-link-update"
HEADERS = {
    # The numeric values are ESP-IDF's own. mix_link.c distinguishes an
    # out-of-order chunk (ESP_ERR_INVALID_STATE, answered with the true write
    # position) from every other refusal, so a stub that collapsed them all
    # onto 1 would let a real regression pass here.
    "esp_err.h": "#pragma once\ntypedef int esp_err_t;\n#define ESP_OK 0\n#define ESP_FAIL -1\n"
                 "#define ESP_ERR_NO_MEM 0x101\n#define ESP_ERR_INVALID_ARG 0x102\n"
                 "#define ESP_ERR_INVALID_STATE 0x103\n#define ESP_ERR_INVALID_SIZE 0x104\n"
                 "#define ESP_ERR_NOT_FOUND 0x105\n#define ESP_ERR_INVALID_CRC 0x107\n",
    "freertos/FreeRTOS.h": "#pragma once\n#define pdTRUE 1\n#define pdPASS 1\n#define pdMS_TO_TICKS(n) (n)\n",
    "freertos/task.h": "#pragma once\nvoid vTaskDelay(unsigned);\nint xTaskCreate(void (*)(void*),const char*,unsigned,void*,unsigned,void*);\n",
    "freertos/queue.h": "#pragma once\n#include <stddef.h>\ntypedef struct test_queue *QueueHandle_t;\nQueueHandle_t xQueueCreate(unsigned,size_t);\nint xQueueSend(QueueHandle_t,const void*,unsigned);\nint xQueueReceive(QueueHandle_t,void*,unsigned);\nvoid xQueueReset(QueueHandle_t);\n",
    "esp_random.h": "#pragma once\n#include <stdint.h>\nuint32_t esp_random(void);\n",
    "cJSON.h": "#pragma once\n#include <stddef.h>\ntypedef struct {double valuedouble;} cJSON;\ncJSON *cJSON_ParseWithLength(const char*,size_t);\ncJSON *cJSON_GetObjectItemCaseSensitive(cJSON*,const char*);\nint cJSON_IsNumber(const cJSON*);\nvoid cJSON_Delete(cJSON*);\n",
    "tusb.h": "#pragma once\n#include <stdbool.h>\n#include <stdint.h>\nbool tud_cdc_connected(void);\nbool tud_mounted(void);\nuint32_t tud_cdc_read(void*,uint32_t);\nuint32_t tud_cdc_write(const void*,uint32_t);\nvoid tud_cdc_write_flush(void);\n",
}


def linux(path):
    text = str(path)
    return "/mnt/" + text[0].lower() + text[2:].replace("\\", "/") if os.name == "nt" else text


def execute(args):
    if os.name == "nt":
        args = ["wsl.exe", "-d", "Ubuntu-22.04", "-u", "fwz233", "--", *args]
    # GCC quotes identifiers with U+2018/U+2019, which the Windows ANSI code
    # page cannot decode. Without an explicit UTF-8 read the diagnostic is lost
    # to a UnicodeDecodeError and the failure reports an empty compiler log.
    result = subprocess.run(args, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(f"Command failed: {args}\n{result.stdout}{result.stderr}")
    return result.stdout


class LinkUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        for name, contents in HEADERS.items():
            dest = OUT / "stubs" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents, encoding="utf-8")
        cls.exe = OUT / "link_update_harness"
        execute(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-misleading-indentation",
                 "-g", "-O1", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie",
                 "-I" + linux(OUT / "stubs"), linux(ROOT / "tests/test_link_update_host.c"),
                 linux(ROOT / "firmware/esp32s3/main/mix_protocol.c"),
                 linux(ROOT / "firmware/esp32s3/main/mix_terminal.c"), "-lm", "-o", linux(cls.exe)])

    def test_host_request_without_ui_answer(self):
        self.assertIn("passed", execute([linux(self.exe), "automatic"]))

    def test_malformed_offline_wrong_epoch_and_session(self):
        self.assertIn("passed", execute([linux(self.exe), "invalid"]))

    def test_grant_expiry_and_timer_wrap(self):
        self.assertIn("passed", execute([linux(self.exe), "expiry"]))

    def test_single_use_and_replayed_sequence(self):
        self.assertIn("passed", execute([linux(self.exe), "replay"]))

    def test_disconnect_and_restart_revoke_grant(self):
        self.assertIn("passed", execute([linux(self.exe), "reset"]))

    def test_queue_failure_never_grants(self):
        self.assertIn("passed", execute([linux(self.exe), "queue"]))

    def test_ota_transfer_resync_refusal_and_teardown(self):
        """The in-protocol A/B update: the path that removes esptool reflashing."""
        self.assertIn("passed", execute([linux(self.exe), "ota"]))


if __name__ == "__main__":
    unittest.main()
