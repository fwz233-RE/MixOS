"""Host checks for the preview-owned firmware UI; no ESP-IDF or hardware needed.
Run: python -m unittest discover -s tests -p test_preview_ui.py -v
These stubs validate the C/API contract, not the actual ESP-IDF driver ABI.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / 'firmware/esp32s3/main'
HEADERS = {
    'esp_err.h': '#pragma once\ntypedef int esp_err_t;\n#define ESP_OK 0\n#define ESP_ERR_INVALID_ARG 1\n#define ESP_ERR_INVALID_STATE 2\n#define ESP_ERR_NO_MEM 3\n',
    'esp_lcd_panel_ops.h': '#pragma once\n#include "esp_err.h"\ntypedef void *esp_lcd_panel_handle_t;\nesp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t,int,int,int,int,const void*);\n',
    'esp_timer.h': '#pragma once\n#include <stdint.h>\nint64_t esp_timer_get_time(void);\n',
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

    def run_host_scenario(self, scenario=None):
        """Compile and execute the real UI/terminal C code with platform stubs.

        Separate processes keep the fixed-page, cancellation, application and
        terminal contracts independent from each other's mutable UI state.
        """
        clang = shutil.which('clang')
        if not clang:
            self.skipTest('clang unavailable')
        from test_mix_local_controls import extract_touch_poll, extract_touch_sampling
        with tempfile.TemporaryDirectory(prefix='mixui-host-') as tmp:
            headers = dict(HEADERS)
            main_source = (MAIN / 'main.c').read_text(encoding='utf-8')
            headers['main_touch_poll.h'] = extract_touch_poll(main_source)
            headers['main_touch_sampling.h'] = extract_touch_sampling(main_source)
            for name, contents in headers.items():
                p = Path(tmp) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(contents, encoding='utf-8')
            exe = Path(tmp) / 'mixui_host.exe'
            result = subprocess.run([clang, '-std=c11', '-O2', '-DMIX_TEST_TOUCH_PIPELINE', '-I', tmp, '-I', str(MAIN), str(ROOT / 'tests/test_preview_ui_host.c'), str(MAIN / 'mix_terminal.c'), '-o', str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            command = [str(exe)] + ([scenario] if scenario else [])
            run = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, f'scenario={scenario or "integration"}\n' + run.stdout + run.stderr)
            return run.stdout

    def test_host_ui_terminal_integration(self):
        output = self.run_host_scenario()
        self.assertIn('UI host integration passed', output)
        self.assertIn('24 launch motion combinations', output)
        self.assertIn('wraparound, cancellation, readiness, draw recovery', output)

    def test_host_unlocked_startup_and_inert_lock_key(self):
        output = self.run_host_scenario('unlocked')
        self.assertIn('unlocked runtime passed', output)
        self.assertIn('Home startup, inert lock key, always awake', output)

    def test_wait_input_queue_drives_fixed_page_taps(self):
        output = self.run_host_scenario('wait-input')
        self.assertIn('wait input passed: queued real UI fixed-page taps', output)

    def test_real_touch_poll_drives_fixed_page_release_and_cancel(self):
        output = self.run_host_scenario('touch_pipeline')
        self.assertIn('touch pipeline passed', output)

    def test_fixed_settings_product_contract(self):
        output = self.run_host_scenario('fixed-settings')
        self.assertIn('fixed-settings runtime passed', output)
        for contract in ('startup awake', 'four-card Settings', 'eight categories',
                         'fixed pages', 'theme persistence', 'controls',
                         'network paging/password', 'device/system confirmations',
                         'inert menu', 'hierarchical Back', 'maintenance/OTA guards'):
            self.assertIn(contract, output)

    def test_notes_larger_font_and_app_geometry_restore(self):
        output = self.run_host_scenario('notes-geometry')
        self.assertIn('notes geometry passed: 48x16, 34px', output)

    def test_host_bottom_navigation_targets_and_fade_interruption(self):
        output = self.run_host_scenario('navigation')
        self.assertIn('navigation runtime passed: three 120x120 targets', output)
        self.assertIn('Back hierarchy, Home, direct Settings', output)
        self.assertIn('launch and fade interruption', output)
        self.assertIn('empty pages, modal and busy guards, single CLOSE', output)

    def test_host_fixed_page_touch_cancellation(self):
        output = self.run_host_scenario('settings-cancel')
        self.assertIn('settings cancel passed: 12px threshold, press-coordinate jitter', output)
        self.assertIn('all fixed pages without scroll', output)

    def test_agent_local_page_preserves_terminal_isolation(self):
        output = self.run_host_scenario('empty')
        self.assertIn('32 Agent theme/language/link/session combinations', output)
        self.assertIn('no host requests or terminal clean/render', output)

    def test_host_bilingual_layout_density_and_status(self):
        output = self.run_host_scenario('layout')
        self.assertIn('12 themes, bilingual size-aware bounds, non-overlapping text', output)
        self.assertIn('UTF-8 clipping, date rollover and measured speed formatting', output)

    def test_official_navigation_masks_and_inactive_menu(self):
        output = self.run_host_scenario('nav-icons')
        self.assertIn('material navigation passed: exact official masks, 12 themes', output)

    def test_fixed_pages_have_no_scroll_budget(self):
        output = self.run_host_scenario('settings-budget')
        self.assertIn('settings render budget passed: no scroll presents', output)
        self.assertIn('footer-only telemetry, link changes, readable notices', output)

    def test_four_card_typography_uses_one_shared_contract(self):
        output = self.run_host_scenario('card-style')
        self.assertIn('card style passed: all four cards share type', output)

    def test_terminal_sparse_batch_cadence_and_failure_retention(self):
        output = self.run_host_scenario('terminal-budget')
        self.assertIn('terminal budget passed: sparse batches, one footer latch, cursor erasure', output)
        self.assertIn('bounded rectangles, 16ms admission, idle sleep, clock wrap, retained failed rows', output)

    def test_theme_palette_roles_match_presets(self):
        ui = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        md3 = (MAIN / 'mix_md3.c').read_text(encoding='utf-8')
        palette = ui.split('static const palette_t palettes[MIX_THEME_COUNT] = {', 1)[1].split('};', 1)[0]
        rgb = [tuple(map(int, match)) for match in re.findall(r'RGB\((\d+),(\d+),(\d+)\)', palette)]
        roles = [int(value, 16) for value in re.findall(r'0x([0-9A-Fa-f]{6})', md3)]
        self.assertEqual(len(rgb), 12 * 7)
        self.assertEqual(len(roles), 12 * 12)
        for theme in range(12):
            for local, role in enumerate((0, 1, 2, 4, 5, 7, 11)):
                r, g, b = rgb[theme * 7 + local]
                self.assertEqual((r << 16) | (g << 8) | b, roles[theme * 12 + role],
                                 f'theme={theme}, role={role}')
        self.assertIn('NOT generated', md3)

    def test_host_toast_partial_present_lifetime_and_frozen_canvas(self):
        output = self.run_host_scenario('toast')
        self.assertIn('toast runtime passed: partial present, consecutive updates, 1500ms expiry', output)
        self.assertIn('terminal overlay retention, immutable incoming fade', output)

    def test_single_framebuffer_and_no_hardware_operations(self):
        text = (MAIN / 'mix_ui.c').read_text(encoding='utf-8')
        self.assertEqual(text.count('heap_caps_malloc('), 1)
        self.assertIn('1572864', text)
        self.assertNotIn('uint16_t scratch[32*24]', text)
        self.assertIn("ttf_draw_cell(fb,W,H,x,y,w,h,size,fg,cp,bold)", text)
        self.assertIn('batt_log_get(history,BATT_LOG_CAP)', text)
        self.assertIn('mix_terminal_row(row)', text)
        self.assertIn('(now-term_ms)>=TERM_FRAME_MS', text)
        self.assertIn('#define TERM_FRAME_MS 16u', text)
        self.assertIn('#define TERM_FAST_MS 100u', text)
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
        # Translate, Notes and Shell own host sessions. Agent is UI-local;
        # the fourth card is Settings and must never serialize APP_OPEN.
        nav = ui.split('static void navigate(page_t p)', 1)[1].split('\n}', 1)[0]
        self.assertIn('if(app_has_session())queue_value(MIX_ACTION_APP_OPEN,current_app)', nav)
        self.assertIn('if(page==PAGE_APP&&app_has_session())queue(MIX_ACTION_TERMINAL_CLOSE)', nav)
        whitelist = ui.split('static bool app_has_session(void)', 1)[1].split('\n}', 1)[0]
        for app in ('MIX_APP_TRANSLATE', 'MIX_APP_NOTES', 'MIX_APP_SHELL'):
            self.assertIn(f'current_app=={app}', whitelist)
        self.assertNotIn('MIX_APP_AGENT', whitelist)
        self.assertNotIn('UI_APP_GAME', launch)
        self.assertNotRegex(view, r'\bMIX_APP_GAMES?\b')
        self.assertIn('navigation_settings()', launch)
        self.assertEqual(ui.count('queue_value(MIX_ACTION_APP_OPEN'), 1)
        for forbidden in ('execv', 'system(', '/bin/', 'popen'):
            self.assertNotIn(forbidden, ui)

if __name__ == '__main__':
    unittest.main()
