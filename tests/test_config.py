import io
import tarfile
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from silvercare.config import DataSourceSettings, Settings
from silvercare.data import download_aihub


def test_shareable_environment_template_contains_no_connection_or_resource_values():
    path = Path(__file__).resolve().parents[1] / ".env.example"
    values = dotenv_values(path)
    safe_defaults = {
        "APP_ENV": "development",
        "PUBLIC_ORIGIN": "http://127.0.0.1:8000",
        "RETRIEVAL_MODE": "keyword",
        "EMBEDDING_DIMENSIONS": "512",
        "REWRITE_QUERIES": "false",
        "SEMANTIC_RANKING": "false",
        "PREFER_OFFICIAL_REFERENCES": "false",
        "NONCOMMERCIAL_DATA_USE": "false",
        "CONTEXTUAL_RETRIEVAL": "false",
        "RATE_LIMIT_PER_MINUTE": "12",
        "MAX_CONCURRENT_REQUESTS": "4",
        "TRUSTED_PROXY": "false",
    }
    assert values
    for key, value in values.items():
        assert value == safe_defaults.get(key, ""), f"{key} must not contain connection data"
    expected = (
        {name.upper() for name in DataSourceSettings.model_fields}
        | {
            "KDCA_HEALTH_API_KEY" if name == "kdca_health_api_key" else name.upper()
            for name in Settings.model_fields
        }
        | {"AIHUB_ID", "AIHUB_PASSWORD", "DATA_GO_KR_ID", "DATA_GO_KR_PASSWORD"}
    )
    assert set(values) == expected


@pytest.fixture(autouse=True)
def clear_source_env(monkeypatch):
    for key in (
        "AIHUB_API_KEY",
        "WELFARE_API_KEY",
        "NHIS_LTC_API_KEY",
        "WELFARE_CENTRAL_API_KEY",
        "WELFARE_LOCAL_API_KEY",
        "NHIS_LTC_SEARCH_API_KEY",
        "NHIS_LTC_DETAIL_API_KEY",
        "API_KEY",
        "KDCA_HEALTH_API_KEY",
        "NHIS_HEALTH_INFO_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_source_keys_are_separate_and_masked(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "AIHUB_API_KEY=synthetic-aihub-key\n"
        "WELFARE_API_KEY=synthetic-welfare-key\n"
        "NHIS_LTC_API_KEY=synthetic-nhis-key\n"
        "API_KEY=ambiguous-key\n"
        "ID=unused-login\n"
    )
    config = DataSourceSettings(_env_file=env)
    assert config.aihub_api_key.get_secret_value() == "synthetic-aihub-key"
    assert config.welfare_central_api_key.get_secret_value() == "synthetic-welfare-key"
    assert config.welfare_local_api_key.get_secret_value() == "synthetic-welfare-key"
    assert config.nhis_ltc_search_api_key.get_secret_value() == "synthetic-nhis-key"
    assert config.nhis_ltc_detail_api_key.get_secret_value() == "synthetic-nhis-key"
    for key in ("synthetic-aihub-key", "synthetic-welfare-key", "synthetic-nhis-key"):
        assert key not in repr(config)
        assert key not in config.model_dump_json()


def test_kdca_approved_key_alias_is_masked_and_dedicated_name_takes_precedence(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NHIS_HEALTH_INFO_API_KEY=synthetic-approved\n")
    config = Settings(_env_file=env)
    assert config.kdca_health_api_key.get_secret_value() == "synthetic-approved"
    assert "synthetic-approved" not in repr(config) + config.model_dump_json()
    env.write_text("NHIS_HEALTH_INFO_API_KEY=legacy\nKDCA_HEALTH_API_KEY=dedicated\n")
    assert Settings(_env_file=env).kdca_health_api_key.get_secret_value() == "dedicated"


def test_source_environment_overrides_only_its_own_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "AIHUB_API_KEY=file-aihub\nWELFARE_API_KEY=file-welfare\nNHIS_LTC_API_KEY=file-nhis\n"
    )
    monkeypatch.setenv("WELFARE_API_KEY", "environment-welfare")
    monkeypatch.setenv("API_KEY", "unrelated-generic")
    config = DataSourceSettings(_env_file=env)
    assert config.aihub_api_key.get_secret_value() == "file-aihub"
    assert config.welfare_central_api_key.get_secret_value() == "environment-welfare"
    assert config.welfare_local_api_key.get_secret_value() == "environment-welfare"
    assert config.nhis_ltc_search_api_key.get_secret_value() == "file-nhis"


