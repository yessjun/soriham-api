"""initial_schema

Revision ID: 59b5b7474128
Revises: 
Create Date: 2026-08-17 14:47:33.737128

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '59b5b7474128'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 한국어 부분 문자열 검색용 trigram 확장
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('recordings',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('filename', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('partial_hash', sa.Text(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_sec', sa.Double(), nullable=True),
    sa.Column('language', sa.Text(), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('stt_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('duplicate_of_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status in ('pending', 'transcribing', 'diarizing', 'enriching', 'done', 'error', 'missing', 'duplicate')", name='recordings_status_check'),
    sa.ForeignKeyConstraint(['duplicate_of_id'], ['recordings.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('path'),
    sa.UniqueConstraint('public_id')
    )
    op.create_index(op.f('ix_recordings_partial_hash'), 'recordings', ['partial_hash'], unique=False)
    op.create_table('tags',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'),
    sa.UniqueConstraint('public_id')
    )
    op.create_table('job_log',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('stage', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('audio_sec', sa.Double(), nullable=True),
    sa.Column('elapsed_sec', sa.Double(), nullable=True),
    sa.Column('device', sa.Text(), nullable=True),
    sa.Column('model', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_log_recording_id'), 'job_log', ['recording_id'], unique=False)
    op.create_table('recording_tags',
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('tag_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recording_id', 'tag_id')
    )
    op.create_table('segments',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('idx', sa.Integer(), nullable=False),
    sa.Column('start_sec', sa.Double(), nullable=False),
    sa.Column('end_sec', sa.Double(), nullable=False),
    sa.Column('speaker_key', sa.Text(), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('words', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recording_id', 'idx')
    )
    op.create_index(op.f('ix_segments_recording_id'), 'segments', ['recording_id'], unique=False)
    op.create_table('speaker_names',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('speaker_key', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recording_id', 'speaker_key')
    )
    op.create_index(op.f('ix_speaker_names_recording_id'), 'speaker_names', ['recording_id'], unique=False)

    # 검색·큐 소비용 인덱스
    op.create_index('ix_recordings_status', 'recordings', ['status'])
    op.create_index(
        'ix_recordings_recorded_at', 'recordings', [sa.text('recorded_at DESC NULLS LAST')]
    )
    for name, table, column in [
        ('ix_segments_text_trgm', 'segments', 'text'),
        ('ix_recordings_filename_trgm', 'recordings', 'filename'),
        ('ix_recordings_title_trgm', 'recordings', 'title'),
        ('ix_recordings_summary_trgm', 'recordings', 'summary'),
    ]:
        op.create_index(
            name,
            table,
            [column],
            postgresql_using='gin',
            postgresql_ops={column: 'gin_trgm_ops'},
        )


def downgrade() -> None:
    """Downgrade schema."""
    # 파괴적 downgrade: 전 테이블과 데이터를 삭제한다
    op.drop_index('ix_recordings_summary_trgm', table_name='recordings')
    op.drop_index('ix_recordings_title_trgm', table_name='recordings')
    op.drop_index('ix_recordings_filename_trgm', table_name='recordings')
    op.drop_index('ix_segments_text_trgm', table_name='segments')
    op.drop_index('ix_recordings_recorded_at', table_name='recordings')
    op.drop_index('ix_recordings_status', table_name='recordings')
    op.drop_index(op.f('ix_speaker_names_recording_id'), table_name='speaker_names')
    op.drop_table('speaker_names')
    op.drop_index(op.f('ix_segments_recording_id'), table_name='segments')
    op.drop_table('segments')
    op.drop_table('recording_tags')
    op.drop_index(op.f('ix_job_log_recording_id'), table_name='job_log')
    op.drop_table('job_log')
    op.drop_table('tags')
    op.drop_index(op.f('ix_recordings_partial_hash'), table_name='recordings')
    op.drop_table('recordings')
    # ### end Alembic commands ###
