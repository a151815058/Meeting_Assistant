import pytest
from sqlalchemy.engine import make_url

from app.config import ProductionConfig, site_verification_token, supabase_database_url


@pytest.mark.parametrize("pasted", [
    "N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0",
    '  N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0\n',
    'content="N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0"',
    '<meta name="google-site-verification" content="N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0" />',
    '"N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0"',
])
def test_site_verification_token_accepts_what_people_paste(pasted):
    """TC-56: GOOGLE_SITE_VERIFICATION 貼上純值、content="..."、或整個 meta 標籤皆取出正確的驗證值。"""
    assert site_verification_token(pasted) == "N_Z5wvfb3qxNXrQcgwDxzi5SpY58fCmgJ3DC77Jgwd0"
    assert site_verification_token("") == ""


def test_supabase_url_is_unset_without_host():
    """TC-50: 未設定 SUPABASE_DB_HOST 時不使用 Supabase，沿用 DATABASE_URL。"""
    assert supabase_database_url({}) is None
    assert supabase_database_url({"SUPABASE_DB_HOST": "  ", "SUPABASE_DB_PASSWORD": "x"}) is None


def test_supabase_url_built_from_fields_with_escaped_password():
    """TC-50: 由 SUPABASE_DB_* 組出連線字串，密碼特殊字元自動跳脫，預設 sslmode=require。"""
    url = supabase_database_url({
        "SUPABASE_DB_HOST": "aws-0-ap-northeast-1.pooler.supabase.com",
        "SUPABASE_DB_USER": "postgres.abcdefgh",
        "SUPABASE_DB_PASSWORD": "p@ss:w/rd%#?",
    })
    parsed = make_url(url)
    assert parsed.drivername == "postgresql+psycopg2"
    assert parsed.host == "aws-0-ap-northeast-1.pooler.supabase.com"
    assert parsed.port == 5432
    assert parsed.database == "postgres"
    assert parsed.username == "postgres.abcdefgh"
    assert parsed.password == "p@ss:w/rd%#?"
    assert parsed.query == {"sslmode": "require"}


def test_supabase_url_honours_port_name_and_sslmode():
    """TC-50: 可自訂連接埠、資料庫名稱與 sslmode。"""
    parsed = make_url(supabase_database_url({
        "SUPABASE_DB_HOST": "db.abcdefgh.supabase.co", "SUPABASE_DB_USER": "postgres",
        "SUPABASE_DB_PASSWORD": "pw", "SUPABASE_DB_PORT": "6543", "SUPABASE_DB_NAME": "meetings",
        "SUPABASE_DB_SSLMODE": "verify-full",
    }))
    assert (parsed.port, parsed.database, parsed.query["sslmode"]) == (6543, "meetings", "verify-full")


def test_production_refuses_unencrypted_supabase_connection(monkeypatch):
    """TC-50: 正式環境連線 Supabase 若 sslmode 低於 require，應用程式拒絕啟動。"""
    from app import create_app

    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    monkeypatch.setattr(ProductionConfig, "SUPABASE_ENABLED", True)
    monkeypatch.setattr(ProductionConfig, "SUPABASE_DB_SSLMODE", "disable")
    with pytest.raises(RuntimeError, match="SUPABASE_DB_SSLMODE"):
        create_app("production")

    monkeypatch.setattr(ProductionConfig, "SUPABASE_DB_SSLMODE", "require")
    create_app("production")
