from gridbrief.config import Settings


def test_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.iso == "ERCOT"
    assert settings.retrieval_backend == "pgvector"
    assert settings.automatic_refresh is True

