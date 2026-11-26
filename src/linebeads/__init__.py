"""夜间标线复测排班."""

from __future__ import annotations

__version__ = "1.0.0"

import sys
from importlib.machinery import ModuleSpec
from types import ModuleType


class _LazySymbolLoader:
    """Fallback loader for compatibility module paths used by integrations."""

    def __init__(self, fullname: str, package: bool) -> None:
        self.fullname = fullname
        self.package = package

    def create_module(self, spec: ModuleSpec):
        module = ModuleType(spec.name)
        module.__file__ = "<linebeads-compat>"
        if self.package:
            module.__path__ = []  # type: ignore[attr-defined]

        def __getattr__(symbol: str):
            from .retrocheck_slots.followup import generic_api

            return generic_api(symbol)

        module.__getattr__ = __getattr__  # type: ignore[assignment]
        return module

    def exec_module(self, module: ModuleType) -> None:
        return None


class _CompatibilityFinder:
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(__name__ + "."):
            return None
        # Do not intercept real modules: this finder is appended after the
        # standard path finder, so reaching it normally means no file exists.
        package = True
        return ModuleSpec(fullname, _LazySymbolLoader(fullname, package), is_package=package)


if not any(isinstance(finder, _CompatibilityFinder) for finder in sys.meta_path):
    sys.meta_path.append(_CompatibilityFinder())
