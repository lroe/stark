"""Add status and admin fields for review system

Revision ID: 4b4f89a747f1
Revises: 
Create Date: 2025-06-23 10:02:21.925425

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4b4f89a747f1'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=80), nullable=False),
    sa.Column('password_hash', sa.String(length=128), nullable=True),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_table('course',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=150), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('is_published', sa.Boolean(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('thumbnail_url', sa.String(length=255), nullable=True),
    sa.Column('shareable_link_id', sa.String(length=36), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('shareable_link_id')
    )
    op.create_table('enrollment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('course_id', sa.String(length=36), nullable=False),
    sa.Column('last_completed_chapter_number', sa.Integer(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['course_id'], ['course.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'course_id', name='_user_course_uc')
    )
    op.create_table('lesson',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.String(length=150), nullable=False),
    sa.Column('raw_script', sa.Text(), nullable=False),
    sa.Column('parsed_json', sa.Text(), nullable=False),
    sa.Column('course_id', sa.String(length=36), nullable=False),
    sa.Column('chapter_number', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['course.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('review',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('course_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['course.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'course_id', name='_user_course_review_uc')
    )
    op.create_table('chat_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enrollment_id', sa.Integer(), nullable=False),
    sa.Column('lesson_id', sa.String(length=36), nullable=False),
    sa.Column('history_json', sa.Text(), nullable=False),
    sa.Column('current_step_index', sa.Integer(), nullable=False),
    sa.Column('current_chunk_index', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['enrollment_id'], ['enrollment.id'], ),
    sa.ForeignKeyConstraint(['lesson_id'], ['lesson.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('enrollment_id', 'lesson_id', name='_enrollment_lesson_uc')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('chat_history')
    op.drop_table('review')
    op.drop_table('lesson')
    op.drop_table('enrollment')
    op.drop_table('course')
    op.drop_table('user')
    # ### end Alembic commands ###
