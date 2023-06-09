"""Upgrade to SQLAlchemy 2.0

Revision ID: fd22da67719f
Revises: 72fd142f3f59
Create Date: 2023-06-08 12:45:32.732393

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'fd22da67719f'
down_revision = '72fd142f3f59'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('stories', 'published_date',
               existing_type=postgresql.TIMESTAMP(),
               nullable=True)
    op.drop_index('story_above_threshold', table_name='stories')
    op.drop_index('story_posted_date', table_name='stories')
    op.drop_index('story_posted_date_threshold', table_name='stories')
    op.drop_index('story_processed_date', table_name='stories')
    op.drop_index('story_processed_date_threshold', table_name='stories')
    op.drop_index('story_project', table_name='stories')
    op.drop_index('story_project_id', table_name='stories')
    op.drop_index('story_published_date', table_name='stories')
    op.drop_index('story_published_date_threshold', table_name='stories')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_index('story_published_date_threshold', 'stories', [sa.text('(published_date::date)'), 'above_threshold'], unique=False)
    op.create_index('story_published_date', 'stories', [sa.text('(published_date::date)')], unique=False)
    op.create_index('story_project_id', 'stories', ['project_id'], unique=False)
    op.create_index('story_project', 'stories', ['stories_id', 'project_id'], unique=False)
    op.create_index('story_processed_date_threshold', 'stories', [sa.text('(processed_date::date)'), 'above_threshold'], unique=False)
    op.create_index('story_processed_date', 'stories', [sa.text('(processed_date::date)')], unique=False)
    op.create_index('story_posted_date_threshold', 'stories', [sa.text('(posted_date::date)'), 'above_threshold'], unique=False)
    op.create_index('story_posted_date', 'stories', [sa.text('(posted_date::date)')], unique=False)
    op.create_index('story_above_threshold', 'stories', ['above_threshold'], unique=False)
    op.alter_column('stories', 'published_date',
               existing_type=postgresql.TIMESTAMP(),
               nullable=False)
    # ### end Alembic commands ###
