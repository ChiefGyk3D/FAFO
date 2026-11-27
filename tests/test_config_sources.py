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
