"""One switch that keeps tests from touching the real world.

Set MAYHEM_TESTING=1 (tests/conftest.py does it before anything is imported) and every real
side effect refuses to run: SendInput, killing or launching Roblox, Discord posts, and writes to
the real %LOCALAPPDATA%\\MayhemMacro. Code that does one of those calls guard() first.
"""
import os


class SideEffectBlocked(RuntimeError):
    """A real side effect was attempted while MAYHEM_TESTING=1."""


def testing() -> bool:
    return os.environ.get("MAYHEM_TESTING") == "1"


def guard(what: str) -> None:
    """Raise SideEffectBlocked when testing. `what` names the side effect, e.g. "SendInput"."""
    if testing():
        raise SideEffectBlocked(f"{what} is blocked while MAYHEM_TESTING=1")
