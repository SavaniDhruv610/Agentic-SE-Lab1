"""Task 4 package.

``tool_contract`` must be importable both as a top-level module (as the provided tests do
with ``from tool_contract import ...``) and as ``task4_tools.tool_contract``.  Without an
alias, Python would load two copies of the module and the ``ToolResult`` classes would not
compare equal.  This package initialiser makes both names refer to the same module object,
regardless of which one is imported first.
"""

import sys

if "tool_contract" in sys.modules:
    # A top-level import happened first; reuse that module for the package path.
    sys.modules.setdefault("task4_tools.tool_contract", sys.modules["tool_contract"])
else:
    try:
        from . import tool_contract as _tool_contract
    except ImportError:  # pragma: no cover - package should always contain it
        _tool_contract = None
    if _tool_contract is not None:
        sys.modules.setdefault("tool_contract", _tool_contract)
