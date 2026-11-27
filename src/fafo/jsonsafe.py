"""Iterative walks over untrusted nested data (snapshot host, quota and gpu values).

Nothing here recurses: an explicit stack, a hard depth cap and a node cap, so hostile nesting
can never hit the interpreter recursion limit (which differs between Python versions).
"""

from __future__ import annotations

import math

MAX_DEPTH = 32  # container nesting levels kept; deeper values become None
MAX_NODES = 100_000  # values visited; the rest become None


def _items(container):
    return container.items() if isinstance(container, dict) else enumerate(container)


def _is_container(v) -> bool:
    return isinstance(v, (dict, list, tuple))


def finite_copy(value):
    """Copy of value for strict JSON: NaN and infinities, anything nested deeper than MAX_DEPTH
    and anything past MAX_NODES become None. Tuples become lists."""
    if not _is_container(value):
        return None if isinstance(value, float) and not math.isfinite(value) else value
    root = {} if isinstance(value, dict) else []
    stack = [(value, root, 1)]
    nodes = 1
    while stack:
        src, dst, depth = stack.pop()
        for key, item in _items(src):
            nodes += 1
            if nodes > MAX_NODES:
                out = None
            elif _is_container(item):
                if depth >= MAX_DEPTH:
                    out = None
                else:
                    out = {} if isinstance(item, dict) else []
                    stack.append((item, out, depth + 1))
            elif isinstance(item, float) and not math.isfinite(item):
                out = None
            else:
                out = item
            if isinstance(dst, dict):
                dst[key] = out
            else:
                dst.append(out)
    return root


def check_finite(value) -> None:
    """Raise ValueError for a non-finite float, nesting deeper than MAX_DEPTH or more than
    MAX_NODES values anywhere in value."""
    if not _is_container(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite number")
        return
    stack = [(value, 1)]
    nodes = 1
    while stack:
        src, depth = stack.pop()
        for _, item in _items(src):
            nodes += 1
            if nodes > MAX_NODES:
                raise ValueError("too many values")
            if _is_container(item):
                if depth >= MAX_DEPTH:
                    raise ValueError("too deeply nested")
                stack.append((item, depth + 1))
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("non-finite number")
