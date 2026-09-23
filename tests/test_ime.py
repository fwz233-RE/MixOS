"""Checks for the Chinese input path: staging term-ime, building it, proving it.

Everything here runs on the host without a device or network. Most checks need
only Python; the keyboard checks use an existing Clang when available, and the
term-ime state-machine check uses its already staged source and headers. No new
dependencies are installed. The flow-control checks open a real pseudo-terminal,
which exists on Linux and not on Windows.

What these are actually for. The build of term-ime happens on the device and
takes tens of minutes, so every mistake that can be caught here instead of there
saves a cycle — and three of the tests below exist because the mistake happened:
a shell probe whose fallback never ran, a bare ``-j`` that would fork one
compiler per source file, and a pseudo-terminal that ate Ctrl-S.
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'linux')]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PinTests(unittest.TestCase):
    """The pinned tree has to be a tree a recursive clone would have produced."""

    def setUp(self):
        self.stage = load('mixos_stage_ime', ROOT / 'tools/stage_ime.py')

    def test_the_root_is_term_ime_itself(self):
        self.assertEqual(self.stage.PINS[''][0], 'adam-ikari/term-ime')

    def test_every_submodule_lands_inside_a_repository_that_is_also_pinned(self):
        """A gitlink under deps/librime needs deps/librime fetched first."""
        for path in self.stage.PINS:
            if not path:
                continue
            parent = str(Path(path).parent.as_posix())
            while parent not in ('.', ''):
                if parent in self.stage.PINS:
                    break
                parent = str(Path(parent).parent.as_posix())
            else:
                parent = ''
            self.assertIn(parent, self.stage.PINS,
                          f'{path} has no pinned repository containing it')

    def test_every_commit_is_a_full_sha(self):
        for path, (repo, commit) in self.stage.PINS.items():
            self.assertRegex(commit, r'^[0-9a-f]{40}$', f'{path} -> {repo}')

    def test_nothing_is_pinned_twice_to_the_same_place(self):
        self.assertEqual(len(self.stage.PINS), len(set(self.stage.PINS)))

    def test_what_is_dropped_and_what_is_never_fetched_do_not_overlap(self):
        self.assertFalse(set(self.stage.PRUNED) & set(self.stage.OMITTED))

    def test_each_omission_and_each_pruning_carries_a_reason(self):
        for table in (self.stage.PRUNED, self.stage.OMITTED):
            for path, why in table.items():
                self.assertGreater(len(why), 20, f'{path} has no real reason')


class KeyboardImeMappingTests(unittest.TestCase):
    """The physical shortcut is a local action, never global text injection."""

    def test_real_keyboard_one_shot_repeat_and_modifier_regressions(self):
        clang = shutil.which('clang')
        if not clang:
            self.skipTest('existing clang unavailable')
        temporary = Path(tempfile.mkdtemp(prefix='mixos-keyboard-ime-'))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        executable = temporary / 'keyboard.exe'
        build = subprocess.run([
            clang, '-std=c11', '-Wall', '-Wextra', '-Werror',
            '-I', str(ROOT / 'firmware/esp32s3/main'),
            str(ROOT / 'tests/test_input.c'),
            str(ROOT / 'firmware/esp32s3/main/mix_input.c'),
            '-o', str(executable)], capture_output=True, text=True, timeout=60)
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_launcher_advertises_shortcut_only_on_a_mixos_terminal(self):
        source = (ROOT / 'linux/launchers/notes').read_text(encoding='utf-8')
        self.assertIn('if [ "${TERM:-}" = mixos ]; then\n'
                      '        export MIXOS_IME_SHORTCUT=Shift+Space\n'
                      '    else\n'
                      '        unset MIXOS_IME_SHORTCUT\n', source)
        self.assertLess(source.index('export MIXOS_IME_SHORTCUT'),
                        source.index('exec "$IME" "$CONFIG"'))
        self.assertIn('exec "$PYTHON" "$APP"', source)


class TermImeInputHostTests(unittest.TestCase):
    """Compile the real term-ime byte state machine, without Rime or a PTY."""

    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('clang++')
        tree = ROOT / 'build/ime/src'
        if not compiler or not (tree / 'deps/sml/include/boost/sml.hpp').is_file():
            raise unittest.SkipTest('existing clang++ and staged term-ime headers needed')
        temporary = Path(tempfile.mkdtemp(prefix='mixos-term-ime-'))
        cls.addClassCleanup(shutil.rmtree, temporary, ignore_errors=True)
        stage = load('mixos_stage_ime_host', ROOT / 'tools/stage_ime.py')
        hint_edit = next(e for e in stage.SOURCE_EDITS
                         if 'static const char* ImeToggleHint()' in e['replace'])
        # The helper is the exact production patch, not a rewritten test copy.
        helper = hint_edit['replace'].split('\nElement HintsBar()', 1)[0]
        (temporary / 'ime_hint_host.hpp').write_text(
            '#include <cstdlib>\n#include <cstring>\n' + helper, encoding='utf-8')
        cls.executable = temporary / 'term-ime-input.exe'
        build = subprocess.run([
            compiler, '-std=c++17', '-Wall', '-Wextra',
            # spdlog's Windows backend includes windows.h: suppress the GDI
            # Escape() symbol so upstream's input_sm::Escape stays unambiguous.
            '-DNOGDI', '-D_CRT_SECURE_NO_WARNINGS',
            '-I', str(temporary), '-I', str(tree / 'src/core'),
            '-I', str(tree / 'deps/sml/include'),
            '-I', str(tree / 'deps/spdlog/include'),
            '-I', str(ROOT / 'firmware/esp32s3/main'),
            str(ROOT / 'tests/test_ime_input_host.cpp'),
            str(tree / 'src/core/input_processor.cpp'),
            '-o', str(cls.executable)], capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    def test_exact_csi_consumption_and_legacy_ctrl_a_shortcuts(self):
        run = subprocess.run([str(self.executable)], capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_hint_depends_on_terminal_and_explicit_launcher_declaration(self):
        for term, shortcut, expected in (
            ('mixos', 'Shift+Space', 'Shift+Space'),
            ('mixos', None, '^A Space'),
            ('mixos', '', '^A Space'),
            ('mixos', 'other', '^A Space'),
            ('xterm-256color', 'Shift+Space', '^A Space'),
            (None, 'Shift+Space', '^A Space'),
            (None, None, '^A Space'),
        ):
            with self.subTest(term=term, shortcut=shortcut):
                environment = dict(os.environ)
                for name, value in (('TERM', term), ('MIXOS_IME_SHORTCUT', shortcut)):
                    environment.pop(name, None)
                    if value is not None:
                        environment[name] = value
                run = subprocess.run([str(self.executable), '--hint'],
                                     env=environment, capture_output=True,
                                     text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout, expected)


class SourceFixTests(unittest.TestCase):
    """The two kinds of change staging makes to upstream, and their limits."""

    def setUp(self):
        self.stage = load('mixos_stage_ime_fixes', ROOT / 'tools/stage_ime.py')
        self.temporary = Path(tempfile.mkdtemp(prefix='mixos-ime-test-'))
        self.addCleanup(shutil.rmtree, self.temporary, ignore_errors=True)
        self.stage.TREE = self.temporary

    def write(self, relative: str, text: str) -> Path:
        path = self.temporary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='')
        return path

    # -- missing includes ----------------------------------------------------
    def test_the_include_goes_before_the_first_one_already_there(self):
        """Inside the guard, not above it, so a header stays a header."""
        target = next(iter(self.stage.MISSING_INCLUDES))
        header, symbol = self.stage.MISSING_INCLUDES[target]
        self.stage.MISSING_INCLUDES = {target: (header, symbol)}
        path = self.write(target, '#ifndef GUARD_H_\n#define GUARD_H_\n'
                                  '#include <rime/common.h>\n'
                                  f'auto x = std::{symbol}(a, b, c);\n')
        self.stage.add_missing_includes()
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[2], f'#include <{header}>')
        self.assertEqual(lines[0], '#ifndef GUARD_H_')

    def test_a_file_that_already_includes_it_is_left_alone(self):
        target = next(iter(self.stage.MISSING_INCLUDES))
        header, symbol = self.stage.MISSING_INCLUDES[target]
        self.stage.MISSING_INCLUDES = {target: (header, symbol)}
        original = f'#include <{header}>\nauto x = std::{symbol}(a, b);\n'
        path = self.write(target, original)
        applied = self.stage.add_missing_includes()
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        self.assertEqual(applied[0]['state'], 'already present upstream')

    def test_it_refuses_when_the_reason_for_the_include_has_gone(self):
        """Upstream fixing its own code must not leave a silent no-op here."""
        target = next(iter(self.stage.MISSING_INCLUDES))
        header, symbol = self.stage.MISSING_INCLUDES[target]
        self.stage.MISSING_INCLUDES = {target: (header, symbol)}
        self.write(target, '#include <rime/common.h>\nint main() { return 0; }\n')
        with self.assertRaises(RuntimeError) as caught:
            self.stage.add_missing_includes()
        self.assertIn(symbol, str(caught.exception))

    def test_a_missing_file_is_an_error_not_a_shrug(self):
        with self.assertRaises(RuntimeError):
            self.stage.add_missing_includes()

    # -- exact source edits --------------------------------------------------
    def test_the_flow_control_fix_is_what_it_claims_to_be(self):
        """The edit to renderer.cpp must turn IXON off.

        This is the difference between an editor that works and an editor whose
        Save and Back keys are swallowed by XON/XOFF before it sees them.
        """
        edits = [e for e in self.stage.SOURCE_EDITS
                 if e['path'] == 'src/ui/renderer.cpp']
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertIn('IXON', edit['replace'])
        self.assertNotIn('IXON', edit['find'])
        self.assertIn(edit['find'].strip(), edit['replace'])
        self.assertIn('Ctrl-S', edit['why'])

    def test_the_mid_composition_fix_is_what_it_claims_to_be(self):
        """The edit to app.cpp must cancel the syllable and forward the key.

        Control bytes are the notes editor's commands — Save, Back, Record,
        the language switch. Dropped while a syllable is pending, they are dead
        from the first pinyin letter until the composition ends.
        """
        edits = [e for e in self.stage.SOURCE_EDITS
                 if e['path'] == 'src/core/app.cpp'
                 and 'IME composing' in e['find']]
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertIn('byte < 0x20', edit['replace'])
        self.assertIn('ime_->cancel()', edit['replace'])
        self.assertIn('pty_.write', edit['replace'])
        # The upstream fallthrough stays: keys that are neither composition
        # nor control bytes are still ignored, and re-applying is a no-op.
        self.assertIn(edit['find'], edit['replace'])

    def test_the_child_terminal_is_told_the_real_screen_size(self):
        """Pty::spawn hands forkpty a literal 24x80 and nothing corrects it.

        The only call to Pty::resize is in App::on_resize, which runs on
        SIGWINCH. This device's screen never changes size, so no SIGWINCH ever
        arrives and the child keeps 24x80 for its whole life. On a 64x22 screen
        that makes every full-screen program inside term-ime wrap 16 columns
        early and draw three rows past the bottom, which scrolls its own header
        away and puts its footer on the candidate bar.

        The row count has to match the Screen built on the next line: the last
        row belongs to the candidate bar, not to the child.
        """
        edits = [e for e in self.stage.SOURCE_EDITS
                 if e['path'] == 'src/core/app.cpp'
                 and 'Creating screen' in e['find']]
        self.assertEqual(len(edits), 1)
        edit = edits[0]
        self.assertIn('pty_.resize(ws.ws_row - 1, ws.ws_col);', edit['replace'])
        # The anchor is kept, so the screen is still built the same way and
        # re-applying the edit to an already-patched tree is a no-op.
        self.assertIn(edit['find'], edit['replace'])
        # The resize has to come before the anchor: the child is told its size
        # as part of start-up, not after the parser has been built around it.
        self.assertLess(edit['replace'].index('pty_.resize'),
                        edit['replace'].index(edit['find']))

    def test_keyboard_and_hint_patches_survive_a_clean_restage(self):
        """Only SOURCE_EDITS is kept: the generated build tree is disposable."""
        edits = [e for e in self.stage.SOURCE_EDITS
                 if e['path'] in ('src/core/input_processor.cpp', 'src/ui/components.cpp')]
        self.assertEqual(len(edits), 3)
        self.stage.SOURCE_EDITS = edits
        for relative in {e['path'] for e in edits}:
            upstream = '\n'.join(e['find'] for e in edits if e['path'] == relative)
            self.write(relative, upstream)
        applied = self.stage.apply_source_edits()
        self.assertTrue(all(e['state'] == 'applied' for e in applied))
        before = {relative: (self.temporary / relative).read_bytes()
                  for relative in {e['path'] for e in edits}}
        for edit in edits:
            self.assertIn(edit['replace'].encode(), before[edit['path']])
        again = self.stage.apply_source_edits()
        self.assertTrue(all(e['state'] == 'already applied' for e in again))
        for relative, expected in before.items():
            self.assertEqual((self.temporary / relative).read_bytes(), expected)

    def test_local_staged_keyboard_and_hint_match_the_reproducible_patches(self):
        tree = ROOT / 'build/ime/src'
        if not (tree / 'src/ui/components.cpp').is_file():
            self.skipTest('no local staged tree; clean-restage test covers patch source')
        for edit in self.stage.SOURCE_EDITS:
            if edit['path'] in ('src/core/input_processor.cpp', 'src/ui/components.cpp'):
                with self.subTest(path=edit['path'], why=edit['why']):
                    self.assertIn(edit['replace'], (tree / edit['path']).read_text(encoding='utf-8'))

    def test_keyboard_patch_consumes_the_exact_sequence_without_child_output(self):
        edit = next(e for e in self.stage.SOURCE_EDITS
                    if e['path'] == 'src/core/input_processor.cpp')
        self.assertIn('result.data.clear();', edit['replace'])
        self.assertIn('result.forward = false;', edit['replace'])
        self.assertIn('result.toggle_mode = true;', edit['replace'])
        self.assertIn("{0x1b, '[', '3', '2', ';', '2', 'u'}", edit['replace'])
        self.assertIn('sm_.process_event(event);', edit['replace'])

    def test_an_edit_whose_anchor_is_gone_stops_staging(self):
        edit = dict(self.stage.SOURCE_EDITS[0])
        self.stage.SOURCE_EDITS = [edit]
        self.write(edit['path'], 'something else entirely\n')
        with self.assertRaises(RuntimeError) as caught:
            self.stage.apply_source_edits()
        self.assertIn('0 times', str(caught.exception))

    def test_an_anchor_that_appears_twice_stops_staging(self):
        """Replacing both would be a guess about which one mattered."""
        edit = dict(self.stage.SOURCE_EDITS[0])
        self.stage.SOURCE_EDITS = [edit]
        self.write(edit['path'], edit['find'] + 'middle\n' + edit['find'])
        with self.assertRaises(RuntimeError) as caught:
            self.stage.apply_source_edits()
        self.assertIn('2 times', str(caught.exception))

    def test_applying_twice_changes_nothing_the_second_time(self):
        edit = dict(self.stage.SOURCE_EDITS[0])
        self.stage.SOURCE_EDITS = [edit]
        path = self.write(edit['path'], 'before\n' + edit['find'] + 'after\n')
        self.stage.apply_source_edits()
        once = path.read_text(encoding='utf-8')
        applied = self.stage.apply_source_edits()
        self.assertEqual(path.read_text(encoding='utf-8'), once)
        self.assertEqual(applied[0]['state'], 'already applied')

    # -- the scan that found them -------------------------------------------
    def test_the_scan_reports_a_use_without_an_include(self):
        self.write('a.cc', 'auto x = std::any_of(b.begin(), b.end(), f);\n')
        self.assertEqual(self.stage.scan_includes(self.temporary), 1)

    def test_the_scan_stays_quiet_when_the_include_is_there(self):
        self.write('a.cc', '#include <algorithm>\nauto x = std::any_of(b, e, f);\n')
        self.assertEqual(self.stage.scan_includes(self.temporary), 0)

    def test_the_scan_ignores_what_is_never_compiled(self):
        self.write('deps/x/tests/a.cc', 'auto x = std::sort(b, e);\n')
        self.assertEqual(self.stage.scan_includes(self.temporary), 0)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.stage = load('mixos_stage_ime_archive', ROOT / 'tools/stage_ime.py')
        self.temporary = Path(tempfile.mkdtemp(prefix='mixos-ime-pack-'))
        self.addCleanup(shutil.rmtree, self.temporary, ignore_errors=True)
        self.stage.TREE = self.temporary / 'src'
        self.stage.ARCHIVE = self.temporary / 'out.tar.gz'
        (self.stage.TREE / 'src').mkdir(parents=True)
        (self.stage.TREE / 'src/main.cpp').write_text('int main(){}\n')
        (self.stage.TREE / 'CMakeLists.txt').write_text('project(x)\n')

    def test_the_same_tree_packs_to_the_same_bytes(self):
        """The device decides "already sent" from a digest, so it has to be stable."""
        first_count, first = self.stage.pack()
        first_bytes = self.stage.ARCHIVE.read_bytes()
        second_count, second = self.stage.pack()
        self.assertEqual((first_count, first), (second_count, second))
        self.assertEqual(first_bytes, self.stage.ARCHIVE.read_bytes())

    def test_everything_is_rooted_at_one_predictable_directory(self):
        self.stage.pack()
        with tarfile.open(self.stage.ARCHIVE) as archive:
            names = archive.getnames()
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith('term-ime/'), name)

    def test_unpacking_drops_the_wrapper_directory(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            data = b'hello\n'
            info = tarfile.TarInfo('repo-abc123/README')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        source = self.temporary / 'wrapped.tar.gz'
        source.write_bytes(buffer.getvalue())
        destination = self.temporary / 'unpacked'
        self.assertEqual(self.stage.unpack(source, destination), 1)
        self.assertEqual((destination / 'README').read_bytes(), b'hello\n')

    def test_a_member_that_climbs_out_of_the_tree_is_refused(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo('repo-abc123/../../escaped')
            info.size = 0
            archive.addfile(info, io.BytesIO(b''))
        source = self.temporary / 'evil.tar.gz'
        source.write_bytes(buffer.getvalue())
        with self.assertRaises(RuntimeError):
            self.stage.unpack(source, self.temporary / 'unpacked2')


class JobLimitTests(unittest.TestCase):
    """A bare -j is unlimited, and unlimited on this machine means killed."""

    def setUp(self):
        self.worker = load('mixos_build_ime_worker', ROOT / 'tools/build_ime_on_pi.py')

    def test_a_bare_j_becomes_a_bounded_one(self):
        self.assertEqual(self.worker.bounded_arguments(['-j'], 2), ['-j2'])

    def test_an_explicit_count_is_left_alone(self):
        self.assertEqual(self.worker.bounded_arguments(['-j8'], 2), ['-j8'])

    def test_everything_else_arrives_untouched(self):
        arguments = ['--target', 'install', 'VERBOSE=1', '-C', 'sub', '-j', '-k']
        self.assertEqual(self.worker.bounded_arguments(arguments, 3),
                         ['--target', 'install', 'VERBOSE=1', '-C', 'sub', '-j3', '-k'])

    def test_the_shim_uses_that_same_function_rather_than_its_own_copy(self):
        temporary = Path(tempfile.mkdtemp(prefix='mixos-shim-'))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        shim = self.worker.write_make_shim(temporary, 2,
                                           Path('/home/pi/mixos-ime/build_ime_on_pi.py'))
        text = shim.read_text(encoding='utf-8')
        self.assertIn('from build_ime_on_pi import bounded_arguments', text)
        self.assertIn("os.execv('/usr/bin/make'", text)
        self.assertIn('/home/pi/mixos-ime', text)

    def test_the_shim_comes_first_on_the_path(self):
        temporary = Path(tempfile.mkdtemp(prefix='mixos-env-'))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        # The build user exists on the target, not necessarily on a developer's
        # Linux machine. Isolate account lookup from this PATH/shim unit test.
        with mock.patch.object(self.worker.Path, 'expanduser', return_value=temporary):
            environment = self.worker.build_environment(temporary, 'pi', 2)
        self.assertEqual(environment['HOME'], str(temporary))
        self.assertTrue(environment['PATH'].startswith(str(temporary / 'bin')))
        self.assertEqual(environment['CMAKE_BUILD_PARALLEL_LEVEL'], '2')


class PrivilegeTests(unittest.TestCase):
    def setUp(self):
        self.worker = load('mixos_build_ime_priv', ROOT / 'tools/build_ime_on_pi.py')

    def test_it_does_not_try_to_change_user_when_it_is_already_that_user(self):
        """setpriv --init-groups needs privilege; calling it as pi just fails."""
        self.assertEqual(self.worker.as_user('pi', ['/bin/true']), ['/bin/true'])

    def test_the_compile_is_dropped_to_the_ordinary_user_when_root_runs_it(self):
        original = self.worker.running_as_root
        self.worker.running_as_root = lambda: True
        self.addCleanup(setattr, self.worker, 'running_as_root', original)
        command = self.worker.as_user('pi', ['/usr/bin/cmake', '--build', '.'])
        self.assertEqual(command[0], 'setpriv')
        self.assertIn('--reuid', command)
        self.assertEqual(command[-3:], ['/usr/bin/cmake', '--build', '.'])


class VerificationTests(unittest.TestCase):
    """What the checks type, and what they insist on seeing."""

    def setUp(self):
        self.worker = load('mixos_build_ime_verify', ROOT / 'tools/build_ime_on_pi.py')

    def test_the_toggle_is_ctrl_a_then_space(self):
        self.assertEqual(self.worker.TOGGLE, b'\x01 ')

    def test_it_types_pinyin_and_demands_the_characters(self):
        self.assertEqual(self.worker.PINYIN, b'nihao')
        self.assertEqual(self.worker.EXPECTED, '你好')

    def test_escapes_are_stripped_and_the_characters_are_not(self):
        frame = '\x1b[2J\x1b[1;1H\x1b[38;5;7m你好\x1b[0m\x1b[?25l'
        self.assertEqual(self.worker.strip_escapes(frame), '你好')

    def test_the_check_config_names_the_installed_data_rather_than_searching(self):
        """Searching would find the build tree and prove nothing about the install."""
        import json
        temporary = Path(tempfile.mkdtemp(prefix='mixos-cfg-'))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        path = self.worker.user_config(temporary, 'pi', '/bin/cat')
        config = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(config['rime_shared_data_dir'], self.worker.SHARED_DATA)
        self.assertEqual(config['active_language'], 'zh-Hans')
        self.assertEqual(config['shell'], '/bin/cat')

    def test_the_binary_and_the_data_are_both_under_one_prefix(self):
        self.assertTrue(self.worker.BINARY.startswith(self.worker.PREFIX))
        self.assertTrue(self.worker.SHARED_DATA.startswith(self.worker.PREFIX))


class RemoteDriverTests(unittest.TestCase):
    def setUp(self):
        self.driver = load('mixos_build_ime_remote', ROOT / 'tools/build_ime_remote.py')

    def test_a_job_name_it_did_not_print_is_refused(self):
        for bad in ('mixos-ime', 'mixos-ime-2026', '; rm -rf /', 'mixos-ime-20260914'):
            with self.assertRaises(ValueError, msg=bad):
                self.driver.status_script(bad, '/home/pi/mixos-ime')
        self.driver.status_script('mixos-ime-20260914-131025', '/home/pi/mixos-ime')

    def test_the_build_unit_is_root_so_it_can_stop_units_and_install(self):
        command = self.driver.detached_command('mixos-ime-20260914-131025',
                                               '/home/pi/mixos-ime', 'a' * 64, 2, False)
        self.assertIn('systemd-run', command)
        self.assertNotIn('--uid', command)          # root; setpriv drops the compile
        self.assertIn('Restart=no', command)
        self.assertIn('--sha256', command)
        self.assertIn('a' * 64, command)

    def test_the_password_is_never_an_argument(self):
        source = (ROOT / 'tools/build_ime_remote.py').read_text(encoding='utf-8')
        self.assertNotIn('sshpass', source)
        self.assertIn('MIXOS_SSH_PASSWORD', source)
        self.assertIn("data=(password + '\\n')", source)

    def test_installing_cmake_is_skipped_only_on_a_version_it_recognises(self):
        """The old form asked whether a probe said "not installed".

        That probe was a pipeline whose fallback never ran, so a missing cmake
        came back as an empty string and the install was skipped. Deciding from
        the presence of a version instead makes every surprise lead to running
        the install script, which is itself a no-op when cmake is there.
        """
        source = (ROOT / 'tools/build_ime_remote.py').read_text(encoding='utf-8')
        self.assertIn("'cmake version' not in answers.get('cmake', '')", source)
        script = self.driver.install_cmake_script()
        self.assertIn('if command -v cmake >/dev/null; then', script)
        self.assertIn('exit 0', script)
        self.assertIn('--no-install-recommends', script)

    def test_no_probe_puts_its_fallback_after_a_pipeline(self):
        """`cmd | head -1 || echo missing` never reaches the fallback.

        The exit status of a pipeline is the status of its last command, and
        `head` succeeds on empty input, so the `||` is dead code and a missing
        tool reports as an empty answer. Only the probe strings are examined; the
        docstring of `report` quotes the broken form on purpose.
        """
        import inspect
        source = inspect.getsource(self.driver.report)
        probes = source.split('checks = {', 1)[1].split('\n    }', 1)[0]
        for fragment in ('| head -1 || echo', '| tail -1 || echo',
                         '| head -1 || printf', '2>/dev/null || echo'):
            self.assertNotIn(fragment, probes)
        # And the one whose emptiness used to change behaviour asks properly.
        self.assertIn('v=$(cmake --version', probes)


@unittest.skipUnless(sys.platform.startswith('linux'),
                     'a pseudo-terminal and termios are needed for this one')
class FlowControlTests(unittest.TestCase):
    """Ctrl-S has to reach the program, not the line discipline.

    A fresh pseudo-terminal has IXON set, so Ctrl-S is XOFF. The notes editor
    survives that on its own, because when it is the direct child it puts its own
    terminal into raw mode. Running inside term-ime it does not: term-ime clears
    only ICANON, ECHO and ISIG, so Save and Back are swallowed one layer out.
    """

    def test_mixosd_turns_flow_control_off_on_the_session_terminal(self):
        import termios
        mixosd = load('mixos_mixosd_for_flow', ROOT / 'linux/mixosd.py')
        shell = mixosd.PtyShell(64, 22, ['/bin/cat'])
        try:
            attributes = termios.tcgetattr(shell.fd)
            self.assertFalse(attributes[0] & termios.IXON, 'IXON is still on')
            self.assertFalse(attributes[0] & termios.IXOFF, 'IXOFF is still on')
            # And only those: the shell application needs newline translation,
            # so clearing OPOST as a full raw mode would stair-step its output.
            self.assertTrue(attributes[1] & termios.OPOST, 'OPOST was cleared too')
        finally:
            shell.close()

    def test_the_checker_turns_it_off_on_the_terminal_it_owns(self):
        import termios
        worker = load('mixos_build_ime_pty', ROOT / 'tools/build_ime_on_pi.py')
        terminal = worker.Terminal(['/bin/cat'], dict(os.environ), columns=64, rows=22)
        try:
            attributes = termios.tcgetattr(terminal.master)
            self.assertFalse(attributes[0] & termios.IXON)
        finally:
            terminal.close()

    def test_ctrl_s_arrives_at_the_program_instead_of_stopping_its_output(self):
        """The end of it: send Ctrl-S, and see Ctrl-S come back."""
        worker = load('mixos_build_ime_pty_e2e', ROOT / 'tools/build_ime_on_pi.py')
        terminal = worker.Terminal(['/bin/cat'], dict(os.environ), columns=64, rows=22)
        try:
            terminal.send(b'\x13ok\r', settle=1.5)
            self.assertIn('ok', terminal.text)
        finally:
            terminal.close()


if __name__ == '__main__':
    unittest.main()
