from pkos.config import Settings


def test_blank_api_key_means_source_disabled(monkeypatch):
    monkeypatch.setenv("PKOS_GRANOLA_API_KEY", "")
    assert Settings().granola_api_key is None


def test_api_key_is_secret(monkeypatch):
    monkeypatch.setenv("PKOS_GRANOLA_API_KEY", "grn_configTestKey0123456789")
    s = Settings()
    assert s.granola_api_key.get_secret_value() == "grn_configTestKey0123456789"
    assert "grn_configTestKey0123456789" not in repr(s)
