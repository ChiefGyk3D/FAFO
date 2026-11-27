import json
import subprocess
from pathlib import Path

import pytest

from fafo.adapters.base import Enrichment
from fafo.adapters.safo import MAX_OUTPUT, SafoAdapter, parse_issue_line

FIXTURE = Path(__file__).parent / "fixtures" / "safo_agents_status.json"


def fake(out=None, exc=None):
    calls = []

    def run(argv, timeout_s, env):
        calls.append((list(argv), timeout_s, dict(env)))
        if exc:
            raise exc
        return out

    run.calls = calls
    return run


def test_two_sessions_map_by_id_and_unknown_ignored():
    a = SafoAdapter(["safo-x"], runner=fake(FIXTURE.read_bytes()))
    got = a.enrich(["s1", "s2", "missing"], {})
    assert set(got) == {"s1", "s2"}
    assert got["s1"] == Enrichment("ChiefGyk3D/FAFO#19", "In progress", "FAFO observer", "M")
    assert got["s2"].issue == "ChiefGyk3D/FAFO#12" and got["s2"].effort == "S"
    assert a.health == "ok"


def test_absent_command():
    a = SafoAdapter(["nope"], runner=fake(exc=FileNotFoundError()))
    assert a.enrich(["s1"], {}) == {}
    assert a.health == "absent"


def test_real_missing_binary_is_absent():
    a = SafoAdapter(["/nonexistent/fafo-test-binary"])
    assert a.enrich(["s1"], {}) == {}
    assert a.health == "absent"


def test_timeout_is_stale():
    a = SafoAdapter(["safo"], runner=fake(exc=subprocess.TimeoutExpired("safo", 5)))
    assert a.enrich(["s1"], {}) == {}
    assert a.health == "stale"


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b'{"sessions": 3}', b"[" * 5000 + b"]" * 5000,
     b'{"sessions": [], "x": ' + b"[" * 20 + b"]" * 20 + b"}"],
)
def test_malformed_json_returns_empty(raw):
    a = SafoAdapter(["safo"], runner=fake(raw))
    assert a.enrich(["s1"], {}) == {}
    assert a.health == "absent"


def test_oversized_output_rejected():
    pad = b" " * (MAX_OUTPUT + 1)
    a = SafoAdapter(["safo"], runner=fake(FIXTURE.read_bytes() + pad))
    assert a.enrich(["s1"], {}) == {}
    assert a.health == "absent"


def test_control_chars_and_long_strings_cleaned():
    bad = "a" + chr(27) + "[31m" + chr(0) + "b" + chr(10) + "c"
    row = {"session_id": "s1", "issue": bad, "epic": "e" * 10240, "effort": chr(7)}
    a = SafoAdapter(["safo"], runner=fake(json.dumps({"sessions": [row]}).encode()))
    e = a.enrich(["s1"], {})["s1"]
    assert e.issue == "a[31mbc"
    assert len(e.epic) == 120
    assert e.effort is None


def test_command_run_as_argv_with_minimal_env():
    run = fake(b'{"sessions": []}')
    SafoAdapter(["safo", "x y;z"], timeout_s=2.5, runner=run).enrich([], {})
    argv, timeout_s, env = run.calls[0]
    assert argv == ["safo", "x y;z"] and timeout_s == 2.5
    assert set(env) <= {"PATH", "HOME", "LANG"} and env["LANG"] == "C"


@pytest.mark.parametrize("text", ["issue: ChiefGyk3D/FAFO#12", "x\nissue: a.b/c_d-e#1234567\ny"])
def test_parse_issue_accepts(text):
    assert parse_issue_line(text) in {"ChiefGyk3D/FAFO#12", "a.b/c_d-e#1234567"}


