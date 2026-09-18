"""Host checks for the preview-owned firmware UI; no ESP-IDF or hardware needed.
Run: python -m unittest discover -s tests -p test_preview_ui.py -v
These stubs validate the C/API contract, not the actual ESP-IDF driver ABI.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / 'firmware/esp32s3/main'
HEADERS = {
    'esp_err.h': '#pragma once\ntypedef int esp_err_t;\n#define ESP_OK 0\n#define ESP_ERR_INVALID_ARG 1\n#define ESP_ERR_INVALID_STATE 2\n#define ESP_ERR_NO_MEM 3\n',
    'esp_lcd_panel_ops.h': '#pragma once\n#include "esp_err.h"\ntypedef void *esp_lcd_panel_handle_t;\nesp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t,int,int,int,int,const void*);\n',
    'esp_heap_caps.h': '#pragma once\n#include <stddef.h>\n#define MALLOC_CAP_SPIRAM 1\n#define MALLOC_CAP_8BIT 2\nvoid *heap_caps_malloc(size_t,unsigned);\n',
    # mix_ui.c shows the build date in the status bar so the running firmware
    # can be identified from the device itself; the real header is ESP-IDF's.
    'esp_app_desc.h': '#pragma once\ntypedef struct {\n unsigned magic_word;\n unsigned secure_version;\n unsigned reserv1[2];\n char version[32];\n char project_name[32];\n char time[16];\n char date[16];\n char idf_ver[32];\n unsigned char app_elf_sha256[32];\n unsigned short min_efuse_blk_rev_full;\n unsigned short max_efuse_blk_rev_full;\n unsigned char mmu_page_size;\n unsigned char reserv3[3];\n unsigned reserv2[18];\n} esp_app_desc_t;\nconst esp_app_desc_t *esp_app_get_description(void);\n',
    'nvs.h': '#pragma once\n#include <stdint.h>\n#include "esp_err.h"\ntypedef unsigned nvs_handle_t;\n#define NVS_READWRITE 1\n#define NVS_READONLY 0\n#define ESP_ERR_NVS_NOT_FOUND 4359\nesp_err_t nvs_open(const char*,int,nvs_handle_t*);\nesp_err_t nvs_get_u8(nvs_handle_t,const char*,uint8_t*);\nesp_err_t nvs_set_u8(nvs_handle_t,const char*,uint8_t);\nesp_err_t nvs_commit(nvs_handle_t);\nvoid nvs_close(nvs_handle_t);\n',
    'driver/i2c_master.h': '#pragma once\ntypedef void *i2c_master_dev_handle_t;\n',
}

class PreviewFirmwareTests(unittest.TestCase):
    def test_c_syntax_with_public_interfaces(self):
        clang = shutil.which('clang')
        if not clang:
            self.skipTest('clang unavailable')
        with tempfile.TemporaryDirectory(prefix='mixui-test-') as tmp:
            for name, contents in HEADERS.items():
                p = Path(tmp) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(contents, encoding='utf-8')
            result = subprocess.run([clang, '-std=c11', '-fsyntax-only', '-Wall', '-Wextra', '-Werror', '-I', tmp, '-I', str(MAIN), str(MAIN / 'mix_ui.c')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_host_ui_terminal_integration(self):
        clang = shutil.which('clang')
        if not clang:
            self.skipTest('clang unavailable')
        with tempfile.TemporaryDirectory(prefix='mixui-host-') as tmp:
            for name, contents in HEADERS.items():
                p = Path(tmp) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(contents, encoding='utf-8')
            exe = Path(tmp) / 'mixui_host.exe'
            result = subprocess.run([clang, '-std=c11', '-I', tmp, '-I', str(MAIN), str(ROOT / 'tests/test_preview_ui_host.c'), str(MAIN / 'mix_terminal.c'), '-o', str(exe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('UI host integration passed', run.stdout)

    def test_single_framebuffer_and_no_hardware_operations(self):
        text = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        self.assertEqual(text.count('heap_caps_malloc('), 1)
        self.assertIn('1572864', text)
        self.assertNotIn('uint16_t scratch[32*24]', text)
        self.assertIn("ttf_draw_cell(fb,W,H,x,y,w,h,size,fg,cp,bold)", text)
        self.assertIn('batt_log_get(history,BATT_LOG_CAP)', text)
        self.assertIn('mix_terminal_row(row)', text)
        self.assertIn('(now-term_ms)>=50', text)
        self.assertIn('(now-view_ms)>=1000', text)
        for forbidden in ['#include "ui.h"', 'i2c_master_transmit(', 'batt_log_start(', 'vsync_mon', 'nvs_flash_erase(']:
            self.assertNotIn(forbidden, text)

    def test_confirmation_never_parses_notice_or_terminal_output(self):
        text = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        notice = text.split('void mix_ui_notice(', 1)[1].split('bool mix_ui_take_action', 1)[0]
        self.assertNotIn('resolve_modal', notice)
        self.assertNotIn('queue(', notice)
        self.assertNotIn('mix_terminal_feed', text)
        self.assertIn('return page==PAGE_APP&&!modal', text)

    def test_passphrase_stays_local_and_is_erased_after_one_request(self):
        """The Wi-Fi passphrase may exist in exactly one buffer, and only until
        the parent has turned it into a single request."""
        text = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        # One buffer, and no second copy anywhere.
        self.assertEqual(text.count('static char net_pass['), 1)
        self.assertEqual(text.count('net_pass_expire=true'), 1)
        take = text.split('bool mix_ui_take_action(', 1)[1].split('\n}', 1)[0]
        self.assertIn('net_pass_expire=true', take)
        # The action queue carries a kind and an int; a passphrase cannot ride it.
        self.assertNotIn('net_pass', text.split('static void queue_value(', 1)[1]
                         .split('\n}', 1)[0])
        tick = text.split('void mix_ui_tick(', 1)[1]
        self.assertIn('memset(net_pass,0,sizeof(net_pass))', tick.split('if(net_pass_expire)', 1)[1]
                      .split('}', 1)[0])

    def test_launcher_starts_only_allow_listed_applications(self):
        """Every start is an APP_OPEN carrying a mix_app_t, never a string."""
        ui = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        view = (MAIN / 'mix_view.h').read_text(encoding='utf-8')
        for app in ('MIX_APP_TRANSLATE', 'MIX_APP_NOTES', 'MIX_APP_AGENT', 'MIX_APP_SHELL'):
            self.assertIn(app, view)
        launch = ui.split('static void launch(int index)', 1)[1].split('\n}', 1)[0]
        # Entering the page is what opens the session, so launch() must delegate.
        # It used to queue the open itself as well, so one card press produced
        # two APP_OPEN actions; the link absorbed the second, but "one press,
        # one action" is the contract, so exactly one place may queue it.
        self.assertIn('navigate(PAGE_APP)', launch)
        self.assertNotIn('queue_value(MIX_ACTION_APP_OPEN', launch)
        # The agent button is a placeholder: it must not open a session. That
        # guard now lives with the queueing, in navigate().
        nav = ui.split('static void navigate(page_t p)', 1)[1].split('\n}', 1)[0]
        self.assertIn('if(current_app!=MIX_APP_AGENT)queue_value(MIX_ACTION_APP_OPEN,current_app)', nav)
        self.assertEqual(ui.count('queue_value(MIX_ACTION_APP_OPEN'), 1)
        for forbidden in ('execv', 'system(', '/bin/', 'popen'):
            self.assertNotIn(forbidden, ui)

if __name__ == '__main__':
    unittest.main()