def test_source_keys_preserve_encoding_and_ignore_webapp_guards(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "APP_ENV=production\n"
        "AIHUB_API_KEY=synthetic-aihub\n"
        "WELFARE_API_KEY=synthetic%2Bencoded%2Fkey%3D\n"
    )
    config = DataSourceSettings(_env_file=env)
    assert config.welfare_central_api_key.get_secret_value() == "synthetic%2Bencoded%2Fkey%3D"
    assert "/NationalWelfareInformationsV001" in config.welfare_central_api_endpoint
    assert "/LocalGovernmentWelfareInformations" in config.welfare_local_api_endpoint
    assert "/searchLtcInsttService02" in config.nhis_ltc_search_api_endpoint
    assert "/getLtcInsttDetailInfoService02" in config.nhis_ltc_detail_api_endpoint


def test_dedicated_keys_override_legacy_shared_names_without_cross_service_shadowing(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "WELFARE_API_KEY=legacy-welfare\n"
        "NHIS_LTC_API_KEY=legacy-nhis\n"
        "WELFARE_CENTRAL_API_KEY=central-only\n"
        "WELFARE_LOCAL_API_KEY=local-only\n"
        "NHIS_LTC_SEARCH_API_KEY=search-only\n"
        "NHIS_LTC_DETAIL_API_KEY=detail-only\n"
    )
    config = DataSourceSettings(_env_file=env)
    assert config.welfare_central_api_key.get_secret_value() == "central-only"
    assert config.welfare_local_api_key.get_secret_value() == "local-only"
    assert config.nhis_ltc_search_api_key.get_secret_value() == "search-only"
    assert config.nhis_ltc_detail_api_key.get_secret_value() == "detail-only"
    for key in ("central-only", "local-only", "search-only", "detail-only"):
        assert key not in repr(config)
        assert key not in config.model_dump_json()


def test_dedicated_environment_override_changes_only_its_service(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "WELFARE_CENTRAL_API_KEY=central\n"
        "WELFARE_LOCAL_API_KEY=local\n"
        "NHIS_LTC_SEARCH_API_KEY=search\n"
        "NHIS_LTC_DETAIL_API_KEY=detail\n"
    )
    monkeypatch.setenv("WELFARE_LOCAL_API_KEY", "environment-local")
    config = DataSourceSettings(_env_file=env)
    assert config.welfare_local_api_key.get_secret_value() == "environment-local"
    assert config.welfare_central_api_key.get_secret_value() == "central"
    assert config.nhis_ltc_detail_api_key.get_secret_value() == "detail"


async def test_aihub_download_uses_only_aihub_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "AIHUB_API_KEY=synthetic-aihub\n"
        "WELFARE_API_KEY=synthetic-welfare\n"
        "NHIS_LTC_API_KEY=synthetic-nhis\n"
        "API_KEY=ambiguous-key\n"
    )
    requests = []
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("labels.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"test"))
    content = archive.getvalue()

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.aihub.or.kr"
        assert request.headers["apikey"] == "synthetic-aihub"
        assert "synthetic-welfare" not in str(request.headers)
        assert "synthetic-nhis" not in str(request.headers)
        assert request.url.params["fileSn"] == "123"
        return httpx.Response(200, content=content, headers={"content-type": "application/x-tar"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("silvercare.data.httpx.AsyncClient", lambda **kwargs: client)
    output = tmp_path / "download.tar"
    size = await download_aihub("107", "123", output, 1)
    assert size == len(content)
    assert output.read_bytes() == content
    assert len(requests) == 1


async def test_generic_key_cannot_be_sent_to_aihub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("API_KEY=public-data-key\nWELFARE_API_KEY=synthetic-welfare\n")
    monkeypatch.setattr(
        "silvercare.data.httpx.AsyncClient",
        lambda **kwargs: pytest.fail("No network call without a namespaced AI Hub key"),
    )
    with pytest.raises(ValueError, match="AIHUB_API_KEY is required"):
        await download_aihub("107", "123", tmp_path / "download.tar", 1)
    assert not (tmp_path / "download.tar").exists()
