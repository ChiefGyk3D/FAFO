import fafo


def test_version_is_semver_like():
    parts = fafo.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
