"""projectx tests: the `projectx_desk` package lives one directory up, so put the checkout root on sys.path."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
