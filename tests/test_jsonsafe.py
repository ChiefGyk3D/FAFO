"""Walks over untrusted nested data are iterative: they must survive a tiny recursion limit."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_render_table import NOW, golden_snapshot
from test_store import make_snapshot

from fafo import jsonsafe
from fafo.render.table import format_json
from fafo.store import read_snapshot, write_snapshot

DEEP = 100_000
LOW_LIMIT = 200


@contextmanager
def low_recursion_limit():
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(LOW_LIMIT)
    try:
        yield
    finally:
        sys.setrecursionlimit(old)


def nest_list(n: int, leaf=1.5):
    v = leaf
    for _ in range(n):
        v = [v]
    return v


def nest_dict(n: int, leaf=1.5):
    v = leaf
    for _ in range(n):
        v = {"a": v}
    return v


def text_list(n: int) -> str:
    return "[" * n + "1.5" + "]" * n


def text_dict(n: int) -> str:
    return '{"a":' * n + "1.5" + "}" * n


def strict(text):
    def refuse(name):
        raise AssertionError(name)

    return json.loads(text, parse_constant=refuse)


def depth_of(doc) -> int:
    d, stack = 0, [(doc, 1)]
    while stack:
        v, n = stack.pop()
        d = max(d, n)
        if isinstance(v, dict):
            stack += [(x, n + 1) for x in v.values()]
        elif isinstance(v, list):
            stack += [(x, n + 1) for x in v]
    return d


@pytest.mark.parametrize("builder", [nest_list, nest_dict])
def test_finite_copy_deep_input_low_limit(builder):
    deep = builder(DEEP)
    with low_recursion_limit():
        out = jsonsafe.finite_copy(deep)
    assert depth_of(out) <= jsonsafe.MAX_DEPTH + 1


def test_finite_copy_replaces_nonfinite_and_keeps_shape():
    src = {"a": [float("nan"), 1, "x", None, True], "b": {"c": float("inf")}, "t": (1, 2)}
    assert jsonsafe.finite_copy(src) == {"a": [None, 1, "x", None, True], "b": {"c": None},
                                         "t": [1, 2]}


def test_finite_copy_node_cap():
    big = list(range(jsonsafe.MAX_NODES + 50))
    out = jsonsafe.finite_copy(big)
    assert len(out) == len(big)
    assert out[0] == 0 and out[-1] is None
    assert sum(1 for x in out if x is not None) <= jsonsafe.MAX_NODES


def test_finite_copy_cycle_terminates():
    a: list = []
    a.append(a)
    a.append(a)
    assert depth_of(jsonsafe.finite_copy(a)) <= jsonsafe.MAX_DEPTH + 1


def test_check_finite_rejects_and_accepts():
    jsonsafe.check_finite({"a": [1, 2.5, {"b": "x"}]})
    for bad in ({"a": float("nan")}, [float("-inf")], nest_list(jsonsafe.MAX_DEPTH + 5)):
        with pytest.raises(ValueError):
            jsonsafe.check_finite(bad)
    with pytest.raises(ValueError):
        jsonsafe.check_finite(list(range(jsonsafe.MAX_NODES + 1)))


@pytest.mark.parametrize("builder", [nest_list, nest_dict])
def test_check_finite_deep_input_low_limit(builder):
    deep = builder(DEEP // 5)  # below the node cap, so only the depth cap can reject it
    with low_recursion_limit(), pytest.raises(ValueError):
        jsonsafe.check_finite(deep)


@pytest.mark.parametrize("where", ["host", "quota", "gpu"])
@pytest.mark.parametrize("builder", [nest_list, nest_dict])
def test_format_json_deep_free_form_values_low_limit(where, builder):
    snap = golden_snapshot()
    deep = builder(DEEP)
    if where == "gpu":
        snap.llm.gpu = {"deep": deep, "util": 78.0}
    else:
        getattr(snap, where)["deep"] = deep
    with low_recursion_limit():
        out = format_json(snap, NOW)
    doc = strict(out)
    target = doc["llm"]["gpu"] if where == "gpu" else doc[where]
    assert depth_of(target["deep"]) <= jsonsafe.MAX_DEPTH + 1
    assert target["deep"] is not None  # the shallow part survives
    if where == "gpu":
        assert target["util"] == 78.0


@pytest.mark.parametrize("where", ["host", "quota", "gpu"])
@pytest.mark.parametrize("text", [text_list, text_dict])
def test_read_snapshot_deep_values_low_limit(where, text, tmp_path: Path):
    path = tmp_path / "snap.json"
    write_snapshot(path, make_snapshot())
    old = {"host": '"load":4.1', "quota": '"5h":0.41', "gpu": '"util":78'}[where]
    raw = path.read_text(encoding="utf-8")
    assert raw.count(old) == (2 if where == "gpu" else 1)  # gpu is in "llm" and "llms"
    key = old.split(":")[0]
    path.write_text(raw.replace(old, f"{key}:{text(DEEP)}"), encoding="utf-8")
    with low_recursion_limit():
        assert read_snapshot(path) is None
