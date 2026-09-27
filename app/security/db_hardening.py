"""Close Supabase's auto-generated REST API (PostgREST) off from the application tables (REQ-51).

Supabase grants its public API roles full access to every table created in the `public` schema,
and anyone holding the project's (public) anon key can reach them through the REST API. This app
never uses that API: it connects directly as the table owner, which row-level security does not
restrict. So the tables are locked down completely for the API roles:

- row-level security on every table, with no policies (the API roles see no rows);
- all privileges of the API roles revoked on existing tables, sequences and functions;
- default privileges revoked, so objects created by later migrations are not granted to them either.

Plain PostgreSQL (local development, tests) has no such roles; only row-level security is enabled.
"""
from sqlalchemy import text

API_ROLES = ("anon", "authenticated")


def _quote(conn, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def existing_api_roles(conn) -> list[str]:
    rows = conn.execute(text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
                        {"names": list(API_ROLES)})
    return sorted(r[0] for r in rows)


def public_tables(conn) -> list[str]:
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))
    return [r[0] for r in rows]


def lock_down_public_schema(conn) -> None:
    for table in public_tables(conn):
        conn.execute(text(f"ALTER TABLE public.{_quote(conn, table)} ENABLE ROW LEVEL SECURITY"))

    roles = existing_api_roles(conn)
    if not roles:
        return
    grantees = ", ".join(_quote(conn, r) for r in roles)
    conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {grantees}"))
    conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {grantees}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {grantees}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {grantees}"))
    # Functions in `public` are callable through the REST API's /rpc endpoint. PostgreSQL also grants
    # EXECUTE to PUBLIC (every role) by default, and that default can only be revoked globally, not
    # per schema; it applies to functions the connected role creates (the app creates none).
    conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC, {grantees}"))
    conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM {grantees}"))
    conn.execute(text("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"))
