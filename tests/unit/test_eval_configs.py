import pytest

from pkos.eval import systems
from pkos.eval.configs import ConfigError, available, load_config, resolve


def cfg(tmp_path, name, body):
    p = tmp_path / f"{name}.toml"
    p.write_text(body)
    return p


def test_shipped_configs_load():
    shipped = available()
    assert {"null", "oracle"} <= set(shipped)
    assert shipped["oracle"].diagnostic and not shipped["null"].diagnostic


def test_name_must_match_file(tmp_path):
    with pytest.raises(ConfigError, match="must equal the file name"):
        load_config(cfg(tmp_path, "a", 'name = "b"\n[system]\nkind = "null"\n'))


def test_unknown_kind(tmp_path):
    with pytest.raises(ConfigError, match="kind"):
        load_config(cfg(tmp_path, "a", 'name = "a"\n[system]\nkind = "magic"\n'))


def test_oracle_must_be_diagnostic(tmp_path):
    c = load_config(cfg(tmp_path, "o", 'name = "o"\n[system]\nkind = "oracle"\n'))
    with pytest.raises(ConfigError, match="diagnostic"):
        systems.build(c)


def test_retrieval_not_available_yet(tmp_path):
    c = load_config(cfg(tmp_path, "r", 'name = "r"\n[system]\nkind = "retrieval"\n'))
    with pytest.raises(ConfigError, match="step 4"):
        systems.build(c)


def test_config_hash_tracks_file_content(tmp_path):
    a = load_config(cfg(tmp_path, "n", 'name = "n"\n[system]\nkind = "null"\n'))
    b = load_config(cfg(tmp_path, "n", 'name = "n"\n# tuned\n[system]\nkind = "null"\n'))
    assert a.sha256 != b.sha256


def test_resolve_unknown_lists_available():
    with pytest.raises(ConfigError, match="available: .*null"):
        resolve(["nope"])
