"""Run production maintenance handling with local SDK stubs; never use hardware."""
import unittest

from _support import ROOT, host_run, posix_path, require_host_cc
from _ota_sdk_stubs import HEADERS as OTA_HEADERS

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
    "cJSON.h": "#pragma once\n#include <stddef.h>\ntypedef struct {double valuedouble;char *valuestring;} cJSON;\n"
               "cJSON *cJSON_ParseWithLength(const char*,size_t);\n"
               "cJSON *cJSON_GetObjectItemCaseSensitive(cJSON*,const char*);\n"
               "int cJSON_IsNumber(const cJSON*);\nint cJSON_IsString(const cJSON*);\n"
               "int cJSON_IsObject(const cJSON*);\nint cJSON_IsTrue(const cJSON*);\n"
               "void cJSON_Delete(cJSON*);\n",
    "tusb.h": "#pragma once\n#include <stdbool.h>\n#include <stdint.h>\nbool tud_cdc_connected(void);\nbool tud_mounted(void);\nuint32_t tud_cdc_read(void*,uint32_t);\nuint32_t tud_cdc_write(const void*,uint32_t);\nvoid tud_cdc_write_flush(void);\n",
}
HEADERS.update({name: OTA_HEADERS[name] for name in ('esp_timer.h', 'esp_task_wdt.h', 'freertos/task.h', 'freertos/queue.h')})
HEADERS['esp_lcd_panel_ops.h'] = '#pragma once\ntypedef void *esp_lcd_panel_handle_t;\n'


def linux(path):
    return posix_path(path)


def execute(args):
    return host_run(args)


class LinkUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = require_host_cc()
        OUT.mkdir(parents=True, exist_ok=True)
        for name, contents in HEADERS.items():
            dest = OUT / "stubs" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(contents, encoding="utf-8")
        cls.exe = OUT / "link_update_harness"
        execute([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-misleading-indentation",
                 "-g", "-O1", "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie",
                 "-I" + linux(OUT / "stubs"), linux(ROOT / "tests/test_link_update_host.c"),
                 linux(ROOT / "firmware/esp32s3/main/mix_protocol.c"),
                 linux(ROOT / "firmware/esp32s3/main/mix_terminal.c"), "-lm", "-o", linux(cls.exe)])

    def scenario(self, name):
        """Assert on the harness's own per-scenario verdict.

        The harness prints ``PASS <scenario>`` for each case it completes and
        exits non-zero on any failed check. Matching the scenario name means a
        harness that silently skipped a case can no longer report success:
        the previous ``assertIn("passed", ...)`` was true for any zero exit.
        """
        output = execute([linux(self.exe), name])
        self.assertIn(f"PASS {name}", output, msg=output)
        return output

    def test_host_request_without_ui_answer(self):
        self.scenario("automatic")

    def test_malformed_offline_wrong_epoch_and_session(self):
        self.scenario("invalid")

    def test_grant_expiry_and_timer_wrap(self):
        self.scenario("expiry")

    def test_single_use_and_replayed_sequence(self):
        self.scenario("replay")

    def test_disconnect_and_restart_revoke_grant(self):
        self.scenario("reset")

    def test_queue_failure_never_grants(self):
        self.scenario("queue")

    def test_switching_applications_ends_the_previous_one(self):
        """Pressing a different launcher card has to replace the session.

        The host answers a second OPEN with 'terminal already open', so a link
        that quietly reported success left the old application on screen under
        the new one's title.
        """
        self.scenario("switch")

    def test_ota_transfer_resync_refusal_and_teardown(self):
        """The in-protocol A/B update: the path that removes esptool reflashing."""
        self.scenario("ota")

    def test_identify_answers_after_a_fresh_link(self):
        """Which build is running has to be answerable, not inferred.

        A build the bootloader rolled back re-enumerates over USB and completes
        the handshake identically to the build that was just installed, so
        every other signal the updater has looks the same either way.
        """
        self.scenario("identify")


if __name__ == "__main__":
    unittest.main()
