"""Single-writer guarantee for delivery actuation.

The delivery ledger already prevents a *restarted* runner from resending: intent
is written before Send, and a record found mid-actuation reconciles to AMBIGUOUS
rather than retrying. What it never proved is that two *concurrent* runners
cannot each actuate once. Both can load the same record before either persists a
transition, both stage, both press Send.

The primitive is a Windows named mutex, for one reason above all others: there
is no lease timer. A lease is the obvious design and it is wrong here, because
the dangerous window is precisely the one where a holder is slow — inside Send
actuation — and any timeout long enough to be safe is too long to be useful.
Windows supplies the only expiry that is actually safe: when the owning process
dies the kernel marks the mutex abandoned and hands it to the next waiter. A
slow but living holder keeps ownership indefinitely, which is correct.

A waiter that times out reports `writer-busy` and stops. **A timeout never
grants takeover.** That is the whole point.

Acquiring an abandoned mutex means the previous holder died at an unknown point.
`recovered` reports that, but it is a *diagnostic hint and not the safety
mechanism*, because Windows only raises it when some other process already holds
an open handle. Measured, not assumed:

* a waiter already blocked on the mutex when the holder is killed does get
  WAIT_ABANDONED;
* a runner that starts fresh *after* the holder died finds no handles open, so
  the named object was destroyed and recreated, and it acquires cleanly with no
  signal at all.

Both are safe, and for the same reason in each case: the caller reloads the
durable ledger after acquiring, unconditionally, and a record found in
SEND_ACTUATED reconciles to AMBIGUOUS on read. Disk is the recovery authority.
Anything that branched on `recovered` instead would be correct only in the first
case and silently wrong in the second.

What this does not cover, and must not be claimed: the remote service
duplicating a submission internally, a human pressing Send in the same
conversation, or any writer that does not participate in this lock.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF


class SingleWriterUnavailable(RuntimeError):
    """The platform cannot provide the guarantee. Never silently downgraded."""


class WriterBusy(RuntimeError):
    """Another live runner holds the lock. Waiting longer would not be safer."""


@dataclass(frozen=True)
class LockOutcome:
    acquired: bool
    recovered: bool = False
    reason_code: str = "ok"


def mutex_name(ledger_path: Path) -> str:
    """A stable name derived from the ledger this lock protects.

    Hashed rather than embedded: a mutex name is a global object name, so a raw
    path would leak the user's directory layout to any process that enumerates
    them, and would also break on the length and character rules.
    """
    resolved = str(Path(ledger_path).resolve()).lower()
    return "Local\\Orbit-Delivery-" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:32]


def available() -> bool:
    return sys.platform == "win32"


class SingleWriterLock:
    """Exclusive right to transition delivery state, across processes.

    Held across the whole critical section -- reload, transition, actuate,
    persist -- because releasing between any two of those reopens the gap it
    exists to close.
    """

    def __init__(self, ledger_path: Path, *, timeout_seconds: float = 30.0):
        self.ledger_path = Path(ledger_path)
        self.name = mutex_name(self.ledger_path)
        self.timeout_seconds = timeout_seconds
        self._handle: Optional[int] = None
        self.recovered = False

    def __enter__(self) -> "SingleWriterLock":
        outcome = self.acquire()
        if not outcome.acquired:
            raise WriterBusy(outcome.reason_code)
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def acquire(self) -> LockOutcome:
        if not available():
            raise SingleWriterUnavailable("single-writer-requires-windows")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise SingleWriterUnavailable(f"create-mutex-failed:{ctypes.get_last_error()}")

        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        result = kernel32.WaitForSingleObject(handle, int(self.timeout_seconds * 1000))

        if result == WAIT_OBJECT_0:
            self._handle = handle
            self.recovered = False
            return LockOutcome(True)
        if result == WAIT_ABANDONED:
            # The previous holder died at an unknown point. Reported for
            # diagnostics only -- see the module docstring: this signal is
            # absent when no handle was open at the moment of death, so
            # correctness comes from always reloading, never from this flag.
            self._handle = handle
            self.recovered = True
            return LockOutcome(True, recovered=True, reason_code="recovered-from-abandoned")

        self._close(handle)
        if result == WAIT_TIMEOUT:
            return LockOutcome(False, reason_code="writer-busy")
        return LockOutcome(False, reason_code=f"lock-wait-failed:{result}")

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        try:
            kernel32.ReleaseMutex(self._handle)
        finally:
            self._close(self._handle)
            self._handle = None

    @staticmethod
    def _close(handle) -> None:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(handle)
        except OSError:
            pass

    @property
    def held(self) -> bool:
        return self._handle is not None
