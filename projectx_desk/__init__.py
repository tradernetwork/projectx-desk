"""ProjectX Desk: a local trading desk for ProjectX / TopstepX futures accounts. Start it with `python -m projectx_desk`."""
import pathlib

PKG_DIR = pathlib.Path(__file__).resolve().parent       # the code and the UI (static/)
ROOT = PKG_DIR.parent                                     # the checkout: data/, .env.projectx, docs/ live here, never inside the package
