"""Where Orbit keeps durable state, and why it is not under AppData.

Orbit is normally run through the Microsoft Store build of Python, and Store
packages get filesystem virtualization: a write to ``%LOCALAPPDATA%`` is quietly
redirected into that package's private LocalCache. Measured on this host:

    LOCALAPPDATA  C:\\Users\\louis\\AppData\\Local
                  -> ...\\Packages\\PythonSoftwareFoundation.Python.3.12_...\\LocalCache\\Local
    APPDATA       C:\\Users\\louis\\AppData\\Roaming
                  -> ...\\Packages\\PythonSoftwareFoundation.Python.3.12_...\\LocalCache\\Roaming
    USERPROFILE   C:\\Users\\louis                    -> itself

Three consequences, all bad for something meant to run unattended:

* a supervisor launched through a *different* Python resolves the same
  ``%LOCALAPPDATA%`` string to the real directory and finds no lanes at all, so
  two processes that agree on the configured path still cannot see each other's
  work -- and unlike an in-memory cache, restarting does not fix it;
* resetting or reinstalling the Store Python deletes every lane and ledger;
* nothing outside that package can see the state, so it is invisible to backups
  and to any diagnosis that does not already know about the redirection.

So the default lives under ``%USERPROFILE%``, which is not redirected, and the
resolved path is checked against the requested one on every startup. Redirection
is reported rather than tolerated: silently writing to a shadow location is the
failure mode that took a supervisor, a CLI and three registered lanes and left
them unable to see one another.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENV_OVERRIDE = "ORBIT_STATE_ROOT"


@dataclass(frozen=True)
class StateRoot:
    requested: Path
    resolved: Path
    source: str

    @property
    def redirected(self) -> bool:
        """The filesystem put the data somewhere other than where we asked."""
        return self.requested.resolve() != self.resolved

    def to_dict(self) -> dict:
        return {
            "requested": str(self.requested),
            "resolved": str(self.resolved),
            "source": self.source,
            "redirected": self.redirected,
        }


def default_state_root() -> Path:
    """The path Orbit uses when nobody says otherwise.

    ``%USERPROFILE%/.orbit/state`` rather than anything under AppData, because
    AppData is the part Store packaging redirects. Falls back to the home
    directory only if the variable is missing entirely.
    """
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    return (Path(base) if base else Path.home()) / ".orbit" / "state"


def resolve_state_root(explicit: Optional[Path] = None) -> StateRoot:
    """Decide the state root and report where it *actually* landed.

    Precedence is explicit argument, then ORBIT_STATE_ROOT, then the default.
    The directory is created so that resolution reflects reality: an
    unmaterialised path cannot be checked for redirection.
    """
    if explicit is not None:
        requested, source = Path(explicit), "argument"
    elif os.environ.get(ENV_OVERRIDE):
        requested, source = Path(os.environ[ENV_OVERRIDE]), ENV_OVERRIDE
    else:
        requested, source = default_state_root(), "default"

    requested.mkdir(parents=True, exist_ok=True)
    return StateRoot(requested=requested, resolved=requested.resolve(), source=source)


def legacy_state_roots() -> list:
    """Places earlier builds wrote to, for migration and for diagnosis.

    Includes both the path the old code asked for and the redirected one it
    really used, because on a machine without Store Python only the first
    exists, and on this machine only the second holds anything.
    """
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        asked = Path(local) / "Orbit" / "state"
        roots.append(asked)
        landed = asked.resolve()
        if landed != asked:
            roots.append(landed)
    return [r for r in roots if r.exists()]


def describe(root: StateRoot) -> str:
    """One line for the operator, naming the redirection when there is one."""
    if not root.redirected:
        return f"{root.resolved}  (from {root.source})"
    return (f"{root.resolved}  (from {root.source}; REDIRECTED, requested "
            f"{root.requested} — this path is not what it appears to be)")
