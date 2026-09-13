"""Preprocess pinned F042 clock configuration; never build/flash a target.

Uses a POSIX preprocessor (local cc on Linux, WSL cc on Windows) and the local
pinned QMK checkout, which QMK_HOME can override. No dependency source or
existing target artifact is modified.
"""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

from _support import ROOT, host_command, posix_path, require_host_cc

KEYBOARD = ROOT / "firmware/keyboard"
QMK = Path(os.environ.get("QMK_HOME", ROOT / ".tools/qmk-0.28.0"))
BOARD = QMK / "platforms/chibios/boards/GENERIC_STM32_F042X6"
HAL = QMK / "lib/chibios/os/hal/ports/STM32"


class KeyboardClockTests(unittest.TestCase):
    def preprocess(self, source, local_override=True):
        compiler = shlex.split(os.environ.get("CC", "")) or [require_host_cc()]
        self.assertTrue((BOARD / "configs/mcuconf.h").is_file(),
                        "Initialize pinned QMK/ChibiOS or set QMK_HOME")
        includes = ([KEYBOARD] if local_override else []) + [
            BOARD / "configs", BOARD / "board", HAL / "STM32F0xx",
            QMK / "lib/chibios/os/hal/ports/common/ARMCMx",
            QMK / "lib/chibios/os/common/startup/ARMCMx/devices/STM32F0xx",
            HAL / "LLD/DMAv1", HAL / "LLD/EXTIv1", HAL / "LLD/TIMv1",
        ]
        # Only the OS abstraction's IRQ-range predicate is supplied here;
        # oscillator capabilities and all clock calculations are real headers.
        command = compiler + ["-E", "-P", "-x", "c", "-DTRUE=1", "-DFALSE=0",
                              "-DOSAL_IRQ_IS_VALID_PRIORITY(n)=((n)>=0 && (n)<4)"]
        command += ["-I" + posix_path(path) for path in includes]
        return subprocess.run(host_command(command + ["-"]), input=source,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=180)

    def test_effective_f042_clock_tree(self):
        source = '''
#include <board.h>
#include <mcuconf.h>
#include <hal_lld.h>
#if !STM32_HAS_HSI48 || !STM32_HSI48_ENABLED
#error USB HSI48 oscillator must be available and enabled
#endif
#if STM32_NO_INIT || !STM32_HSI_ENABLED || STM32_HSE_ENABLED
#error Preserve internal-oscillator startup
#endif
#if STM32_SW != STM32_SW_PLL || STM32_PLLSRC != STM32_PLLSRC_HSI_DIV2
#error Preserve system PLL source
#endif
#if STM32_SYSCLK != 48000000 || STM32_HCLK != 48000000 || STM32_PCLK != 48000000
#error Preserve 48 MHz CPU and bus clocks
#endif
#if STM32_USBSW != STM32_USBSW_HSI48 || STM32_USBCLK != 48000000
#error Preserve independent 48 MHz USB clock
#endif
#if STM32_I2C1SW != STM32_I2C1SW_HSI || STM32_I2C1CLK != 8000000 || STM32_I2C_USE_I2C1
#error Preserve 8 MHz I2C1 clock and exclusive custom slave ownership
#endif
'''
        fixed = self.preprocess(source)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        # Negative control: the real upstream board reproduces the defect.
        upstream = self.preprocess(source, local_override=False)
        self.assertNotEqual(upstream.returncode, 0)
        self.assertIn("USB HSI48 oscillator must be available and enabled", upstream.stderr)

    def test_real_startup_enables_and_waits_for_hsi48(self):
        # Preprocess the actual pinned clock implementation, replacing only its
        # umbrella hal.h include with the real configuration/clock headers.
        # This is not an emulation of hardware oscillator readiness.
        source = (HAL / "STM32F0xx/hal_lld.c").read_text()
        self.assertEqual(source.count('#include "hal.h"'), 1)
        source = source.replace('#include "hal.h"',
                                '#include <board.h>\n#include <mcuconf.h>\n#include <hal_lld.h>')
        for local, expected in ((True, True), (False, False)):
            with self.subTest(local_override=local):
                result = self.preprocess(source, local_override=local)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("RCC->CR2 |= RCC_CR2_HSI48ON;" in result.stdout, expected)
                self.assertEqual("while (!(RCC->CR2 & RCC_CR2_HSI48RDY))" in result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
