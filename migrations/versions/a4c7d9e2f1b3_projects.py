"""projects: project management, meeting -> project link, project metadata in the knowledge base
(REQ-64 ~ REQ-68)

Revision ID: a4c7d9e2f1b3
Revises: 8b3e61d4a2c7
Create Date: 2026-10-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from app.security.db_hardening import lock_down_public_schema


# revision identifiers, used by Alembic.
revision = 'a4c7d9e2f1b3'
down_revision = '8b3e61d4a2c7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projects_owner_id', 'projects', ['owner_id'])

    op.create_table(
        'project_stakeholders',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=255), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_project_stakeholders_project_id', 'project_stakeholders', ['project_id'])

    op.add_column('meetings', sa.Column('project_id', sa.String(length=36), nullable=True))
    op.create_index('ix_meetings_project_id', 'meetings', ['project_id'])
    op.create_foreign_key('fk_meetings_project_id', 'meetings', 'projects', ['project_id'], ['id'],
                          ondelete='SET NULL')

    op.add_column('participants', sa.Column('display_name_edited', sa.Boolean(), nullable=False,
                                            server_default=sa.false()))

    op.add_column('meeting_knowledge', sa.Column('project_id', sa.String(length=36), nullable=True))
    op.add_column('meeting_knowledge', sa.Column('project_name', sa.String(length=255), nullable=True))
    op.add_column('meeting_knowledge', sa.Column('minutes_hash', sa.String(length=64), nullable=True))
    op.create_index('ix_meeting_knowledge_project_id', 'meeting_knowledge', ['project_id'])
    op.create_foreign_key('fk_meeting_knowledge_project_id', 'meeting_knowledge', 'projects', ['project_id'], ['id'],
                          ondelete='SET NULL')

    # New tables must be closed to Supabase's REST API like the existing ones (REQ-51).
    lock_down_public_schema(op.get_bind())


def downgrade():
    op.drop_constraint('fk_meeting_knowledge_project_id', 'meeting_knowledge', type_='foreignkey')
    op.drop_index('ix_meeting_knowledge_project_id', table_name='meeting_knowledge')
    op.drop_column('meeting_knowledge', 'minutes_hash')
    op.drop_column('meeting_knowledge', 'project_name')
    op.drop_column('meeting_knowledge', 'project_id')
    op.drop_column('participants', 'display_name_edited')
    op.drop_constraint('fk_meetings_project_id', 'meetings', type_='foreignkey')
    op.drop_index('ix_meetings_project_id', table_name='meetings')
    op.drop_column('meetings', 'project_id')
    op.drop_table('project_stakeholders')
    op.drop_table('projects')
