"""Per-source options in the config: strict validation and the token_env / key_path allowlist."""

from pathlib import Path

import pytest

from fafo.config import ConfigError, load_config

SAMPLE = """
show_args = ["exec"]

[sources.claude]
sessions_dir = "~/sessions-example"

[sources.ollama]
url = "http://ollama.example.invalid:11434"
admission_log = "/var/log/example-admission.log"

[sources.runners]
repos = ["example-org/example-repo"]
token_env = "FAFO_GH_TOKEN"

[[sources.runners.hosts]]
name = "runner-1"
ssh = "runner-1.example.invalid"
key_path = "/home/example/.ssh/id_example"
repos = ["example-org/example-repo"]

[[sources.runners.hosts]]
name = "runner-2"

[sources.copilot]
repos = ["example-org/example-repo"]
session_paths = ["dynamic/agents/example"]
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "fafo.toml"
    p.write_text(text, encoding="utf-8")
    return p


def err(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, text))
    return str(ei.value)


def test_sample_parses_into_options(tmp_path):
    cfg = load_config(write(tmp_path, SAMPLE))
    assert cfg.sources["claude"].options == {"sessions_dir": "~/sessions-example"}
    assert cfg.sources["ollama"].options["url"] == "http://ollama.example.invalid:11434"
    r = cfg.sources["runners"].options
    assert r["repos"] == ["example-org/example-repo"]
    assert r["token_env"] == "FAFO_GH_TOKEN"
    assert [h["name"] for h in r["hosts"]] == ["runner-1", "runner-2"]
    assert r["hosts"][0]["key_path"] == "/home/example/.ssh/id_example"
    assert cfg.sources["copilot"].options["session_paths"] == ["dynamic/agents/example"]


def test_defaults_have_empty_options():
    assert load_config(None).sources["runners"].options == {}


@pytest.mark.parametrize(
    "text",
    [
        "[sources.claude]\nbogus = 1\n",
        "[sources.host]\nsessions_dir = \"x\"\n",
        "[sources.ollama]\nurl = \"http://a.invalid\"\nextra = 1\n",
        "[sources.runners]\nrepos = []\nextra = 1\n",
        "[[sources.runners.hosts]]\nname = \"a\"\nextra = 1\n",
        "[sources.copilot]\nextra = 1\n",
    ],
)
def test_unknown_option_keys_rejected(tmp_path, text):
    assert "unknown key" in err(tmp_path, text)


@pytest.mark.parametrize(
    "text,where",
    [
        ("[sources.claude]\nsessions_dir = 5\n", "sources.claude.sessions_dir"),
        ("[sources.claude]\nsessions_dir = \"a\\nb\"\n", "sources.claude.sessions_dir"),
        ("[sources.claude]\nsessions_dir = \"" + "a" * 5000 + "\"\n", "sources.claude.sessions_dir"),
        ("[sources.ollama]\nurl = 5\n", "sources.ollama.url"),
        ("[sources.ollama]\nurl = \"" + "a" * 3000 + "\"\n", "sources.ollama.url"),
        ("[sources.runners]\nrepos = \"x\"\n", "sources.runners.repos"),
        ("[sources.runners]\nrepos = [1]\n", "sources.runners.repos"),
        ("[sources.runners]\nrepos = [\"no-slash\"]\n", "sources.runners.repos"),
        ("[sources.runners]\nrepos = [\"a/b/c\"]\n", "sources.runners.repos"),
        ("[sources.runners]\nrepos = [" + ",".join(['"a/b"'] * 101) + "]\n", "sources.runners.repos"),
        ("[sources.runners]\nhosts = \"x\"\n", "sources.runners.hosts"),
        ("[[sources.runners.hosts]]\nssh = \"a\"\n", "sources.runners.hosts"),
        ("[[sources.runners.hosts]]\nname = \"a\"\nssh = \"-oProxy\"\n", "sources.runners.hosts"),
        ("[[sources.runners.hosts]]\nname = \"a\"\nssh = \"a b\"\n", "sources.runners.hosts"),
        ("[[sources.runners.hosts]]\nname = \"a\"\nrepos = [\"zz\"]\n", "sources.runners.hosts"),
        ("[sources.copilot]\nsession_paths = [1]\n", "sources.copilot.session_paths"),
        ("[sources.copilot]\nrepos = [\"bad\"]\n", "sources.copilot.repos"),
    ],
)
def test_option_type_and_bounds_checked(tmp_path, text, where):
    msg = err(tmp_path, text)
    assert where in msg


def test_too_many_hosts_rejected(tmp_path):
    body = "".join(f'[[sources.runners.hosts]]\nname = "h{i}"\n' for i in range(40))
    assert "sources.runners.hosts" in err(tmp_path, body)


def test_duplicate_host_names_rejected(tmp_path):
    body = '[[sources.runners.hosts]]\nname = "a"\n[[sources.runners.hosts]]\nname = "a"\n'
    assert "duplicate" in err(tmp_path, body)


# --- the token_env / key_path allowlist, both ways -------------------------------------------


def test_token_env_name_is_accepted(tmp_path):
    cfg = load_config(write(tmp_path, '[sources.runners]\ntoken_env = "MY_GH_TOKEN_1"\n'))
    assert cfg.sources["runners"].options["token_env"] == "MY_GH_TOKEN_1"


@pytest.mark.parametrize(
    "value",
    [
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2,  # looks like a real token (lowercase, digits)
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
        "lower_case",
        "1STARTS_WITH_DIGIT",
        "HAS SPACE",
        "A" * 65,
        "",
    ],
)
def test_secret_looking_value_under_token_env_rejected_without_echo(tmp_path, value):
    msg = err(tmp_path, f'[sources.runners]\ntoken_env = "{value}"\n')
    assert "token_env" in msg
    if value:
        assert value not in msg


def test_token_env_wrong_type_rejected(tmp_path):
    assert "token_env" in err(tmp_path, "[sources.runners]\ntoken_env = 5\n")


def test_plain_token_key_still_rejected(tmp_path):
    msg = err(tmp_path, '[sources.runners]\ntoken = "x"\n')
    assert "secret-looking key" in msg
    assert '"x"' not in msg


@pytest.mark.parametrize(
    "text",
    [
        'token = "x"\n',
        '[sources.runners]\npassword = "x"\n',
        '[sources.ollama]\napi_key = "x"\n',
        '[sources.claude]\ntoken_env = "FAFO_X"\n',  # allowlist is per location
        '[sources.ollama]\nkey_path = "/x"\n',
        '[sources.host]\ntoken_env = "FAFO_X"\n',
        '[[sources.runners.hosts]]\nname = "a"\npassword = "x"\n',
        '[[sources.runners.hosts]]\nname = "a"\ntoken_env = "FAFO_X"\n',
        '[sources.runners]\nkey_path = "/x"\n',
        '[aliases]\n"a.invalid" = "x"\n[sources.runners]\nsecret_env = "FAFO_X"\n',
    ],
)
def test_secrets_still_rejected_everywhere_else(tmp_path, text):
    assert "secret-looking key" in err(tmp_path, text)


def test_key_path_plain_path_accepted(tmp_path):
    body = '[[sources.runners.hosts]]\nname = "a"\nssh = "a"\nkey_path = "/home/e/.ssh/id_x"\n'
    cfg = load_config(write(tmp_path, body))
    assert cfg.sources["runners"].options["hosts"][0]["key_path"] == "/home/e/.ssh/id_x"


def test_key_path_pem_body_rejected_without_echo(tmp_path):
    marker = "-----" + "BEGIN"
    body = f'[[sources.runners.hosts]]\nname = "a"\nkey_path = "{marker} PRIVATE KEY-----"\n'
    msg = err(tmp_path, body)
    assert "key_path" in msg
    assert "PRIVATE" not in msg


def test_key_path_newline_and_length_rejected(tmp_path):
    nl = chr(92) + "n"  # a TOML escape for a newline
    assert "key_path" in err(
        tmp_path, f'[[sources.runners.hosts]]\nname = "a"\nkey_path = "/a{nl}b"\n'
    )
    long_ = "/" + "a" * 4096
    assert "key_path" in err(
        tmp_path, f'[[sources.runners.hosts]]\nname = "a"\nkey_path = "{long_}"\n'
    )


def test_key_path_wrong_type_rejected(tmp_path):
    assert "key_path" in err(tmp_path, '[[sources.runners.hosts]]\nname = "a"\nkey_path = 5\n')


def test_key_file_is_never_read(tmp_path, monkeypatch):
    key = tmp_path / "id_x"
    key.write_text("not read", encoding="utf-8")
    opened: list[str] = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    body = f'[[sources.runners.hosts]]\nname = "a"\nkey_path = "{key}"\n'
    load_config(write(tmp_path, body))
    assert str(key) not in opened


def test_table_without_enabled_key_is_enabled(tmp_path):
    cfg = load_config(write(tmp_path, '[sources.ollama]\nurl = "http://a.invalid"\n'))
    assert cfg.sources["ollama"].enabled is True
    assert cfg.sources["runners"].enabled is False  # not mentioned: stays at its default


EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def uncommented_example() -> str:
    """config.example.toml with the leading '# ' removed inside BEGIN/END example blocks."""
    out: list[str] = []
    inside = False
    blocks = 0
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line == "#@ BEGIN example":
            inside = True
            blocks += 1
        elif line == "#@ END example":
            inside = False
        elif inside and line.startswith("# "):
            out.append(line[2:])
        else:
            out.append(line)
    assert not inside
    assert blocks == 3  # ollama, runners, copilot: something was collected
    return "\n".join(out) + "\n"


def test_example_file_loads_as_shipped(tmp_path):
    cfg = load_config(EXAMPLE)
    assert {n for n, s in cfg.sources.items() if s.enabled} == {"host", "claude", "codex", "gemini"}


def test_example_file_with_every_example_block_loads_and_builds(tmp_path):
    from fafo.registry import build

    class Stub:
        def __init__(self, name):
            self.name = name

        def collect(self, ctx):
            raise AssertionError("not called")

    cfg = load_config(write(tmp_path, uncommented_example()))
    assert all(s.enabled for s in cfg.sources.values())
    assert cfg.sources["ollama"].options["url"].startswith("http://")
    assert cfg.sources["runners"].options["hosts"][0]["name"] == "runner-a"
    assert cfg.sources["copilot"].options["repos"] == ["owner/name"]
    for name in ("runners", "copilot"):
        assert cfg.sources[name].options["token_file"] == "/run/user/1000/fafo-github-token"
    factories = {n: (lambda s, c, e, n=n: Stub(n)) for n in cfg.sources}
    res = build(cfg, {"FAFO_GITHUB_TOKEN": "x"}, factories=factories)
    assert sorted(s.name for s in res.sources) == sorted(cfg.sources)


def test_example_file_with_real_factories_builds(tmp_path):
    from fafo.registry import build

    cfg = load_config(write(tmp_path, uncommented_example()))
    res = build(cfg, {"FAFO_GITHUB_TOKEN": "x"})
    assert sorted(s.name for s in res.sources) == sorted(cfg.sources)


def test_example_file_has_no_secret_values():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "-----" + "BEGIN" not in text
    assert "ghp_" not in text and "github_pat_" not in text
    assert chr(0x2014) not in text


REPO_CASES = [
    ("owner/name", True),
    ("a/b", True),
    ("my-org/my.repo_1", True),
    ("..x/y", False),
    ("../x", False),
    ("owner/..", False),
    (".", False),
    ("owner/.", False),
    ("./name", False),
    ("a/b/c", False),
    ("", False),
    ("noslash", False),
    ("a/" + "b" * 101, False),
    ("a" * 200 + "/b", False),
    ("a b/c", False),
    ("a/b\n", False),
    (None, False),
    (5, False),
]


@pytest.mark.parametrize("repo,ok", REPO_CASES)
def test_config_and_ghclient_validators_agree(repo, ok):
    from fafo.ghapi import GhArgError, GhClient
    from fafo.repospec import valid_repo

    assert valid_repo(repo) is ok
    try:
        GhClient._check_repo(repo)
        client_ok = True
    except GhArgError:
        client_ok = False
    assert client_ok is ok


@pytest.mark.parametrize("repo", [r for r, ok in REPO_CASES if not ok and isinstance(r, str)])
@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_bad_repo_rejected_at_load_naming_option_only(tmp_path, source, repo):
    import json

    text = f"[sources.{source}]\nrepos = [{json.dumps(repo)}]\n"
    msg = err(tmp_path, text)
    assert f"sources.{source}.repos" in msg
    if len(repo) > 3:
        assert repo not in msg


# --- token_file: an exact-location path key, never read by the config -------------------------


@pytest.mark.parametrize("source", ["runners", "copilot"])
def test_token_file_absolute_path_accepted_and_not_read(tmp_path, source):
    missing = tmp_path / "does-not-exist-token"
    cfg = load_config(write(tmp_path, f'[sources.{source}]\ntoken_file = "{missing}"\n'))
    assert cfg.sources[source].options["token_file"] == str(missing)


def test_token_file_default_is_unset_and_may_pair_with_token_env(tmp_path):
    cfg = load_config(write(tmp_path, '[sources.runners]\ntoken_env = "FAFO_GH_TOKEN"\n'))
    assert "token_file" not in cfg.sources["runners"].options
    body = '[sources.copilot]\ntoken_env = "FAFO_GH_TOKEN"\ntoken_file = "/run/example/gh-token"\n'
    opts = load_config(write(tmp_path, body)).sources["copilot"].options
    assert opts["token_env"] == "FAFO_GH_TOKEN" and opts["token_file"] == "/run/example/gh-token"


@pytest.mark.parametrize(
    "value",
    [
        "relative/token",
        "~/token",
        "justaname",
        "/a" + chr(10) + "b",
        "/a" + chr(0) + "b",
        "/a" + chr(127),
        "/" + "a" * 4096,
        "-----" + "BEGIN PRIVATE KEY-----",
        "/x/-----" + "BEGIN/y",
        "",
    ],
)
def test_token_file_bad_values_rejected_without_echo(tmp_path, value):
    import json

    literal = json.dumps(value).replace(chr(127), "\\u007f")
    msg = err(tmp_path, f"[sources.runners]\ntoken_file = {literal}\n")
    assert "token_file" in msg
    assert not value or value not in msg


def test_token_file_wrong_type_rejected(tmp_path):
    assert "token_file" in err(tmp_path, "[sources.runners]\ntoken_file = 5\n")


@pytest.mark.parametrize(
    "body",
    [
        '[sources.ollama]\ntoken_file = "/x"\n',
        '[sources.claude]\ntoken_file = "/x"\n',
        '[[sources.runners.hosts]]\nname = "a"\ntoken_file = "/x"\n',
        '[aliases]\ntoken_file = "x"\n',
    ],
)
def test_token_file_only_allowed_at_the_exact_locations(tmp_path, body):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body))


OLLAMA_TWO = """
[sources.ollama]
interval_s = 5

