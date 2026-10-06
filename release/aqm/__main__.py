"""`python3 release/aqm <verb>` and `python3 -m aqm <verb>`."""
import pathlib
import sys

if __package__ in (None, ""):            # run as a directory, not a module
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from aqm.cli import main

if __name__ == "__main__":
    sys.exit(main())
