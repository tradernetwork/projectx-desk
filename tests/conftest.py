"""projectx tests: flat modules (px_*.py, sizing.py) live one directory up, so put it on sys.path."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
