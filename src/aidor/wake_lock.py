"""Cross-platform wake-lock. Prevents the machine from sleeping mid-run.

Usage:
    with WakeLock(enabled=True):
        ... long-running work ...

On Windows uses SetThreadExecutionState. On Linux uses `systemd-inhibit` as a
best-effort subprocess wrapper (requires the binary). On unsupported / absent
platforms the context manager is a no-op and logs a single warning.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from types import TracebackType

log = logging.getLogger(__name__)


class WakeLock:
    def __init__(self, *, enabled: bool = True, reason: str = "aidor long review run") -> None:
        self.enabled = enabled
        self.reason = reason
        self._windows_prev: int | None = None
        self._linux_proc: subprocess.Popen | None = None

    def __enter__(self) -> WakeLock:
        if not self.enabled:
            return self
        if sys.platform == "win32":
            self._acquire_windows()
        elif sys.platform.startswith("linux"):
            self._acquire_linux()
        elif sys.platform == "darwin":
            self._acquire_macos()
        else:
            log.info("wake-lock: unsupported platform %s; continuing without it", sys.platform)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if not self.enabled:
            return
        try:
            if sys.platform == "win32":
                self._release_windows()
            else:
                self._release_subprocess_lock()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("wake-lock: release failed: %s", exc)

    # ---- Platform-specific --------------------------------------------------

    def _acquire_windows(self) -> None:
        try:
            import ctypes

            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            flags = 0x80000000 | 0x00000001 | 0x00000040
            prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._windows_prev = prev
            log.debug("wake-lock: windows SetThreadExecutionState(0x%x) prev=0x%x", flags, prev)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("wake-lock: windows acquire failed: %s", exc)

    def _release_windows(self) -> None:
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS only
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("wake-lock: windows release failed: %s", exc)

    def _acquire_linux(self) -> None:
        if not shutil.which("systemd-inhibit"):
            log.info("wake-lock: systemd-inhibit not found; sleep may interrupt long runs")
            return
        try:
            self._linux_proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--who=aidor",
                    f"--why={self.reason}",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.debug("wake-lock: linux systemd-inhibit pid=%s", self._linux_proc.pid)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("wake-lock: linux acquire failed: %s", exc)

    def _acquire_macos(self) -> None:  # pragma: no cover - macOS not tested here
        if not shutil.which("caffeinate"):
            log.info("wake-lock: caffeinate not found; sleep may interrupt long runs")
            return
        try:
            self._linux_proc = subprocess.Popen(
                ["caffeinate", "-imsu"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.warning("wake-lock: macos acquire failed: %s", exc)

    def _release_subprocess_lock(self) -> None:
        if self._linux_proc is None:
            return
        try:
            self._linux_proc.terminate()
            self._linux_proc.wait(timeout=5)
        except Exception:
            try:
                self._linux_proc.kill()
            except Exception:  # pragma: no cover
                pass
        finally:
            self._linux_proc = None
