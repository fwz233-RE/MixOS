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
    'nvs.h': '#pragma once\n#include <stdint.h>\n#include "esp_err.h"\ntypedef unsigned nvs_handle_t;\n#define NVS_READWRITE 1\n#define NVS_READONLY 0\nesp_err_t nvs_open(const char*,int,nvs_handle_t*);\nesp_err_t nvs_get_u8(nvs_handle_t,const char*,uint8_t*);\nesp_err_t nvs_set_u8(nvs_handle_t,const char*,uint8_t);\nesp_err_t nvs_commit(nvs_handle_t);\nvoid nvs_close(nvs_handle_t);\n',
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
        self.assertIn('ttf_draw_cell(fb,W,H,x,y,width,24,20,fg,cp,bold)', text)
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
        self.assertIn('return page==TERMINAL&&!modal', text)

if __name__ == '__main__':
    unittest.main()
