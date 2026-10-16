"""Double-click to start MayhemMacro, with no console window.

The app runs from source with the project's own Python in .venv, because
that is where its packages are installed. Opened with any other Python, this
file hands over to .venv\\Scripts\\pythonw.exe and quits. If the app can't
start at all, a small message box says why: a program with no console has
nowhere else to say it.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
VENV_PYTHONW = os.path.join(VENV, "Scripts", "pythonw.exe")


def in_project_venv() -> bool:
    """True when this is the project's own .venv Python."""
    return os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(VENV)


def hand_over(popen=None, exists=os.path.exists) -> bool:
    """Not the project's Python: start this file again with .venv's pythonw
    (no console) and let this one end. False if there is no .venv to use."""
    if not exists(VENV_PYTHONW):
        return False
    if popen is None:
        import subprocess
        popen = subprocess.Popen
    popen([VENV_PYTHONW, os.path.abspath(__file__)], cwd=HERE, close_fds=True)
    return True


def say(text: str) -> None:
    """A plain message box: the only way a windowless start can speak."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "MayhemMacro", 0x10)  # MB_ICONERROR
    except Exception:
        pass


def main() -> None:
    if not in_project_venv() and hand_over():
        return
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import mayhemmacro
    except ImportError as exc:
        say("MayhemMacro couldn't start because a part it needs is missing "
            f"({exc.name or exc}).\n\nIn the MayhemMacro folder, run:\n"
            ".venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return
    mayhemmacro.main()


if __name__ == "__main__":
    main()
