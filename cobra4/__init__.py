"""cobra4 — high-level cloud-native language transpiled to Python."""

__version__ = "0.5.0"

# Activate the .c4 import finder so user code can do
#     use my_module          # → my_module.c4 anywhere on sys.path
#     use mypkg.utils        # → mypkg/utils.c4
# without relying on transpile-then-import workflows.
from cobra4 import import_hook as _c4_import_hook

_c4_import_hook.install()
