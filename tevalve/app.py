"""Start-up: single-instance lock, device detection, threads, GUI.
"""


import os
import threading
from datetime import datetime
from pathlib import Path

from . import logfile
from . import shared
from .config import LOG_INTERVAL_S
from .devices import (
    LABJACK_AVAILABLE, check_sense_pins, detect_keller_bus, keller_thread,
    labjack_thread,
)
from .gui import TEGui
from .logfile import logger_thread
from .shared import log_event


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE-INSTANCE LOCK
# ═══════════════════════════════════════════════════════════════════════════════
# Two copies of this program cannot share the LabJack, and — more importantly —
# a forgotten instance may still be driving FIO0. So the second copy refuses to
# start rather than failing halfway through connecting.
#
# The lock is an OS-level file lock held open for the life of the process. The
# operating system releases it automatically when the process dies, however it
# dies, so there is no stale-lock file to clean up by hand.

_LOCK_PATH = str(Path(__file__).resolve().parent.parent / ".te-valve-driver.lock")
_lock_fh = None


def acquire_single_instance_lock():
    """Return True if we got the lock, False if another instance holds it."""
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_PATH, 'a+')
    except Exception:
        return True          # can't create a lock file — don't block the user

    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_fh.close()
        _lock_fh = None
        return False
    except Exception:
        return True          # unsupported platform — fail open, not closed

    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
    _lock_fh.flush()
    return True



# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():

    print("\nTE-VALVE-DRIVER — startup")
    print("─" * 50)

    if not acquire_single_instance_lock():
        print("\n" + "!" * 60)
        print("  ANOTHER INSTANCE IS ALREADY RUNNING.")
        print()
        print("  That instance still owns the LabJack, and it may still be")
        print("  driving the heater on FIO0. Close its window, or end the")
        print("  python.exe task, then start this one again.")
        print()
        print("  If in doubt about the heater: SW171 off and 24 V off at")
        print("  the wall makes it safe regardless of what software does.")
        print("!" * 60 + "\n")
        return

    pin_err = check_sense_pins()
    if pin_err:
        print(f"\nCONFIG ERROR: {pin_err}\nFix the heater sense settings and restart.\n")
        return

    logfile.init_paths()

    print("Scanning for Keller sensor...")
    keller_port, keller_bus = detect_keller_bus()
    shared.keller_ok = keller_bus is not None
    if not shared.keller_ok:
        print("  [Keller] NOT FOUND — upstream pressure/temperature will be blank.")

    if LABJACK_AVAILABLE:
        print("LabJack will connect on its own thread (vacuum + thermocouple).")
    else:
        print("LabJack not available — vacuum and TE temperature disabled.")

    print(f"\nLogging to: {logfile.LOG_FILE}")
    print(f"Heater switching log: {logfile.PWM_LOG_FILE}")
    print(f"Log cadence: every {LOG_INTERVAL_S:g} s (drift-free)")
    print("─" * 50)

    # ── Start worker threads ───────────────────────────────────────────────
    if keller_bus is not None:
        threading.Thread(target=keller_thread, args=(keller_port, keller_bus),
                         daemon=True).start()
    if LABJACK_AVAILABLE:
        threading.Thread(target=labjack_thread, daemon=True).start()
    threading.Thread(target=logger_thread, daemon=True).start()

    log_event("System started")
    if shared.keller_ok:
        log_event(f"Keller online · {keller_port}")

    # ── GUI runs on the main thread ────────────────────────────────────────
    TEGui().run()


def run():
    """Entry point for TE-VALVE-DRIVER.py: keeps the console open on a crash,
    so a double-clicked window doesn't vanish before the error can be read."""
    try:
        main()
    except Exception:
        import traceback
        print("\n" + "=" * 60)
        print("FATAL ERROR:")
        print("=" * 60)
        traceback.print_exc()
    finally:
        input("\nPress Enter to close...")
