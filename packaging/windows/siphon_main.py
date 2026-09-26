"""Entry point of the Windows build: Siphon.exe (the window) and siphon-cli.exe (get, selftest, --version)."""

import sys

from siphon.__main__ import run

sys.exit(run())
