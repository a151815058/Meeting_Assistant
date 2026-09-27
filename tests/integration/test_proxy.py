from flask import jsonify, request

from app import create_app
from app.config import TestingConfig

FORWARDED = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https",
             "X-Forwarded-Host": "meeting-assistant.onrender.com"}


def _app_seeing_request(monkeypatch, hops: int):
    monkeypatch.setattr(TestingConfig, "TRUSTED_PROXY_HOPS", hops)
    application = create_app("testing")

    @application.route("/_seen")
    def seen():
        return jsonify(url=request.url, remote_addr=request.remote_addr)

    return application.test_client()


def test_behind_trusted_proxy_uses_forwarded_scheme_host_and_ip(monkeypatch):
    """TC-52: 設定 TRUSTED_PROXY_HOPS=1（Render）時，採用 proxy 轉送的 https、網域與使用者 IP，OAuth callback 網址為 https。"""
    data = _app_seeing_request(monkeypatch, 1).get("/_seen?code=x", headers=FORWARDED).get_json()
    assert data["url"] == "https://meeting-assistant.onrender.com/_seen?code=x"
    assert data["remote_addr"] == "203.0.113.7"


def test_without_proxy_forwarded_headers_are_ignored(monkeypatch):
    """TC-52: 未設定 proxy（預設 0）時忽略 X-Forwarded-*，避免使用者偽造 IP 或 https。"""
    data = _app_seeing_request(monkeypatch, 0).get("/_seen?code=x", headers=FORWARDED).get_json()
    assert data["url"] == "http://localhost/_seen?code=x"
    assert data["remote_addr"] == "127.0.0.1"
