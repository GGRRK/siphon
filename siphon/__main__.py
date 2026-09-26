"""`python -m siphon get URL...` runs the terminal CLI, `selftest [--net]` checks this machine,
`--version` prints the versions; anything else opens the window."""

import sys

from .paths import setup_environment


def run() -> int:
    if sys.stderr is None:  # the windowed Windows build: no console
        from . import logfile

        logfile.redirect()
    setup_environment()  # before anything imports yt-dlp or libmpv
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "get":
        from .cli import main

        return main(sys.argv[2:])
    if command == "selftest":
        from .selftest import main

        return main(sys.argv[2:])
    if command == "--version":
        from . import __version__
        from .core import engine_version

        print(f"Siphon {__version__}\nyt-dlp {engine_version()}")
        return 0
    from .app import main

    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(run())