@pytest.mark.parametrize(
    "text",
    [
        "issue: a/b#12345678",
        "issue: a/b#",
        "issue: a/b;rm -rf#1",
        "issue: a/b$(x)#1",
        "issue: a/b#1\n",  # trailing newline is split off; line itself is valid, see below
        "issue: a/b#1 extra",
        " issue: a/b#1",
        "issue: " + "a" * 101 + "/b#1",
        "issue: a/b#1 x",
        "",
    ],
)
def test_parse_issue_rejects(text):
    got = parse_issue_line(text)
    if text == "issue: a/b#1\n":
        assert got == "a/b#1"
    else:
        assert got is None


def test_parse_issue_first_match_only():
    assert parse_issue_line("issue: a/b#1\nissue: c/d#2") == "a/b#1"
    assert parse_issue_line("issue: bad line\nissue: c/d#2") is None


def test_issue_line_fallback_when_command_gives_none():
    raw = json.dumps({"sessions": [{"session_id": "s1", "card_status": "Next"}]}).encode()
    a = SafoAdapter(["safo"], runner=fake(raw))
    got = a.enrich(["s1"], {"s1": "task\nissue: ChiefGyk3D/FAFO#19"})
    assert got["s1"].issue == "ChiefGyk3D/FAFO#19" and got["s1"].card_status == "Next"


def test_same_class_works_as_shit_port():
    raw = json.dumps(
        {"sessions": [{"sid": "w9", "ticket": "o/r#5", "col": "Doing", "group": "G", "size": "XL"}]}
    ).encode()
    names = {"session_id": "sid", "issue": "ticket", "card_status": "col",
             "epic": "group", "effort": "size"}
    a = SafoAdapter(["shit", "status", "--json"], field_names=names, runner=fake(raw), name="shit")
    assert a.name == "shit"
    assert a.enrich(["w9"], {})["w9"] == Enrichment("o/r#5", "Doing", "G", "XL")


def _rows(*rows, **top):
    return json.dumps({"sessions": list(rows), **top}).encode()


_OK = {"session_id": "s1", "issue": "o/r#1"}


@pytest.mark.parametrize(
    "raw",
    [
        _rows(_OK, "not an object"),
        _rows(_OK, 5),
        _rows({"session_id": "s1", "epic": 5}),
        _rows({"session_id": "s1", "effort": ["M"]}),
        _rows({"session_id": "s1", "extra": "x"}),
        _rows({"issue": "o/r#1"}),
        _rows({"session_id": ""}),
        _rows({"session_id": None}),
        _rows({"session_id": 7}),
        _rows({"session_id": "bad id;rm"}),
        _rows({"session_id": "a" * 129}),
        _rows(_OK, {"session_id": "s1", "epic": "dup"}),
        _rows(*[{"session_id": f"s{i}"} for i in range(1001)]),
        b'[{"session_id": "s1"}]',
        b'"text"',
        _rows(_OK, extra_top=1),
        b'{"other": []}',
    ],
)
def test_strict_shape_all_or_nothing(raw):
    a = SafoAdapter(["safo"], runner=fake(raw))
    assert a.enrich(["s1", "s2"], {}) == {}
    assert a.health == "absent"


def test_strict_shape_accepts_nulls_and_limit():
    rows = [{"session_id": f"s{i}", "issue": None, "epic": None} for i in range(1000)]
    a = SafoAdapter(["safo"], runner=fake(_rows(*rows)))
    assert set(a.enrich(["s0", "s999"], {})) == {"s0", "s999"}
    assert a.health == "ok"


def test_strict_shape_uses_configured_field_names():
    names = {"session_id": "sid", "issue": "ticket"}
    good = json.dumps({"sessions": [{"sid": "w1", "ticket": "o/r#2"}]}).encode()
    bad = json.dumps({"sessions": [{"sid": "w1", "issue": "o/r#2"}]}).encode()
    assert SafoAdapter(["x"], field_names=names, runner=fake(good)).enrich(["w1"], {})
    a = SafoAdapter(["x"], field_names=names, runner=fake(bad))
    assert a.enrich(["w1"], {}) == {} and a.health == "absent"
