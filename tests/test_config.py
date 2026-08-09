from gridbrief.config import Settings


def test_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.iso == "ERCOT"
    assert settings.retrieval_backend == "pgvector"
    assert settings.automatic_refresh is True
    assert settings.raw_item_retention_days == 14
    assert settings.timeseries_retention_days == 30
    assert settings.document_retention_days == 30
    assert settings.edition_retention_days == 90
    assert settings.timeseries_retention_days >= 8
