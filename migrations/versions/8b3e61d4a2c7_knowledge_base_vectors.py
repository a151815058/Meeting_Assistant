"""knowledge base: pgvector + meeting_knowledge tables (REQ-58 ~ REQ-60)

Revision ID: 8b3e61d4a2c7
Revises: 5f2a9c1e7b40
Create Date: 2026-09-29 22:00:00.000000

"""
from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.security.db_hardening import lock_down_public_schema


# revision identifiers, used by Alembic.
revision = '8b3e61d4a2c7'
down_revision = '5f2a9c1e7b40'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # Supabase keeps extensions in the `extensions` schema (on the default search_path); plain
    # PostgreSQL has no such schema. pgvector is not a trusted extension: locally it has to be
    # created once by a superuser (see README) - IF NOT EXISTS then needs no privilege.
    has_extensions_schema = bind.exec_driver_sql(
        "SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'").scalar()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector" + (" WITH SCHEMA extensions" if has_extensions_schema else ""))

    op.create_table(
        'meeting_knowledge',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('minutes_id', sa.String(length=36), nullable=True),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('meeting_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('organizer_id', sa.String(length=36), nullable=False),
        sa.Column('organizer_name', sa.String(length=255), nullable=True),
        sa.Column('participant_emails', postgresql.ARRAY(sa.String(length=255)), nullable=False),
        sa.Column('participant_names', postgresql.ARRAY(sa.String(length=255)), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('key_points', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('decisions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('action_items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('keywords', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('summary_error', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error', sa.String(length=50), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('embedding_model', sa.String(length=255), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['minutes_id'], ['minutes.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['organizer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id'),
    )
    op.create_index('ix_meeting_knowledge_organizer_id', 'meeting_knowledge', ['organizer_id'])
    op.create_index('ix_meeting_knowledge_participant_emails', 'meeting_knowledge', ['participant_emails'],
                    postgresql_using='gin')

    op.create_table(
        'meeting_knowledge_chunks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('knowledge_id', sa.String(length=36), nullable=False),
        sa.Column('meeting_id', sa.String(length=36), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('section', sa.String(length=500), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(dim=384), nullable=False),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['knowledge_id'], ['meeting_knowledge.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('knowledge_id', 'chunk_index', name='uq_knowledge_chunk_index'),
    )
    op.create_index('ix_meeting_knowledge_chunks_knowledge_id', 'meeting_knowledge_chunks', ['knowledge_id'])
    op.create_index('ix_meeting_knowledge_chunks_meeting_id', 'meeting_knowledge_chunks', ['meeting_id'])
    op.create_index('ix_meeting_knowledge_chunks_embedding', 'meeting_knowledge_chunks', ['embedding'],
                    postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})

    # New tables must be closed to Supabase's REST API like the existing ones (REQ-51).
    lock_down_public_schema(bind)


def downgrade():
    op.drop_table('meeting_knowledge_chunks')
    op.drop_table('meeting_knowledge')
    # The vector extension is left installed: other objects may use it.