[[sources.ollama.servers]]
name = "gpu-box"
url = "http://gpu.example.invalid:11434"
admission_log = "/var/log/example-admission.log"

[[sources.ollama.servers]]
name = "laptop.local"
url = "https://laptop.example.invalid:11434"
"""


def test_ollama_servers_list_parses(tmp_path):
    o = load_config(write(tmp_path, OLLAMA_TWO)).sources["ollama"].options
    assert [s["name"] for s in o["servers"]] == ["gpu-box", "laptop.local"]
    assert o["servers"][0]["admission_log"] == "/var/log/example-admission.log"
    assert o["servers"][1]["url"] == "https://laptop.example.invalid:11434"


def test_ollama_old_single_url_still_loads(tmp_path):
    o = load_config(write(tmp_path, SAMPLE)).sources["ollama"].options
    assert o["url"] == "http://ollama.example.invalid:11434" and "servers" not in o


def many(n):
    return "[sources.ollama]\n" + "".join(
        f'[[sources.ollama.servers]]\nname = "s{i}"\nurl = "http://s{i}.example.invalid"\n'
        for i in range(n)
    )


def test_ollama_eight_servers_ok_nine_refused(tmp_path):
    assert len(load_config(write(tmp_path, many(8))).sources["ollama"].options["servers"]) == 8
    assert "sources.ollama.servers" in err(tmp_path, many(9))


@pytest.mark.parametrize(
    "body",
    [
        (
            '[[sources.ollama.servers]]\nname = "a"\nurl = "http://x.invalid"\n'
            '[[sources.ollama.servers]]\nname = "a"\nurl = "http://y.invalid"\n'
        ),
        (
            '[[sources.ollama.servers]]\nname = "A"\nurl = "http://x.invalid"\n'
            '[[sources.ollama.servers]]\nname = "a"\nurl = "http://y.invalid"\n'
        ),
        '[[sources.ollama.servers]]\nname = "bad name"\nurl = "http://x.invalid"\n',
        '[[sources.ollama.servers]]\nname = "' + "n" * 33 + '"\nurl = "http://x.invalid"\n',
        '[[sources.ollama.servers]]\nname = ""\nurl = "http://x.invalid"\n',
        '[[sources.ollama.servers]]\nurl = "http://x.invalid"\n',
        '[[sources.ollama.servers]]\nname = "a"\n',
        '[[sources.ollama.servers]]\nname = "a"\nurl = "http://x.invalid"\nextra = 1\n',
        '[[sources.ollama.servers]]\nname = "a"\nurl = 5\n',
        'admission_log = "/var/log/x.log"\n[[sources.ollama.servers]]\nname = "a"\nurl = "http://x.invalid"\n',
        'url = "http://z.invalid"\n[[sources.ollama.servers]]\nname = "a"\nurl = "http://x.invalid"\n',
    ],
)
def test_ollama_servers_bad_forms_name_the_option(tmp_path, body):
    assert "sources.ollama" in err(tmp_path, "[sources.ollama]\n" + body)


def test_example_servers_block_loads_and_builds(tmp_path):
    from fafo.registry import build_sources

    out, inside = [], False
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line == "#@ BEGIN example-servers":
            inside = True
        elif line == "#@ END example-servers":
            inside = False
        elif inside and line.startswith("# "):
            out.append(line[2:])
    assert out  # something was collected
    cfg = load_config(write(tmp_path, "\n".join(out) + "\n"))
    assert [s["name"] for s in cfg.sources["ollama"].options["servers"]] == ["gpu-box", "laptop"]
    src = {s.name: s for s in build_sources(cfg, {})}["ollama"]
    assert [s.name for s in src.servers] == ["gpu-box", "laptop"]
