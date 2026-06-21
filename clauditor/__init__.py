"""Top-level ``clauditor`` package: thin public re-exports.

The spec's documented one-liner is::

    from clauditor import log_usage

This package exists purely to make that import work; the implementation lives in
:mod:`collectors.api_wrapper`. Importing this package must NOT require the
optional ``anthropic`` SDK (SPEC.md §13) -- the re-exported helpers are fully
duck-typed and pull in no SDK.
"""

from __future__ import annotations

from collectors.api_wrapper import log_usage, track

__all__ = ["log_usage", "track"]
