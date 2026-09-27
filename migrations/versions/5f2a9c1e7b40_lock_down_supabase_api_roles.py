"""lock down tables from Supabase API roles (REQ-51)

Revision ID: 5f2a9c1e7b40
Revises: c99d57ae58de
Create Date: 2026-09-27 10:00:00.000000

"""
from alembic import op

from app.security.db_hardening import lock_down_public_schema


# revision identifiers, used by Alembic.
revision = '5f2a9c1e7b40'
down_revision = 'c99d57ae58de'
branch_labels = None
depends_on = None


def upgrade():
    lock_down_public_schema(op.get_bind())


def downgrade():
    # Only row-level security is switched off. The API roles' privileges are deliberately not
    # granted back: re-exposing every table through the public REST API must be a conscious,
    # manual decision.
    bind = op.get_bind()
    for (table,) in bind.exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
