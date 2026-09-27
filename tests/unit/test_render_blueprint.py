from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

BLUEPRINT = Path(__file__).resolve().parents[2] / "render.yaml"
SECRETS = {"TOKEN_ENCRYPTION_KEY", "SUPABASE_DB_PASSWORD", "GOOGLE_CLIENT_SECRET", "MS_CLIENT_SECRET",
           "ANTHROPIC_API_KEY"}


def _web_service():
    services = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))["services"]
    assert len(services) == 1
    return services[0]


def test_render_blueprint_runs_one_production_instance_behind_one_proxy():
    """TC-52: render.yaml 以 Docker 部署單一 instance（錄音狀態在記憶體）、正式環境設定、信任 1 層 proxy、健康檢查 /healthz。"""
    service = _web_service()
    assert (service["type"], service["runtime"], service["numInstances"]) == ("web", "docker", 1)
    assert service["healthCheckPath"] == "/healthz"
    env = {e["key"]: e for e in service["envVars"]}
    assert env["FLASK_ENV"]["value"] == "production"
    assert env["TRUSTED_PROXY_HOPS"]["value"] == "1"
    assert env["SUPABASE_DB_SSLMODE"]["value"] == "require"
    assert env["SECRET_KEY"] == {"key": "SECRET_KEY", "generateValue": True}


def test_render_blueprint_holds_no_secret_values():
    """TC-52: render.yaml 不含任何機密值，機密一律 sync: false 由 Render 介面輸入。"""
    env = {e["key"]: e for e in _web_service()["envVars"]}
    for key in SECRETS:
        assert env[key] == {"key": key, "sync": False}, key
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in env and "DEV_LOGIN_ENABLED" not in env
