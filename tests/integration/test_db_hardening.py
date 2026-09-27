import pytest
from sqlalchemy import text

from app.extensions import db as _db
from app.models.user import User
from app.security.db_hardening import API_ROLES, existing_api_roles, lock_down_public_schema, public_tables


def _rls_disabled(conn) -> list[str]:
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity"))
    return [r[0] for r in rows]


def _api_privileges(conn) -> list[tuple]:
    rows = conn.execute(text(
        "SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND grantee = ANY(:roles)"), {"roles": list(API_ROLES)})
    return rows.fetchall()


def test_lockdown_enables_rls_and_app_still_reads_and_writes(app, db):
    """TC-51: 所有 public 資料表啟用 RLS；應用程式以資料表擁有者連線，讀寫不受影響。"""
    with _db.engine.begin() as conn:
        lock_down_public_schema(conn)
        assert public_tables(conn)
        assert _rls_disabled(conn) == []

    _db.session.add(User(email="rls@example.com", display_name="RLS"))
    _db.session.commit()
    assert User.query.filter_by(email="rls@example.com").one().display_name == "RLS"


def test_lockdown_revokes_supabase_api_roles_including_future_tables(app, db):
    """TC-51: 模擬 Supabase 預設授權後執行鎖定：anon/authenticated 對現有與日後新建資料表皆無任何權限。"""
    with _db.engine.begin() as conn:
        roles = existing_api_roles(conn)
        if roles != sorted(API_ROLES):
            pytest.skip("test cluster has no Supabase API roles (anon / authenticated)")
        grantees = ", ".join(roles)
        # What Supabase does out of the box
        conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {grantees}"))
        conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {grantees}"))
        assert _api_privileges(conn)

        lock_down_public_schema(conn)
        assert _api_privileges(conn) == []

    try:
        with _db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE public.later_migration_table (id int)"))
            conn.execute(text("CREATE FUNCTION public.later_migration_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'"))
            assert _api_privileges(conn) == []
            for role in API_ROLES:
                assert not conn.execute(text(
                    "SELECT has_function_privilege(:role, 'public.later_migration_fn()', 'EXECUTE')"),
                    {"role": role}).scalar()
    finally:
        with _db.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS public.later_migration_table"))
            conn.execute(text("DROP FUNCTION IF EXISTS public.later_migration_fn()"))
