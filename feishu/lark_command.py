"""Cross-platform resolution for the lark-cli launcher."""

import shutil
from collections.abc import Callable


def resolve_lark_cli(
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Resolve npm's platform launcher (notably lark-cli.cmd on Windows)."""
    return which("lark-cli") or "lark-cli"
