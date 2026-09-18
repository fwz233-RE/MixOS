"""Offline C regression for host-visible USB detach on normal OTA reboot.
No firmware build, hardware, network or deployment artifacts are touched.
"""
import unittest
import test_mix_health as health

ROOT = health.ROOT
MAIN = health.MAIN
UAC = MAIN.parent / "components/usb_device_uac"
TUSB = MAIN.parent / "managed_components/espressif__tinyusb/src"


class RestartTests(unittest.TestCase):
    run_c = health.HealthTests.run_c

    def test_actual_executor_component_and_tinyusb_register_order(self):
        if not TUSB.is_dir():
            self.skipTest("local TinyUSB source unavailable")
        usbd = (TUSB / "device/usbd.c").read_text(encoding="utf-8")
        dcd = (TUSB / "portable/synopsys/dwc2/dcd_dwc2.c").read_text(encoding="utf-8")
        uac = (UAC / "usb_device_uac.c").read_text(encoding="utf-8")
        # Compile REAL complete functions, including ESP32 pad override branch.
        extracts = "\n".join((
            health.function(dcd, "void dcd_disconnect("),
            health.function(usbd, "bool tud_disconnect("),
            uac[uac.index("static atomic_bool s_tinyusb_initialized"):
                uac.index("static portMUX_TYPE s_mux")]))
        for tick_ms in (1, 7, 10, 100):
            with self.subTest(tick_ms=tick_ms):
                extras = {
                    "freertos/FreeRTOS.h": health.HEADERS["freertos/FreeRTOS.h"] +
                        f"\n#define portTICK_PERIOD_MS {tick_ms}u\n",
                    "freertos/task.h": "#pragma once\nvoid vTaskDelay(unsigned);\n",
                    "esp_system.h": "#pragma once\nvoid esp_restart(void);\n",
                    "usb_device_uac.h": "#pragma once\n#include <stdbool.h>\nbool uac_device_disconnect_for_restart(void);\n",
                    "usb_restart_functions.h": extracts,
                }
                out = self.run_c((ROOT / "tests/test_mix_restart_host.c").read_text(encoding="utf-8"), extras)
                self.assertIn("bounded USB detach before one-shot restart passed", out)

    def test_both_normal_paths_restart_before_ui_and_keep_trial_guards(self):
        main = (MAIN / "main.c").read_text(encoding="utf-8")
        for name in ("void app_main(", "static void maintenance_loop("):
            body = health.function(main, name)
            self.assertIn("if(mix_link_take_restart_request())mix_restart();", body)
            start = body.index("mix_link_tick(ms,&view);")
            end = body.index("if(mix_link_take_restart_request())mix_restart();", start)
            self.assertNotIn("mix_ui_", body[start:end])
            self.assertNotIn("mix_keyboard_", body[start:end])
            self.assertNotIn("vTaskDelay", body[start:end])
        self.assertEqual(main.count("if(mix_link_take_restart_request())"), 2)
        self.assertNotIn("Restarting into the new firmware", main)
        self.assertIn('"mix_restart.c"', (MAIN / "CMakeLists.txt").read_text(encoding="utf-8"))
        restart = health.function((MAIN / "mix_restart.c").read_text(encoding="utf-8"), "void mix_restart(")
        for forbidden in ("mix_ui_", "mix_keyboard_", "nvs_", "esp_ota_", "mix_watchdog_task_", "wdt_hal_"):
            self.assertNotIn(forbidden, restart)
        ota = (MAIN / "mix_ota.c").read_text(encoding="utf-8")
        self.assertIn("esp_ota_mark_app_invalid_rollback_and_reboot(); esp_restart(); return;", ota)
        self.assertIn("if (++s_confirm_failures >= 3) esp_restart();", ota)
        self.assertIn("if (!healthy) return; /* Linux absence is not a local firmware fault. */", ota)

    def test_audited_disconnect_is_synchronous_and_never_reconnects(self):
        if not TUSB.is_dir():
            self.skipTest("local TinyUSB source unavailable")
        usbd = (TUSB / "device/usbd.c").read_text(encoding="utf-8")
        disconnect = health.function(usbd, "bool tud_disconnect(")
        self.assertEqual(disconnect.strip(), "bool tud_disconnect(void) {\n  dcd_disconnect(_usbd_rhport);\n  return true;\n}")
        dcd = (TUSB / "portable/synopsys/dwc2/dcd_dwc2.c").read_text(encoding="utf-8")
        detach = health.function(dcd, "void dcd_disconnect(")
        self.assertIn("conf.pad_pull_override = 1;", detach)
        self.assertIn("conf.dp_pullup = 0;", detach)
        self.assertIn("conf.dp_pulldown = 1;", detach)
        self.assertIn("dwc2->dctl |= DCTL_SDIS;", detach)
        # Only explicit connect/disconnect accesses the independent pad override
        # in the DCD; USB interrupts cannot undo physical detach through DCTL.
        self.assertEqual(dcd.count("USB_WRAP.otg_conf = conf;"), 2)
        uac = (UAC / "usb_device_uac.c").read_text(encoding="utf-8")
        init = health.function(uac, "esp_err_t uac_device_init(")
        self.assertLess(init.index("bool usb_init = tusb_init();"), init.index("atomic_store(&s_tinyusb_initialized, true);"))
        self.assertNotIn("tud_connect(", uac)
        # The SDK's private PHY force-disconnect action is HOST-only; it is not
        # an appropriate substitute for the device-mode TinyUSB API.
        if health.SDK.is_dir():
            phy = (health.SDK / "components/usb/include/esp_private/usb_phy.h").read_text(encoding="utf-8")
            self.assertIn("USB_PHY_ACTION_HOST_FORCE_DISCONN", phy)
            self.assertNotIn("USB_PHY_ACTION_DEVICE_FORCE_DISCONN", phy)


if __name__ == "__main__":
    unittest.main()
