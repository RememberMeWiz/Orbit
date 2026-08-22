"""Whether a person is currently using this machine.

Orbit drives a real application on a real desktop: it switches the visible
conversation and writes into the composer. If the Product Owner is at the
keyboard, that is not merely untidy -- Orbit changes what is on screen under
their hands, and their own typing and Orbit's can interleave in the same window.

So Orbit yields. When someone has used the keyboard or mouse recently the
supervisor skips its cycle and waits, rather than competing for the window. The
machine belongs to the person sitting at it; an unattended operator is only
unattended when nobody is there.

`GetLastInputInfo` reports how long ago the last keyboard or mouse event was
seen, session-wide, which is exactly the question and costs nothing to ask. It
does not distinguish *which* application received the input, so Orbit's own
synthetic input would look like human activity -- but Orbit no longer injects
any: the composer is written through the accessibility value pattern, so the
only remaining synthetic input is the attachment paste, and that is rare and
brief.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

# Long enough that a pause for thought does not hand the window to Orbit
# mid-sentence, short enough that stepping away for a coffee lets work resume.
DEFAULT_IDLE_SECONDS = 45.0


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


@dataclass(frozen=True)
class Presence:
    idle_seconds: float
    present: bool
    measurable: bool

    def to_dict(self) -> dict:
        return {"idle_seconds": round(self.idle_seconds, 1),
                "human_present": self.present,
                "measurable": self.measurable}


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse event, or -1 if unknown."""
    if sys.platform != "win32":
        return -1.0
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return -1.0
        # Both are milliseconds since boot from the same clock, and both wrap at
        # 2**32; the subtraction is taken modulo that so a wrap does not read as
        # forty-nine days of idleness.
        now = ctypes.windll.kernel32.GetTickCount()
        return ((now - info.dwTime) % (2 ** 32)) / 1000.0
    except Exception:
        return -1.0


def presence(threshold_seconds: float = DEFAULT_IDLE_SECONDS) -> Presence:
    """Is someone at the keyboard right now?

    Unmeasurable reads as *absent*, deliberately. Orbit exists to work while
    nobody is watching, so refusing to run on a host where idle time cannot be
    read would disable it entirely on that host -- and the cost of the wrong
    answer here is an interruption, not a safety failure.
    """
    seconds = idle_seconds()
    if seconds < 0:
        return Presence(idle_seconds=-1.0, present=False, measurable=False)
    return Presence(idle_seconds=seconds,
                    present=seconds < threshold_seconds,
                    measurable=True)
