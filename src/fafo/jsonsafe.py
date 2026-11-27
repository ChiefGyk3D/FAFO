"""Iterative walks over untrusted nested data (snapshot host, quota and gpu values).

Nothing here recurses: an explicit stack, a hard depth cap and a node cap, so hostile nesting
can never hit the interpreter recursion limit (which differs between Python versions).
"""

from __future__ import annotations

import dataclasses
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


def _is_dataclass_instance(v) -> bool:
    return dataclasses.is_dataclass(v) and not isinstance(v, type)


def plain_copy(value) -> tuple[object, int]:
    """Strict-JSON plain copy of value (dataclass instances, dicts, lists, tuples, scalars).

    Iterative, with the same MAX_DEPTH and MAX_NODES caps as finite_copy. Returns the copy and
    the number of replaced values. Replaced with None: NaN and infinities, anything nested past
    the depth cap; list and dict entries beyond the node cap are dropped (counted once per container); a
    scalar that is not None, bool, int, float or str is also replaced. Dict entries whose key is not a str are dropped (and counted). Never raises for a bad
    value and never recurses."""
    replaced = 0

    def scalar(v):
        nonlocal replaced
        if v is None or type(v) in (bool, int, str):
            return v
        if isinstance(v, float):
            if math.isfinite(v):
                return v
        elif isinstance(v, (bool, int, str)):
            return v  # subclasses
        replaced += 1
        return None

    def children(src):
        if _is_dataclass_instance(src):
            return [(f.name, getattr(src, f.name, None)) for f in dataclasses.fields(src)]
        return list(_items(src))

    def container(v) -> bool:
        return _is_container(v) or _is_dataclass_instance(v)

    if not container(value):
        return scalar(value), replaced
    root = [] if isinstance(value, (list, tuple)) else {}
    stack = [(value, root, 1)]
    nodes = 1
    while stack:
        src, dst, depth = stack.pop()
        for key, item in children(src):
            if isinstance(dst, dict) and type(key) is not str:
                replaced += 1
                continue
            nodes += 1
            if nodes > MAX_NODES and not _is_dataclass_instance(src):
                # Drop (not null) the overflow: nulls would still count against a reader's
                # node cap. Dataclass fields are bounded by the schema and always kept, so
                # every row stays complete.
                replaced += 1
                break
            elif container(item):
                if depth >= MAX_DEPTH:
                    out = None
                    replaced += 1
                else:
                    out = [] if isinstance(item, (list, tuple)) else {}
                    stack.append((item, out, depth + 1))
            else:
                out = scalar(item)
            if isinstance(dst, dict):
                dst[key] = out
            else:
                dst.append(out)
    return root, replaced
