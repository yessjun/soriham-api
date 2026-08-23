"""add_multitenancy

Revision ID: 2e6e9a392f6f
Revises: 5c16ca905783
Create Date: 2026-08-23 13:41:53.195946

사용자·워크스페이스·세션·공유 테이블을 만들고, 녹음과 태그를 워크스페이스에 묶는다.

`recordings`와 `tags`가 비어 있어야 적용된다. 소유자 없는 기존 행에 NOT NULL을 채울
방법이 없기 때문인데, 리비전이 그것을 지우지는 않는다 — 리비전은 모든 데이터베이스에서
영원히 도는 아티팩트라 여기서 지우면 언젠가 지우면 안 되는 곳에서도 지운다. 비우는 것은
운영자가 그 데이터베이스를 보고 하는 별도 판단이다.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2e6e9a392f6f'
down_revision: str | Sequence[str] | None = '5c16ca905783'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 상태머신에 quota_blocked가 더해진다. alembic autogenerate는 CHECK 제약의 *변경*을
# 감지하지 못하므로(추가는 감지한다) 손으로 갈아 끼운다
RECORDING_STATUSES = (
    'pending', 'transcribing', 'diarizing', 'enriching',
    'done', 'error', 'missing', 'duplicate', 'quota_blocked',
)


def _guard_empty() -> None:
    """소유자를 정할 수 없는 기존 행이 있으면 멈춘다."""
    op.execute(
        """
        do $$
        declare n bigint;
        begin
            select count(*) into n from recordings;
            if n > 0 then
                raise exception
                    '녹음 %건이 남아 있어 적용할 수 없습니다. 워크스페이스가 없던 시절의 '
                    '행이라 소유자를 정할 수 없습니다 — 비우고 다시 시도하세요', n;
            end if;
            select count(*) into n from tags;
            if n > 0 then
                raise exception '태그 %건이 남아 있어 적용할 수 없습니다', n;
            end if;
        end $$;
        """
    )


def upgrade() -> None:
    """Upgrade schema."""
    _guard_empty()
    op.create_table('auth_attempts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), server_default='0', nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key', 'window_start', name='uq_auth_attempts_key_window')
    )
    op.create_index('ix_auth_attempts_window_start', 'auth_attempts', ['window_start'], unique=False)
    op.create_table('workspaces',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), server_default='team', nullable=False),
    sa.Column('last_claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('quota_minutes', sa.Integer(), nullable=True),
    sa.Column('quota_bytes', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind in ('personal', 'team')", name='workspaces_kind_check'),
    sa.CheckConstraint('slug = lower(slug)', name='workspaces_slug_lower_check'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id'),
    sa.UniqueConstraint('slug')
    )
    op.create_index(op.f('ix_workspaces_last_claimed_at'), 'workspaces', ['last_claimed_at'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('email', sa.Text(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('signup_note', sa.Text(), nullable=True),
    sa.Column('reviewed_by_user_id', sa.BigInteger(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_service_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('default_workspace_id', sa.BigInteger(), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status in ('pending', 'active', 'rejected', 'disabled')", name='users_status_check'),
    sa.CheckConstraint('email = lower(email)', name='users_email_lower_check'),
    sa.ForeignKeyConstraint(['default_workspace_id'], ['workspaces.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reviewed_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('public_id')
    )
    op.create_table('invites',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('workspace_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.Text(), server_default='member', nullable=False),
    sa.Column('created_by_user_id', sa.BigInteger(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('max_uses', sa.Integer(), server_default='1', nullable=False),
    sa.Column('uses', sa.Integer(), server_default='0', nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role in ('owner', 'admin', 'member', 'viewer')", name='invites_role_check'),
    sa.CheckConstraint('email is null or email = lower(email)', name='invites_email_lower_check'),
    sa.CheckConstraint('uses <= max_uses', name='invites_uses_check'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index('ix_invites_email', 'invites', ['email'], unique=False)
    op.create_index(op.f('ix_invites_workspace_id'), 'invites', ['workspace_id'], unique=False)
    op.create_table('user_sessions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('csrf_token', sa.Text(), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('ip', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_user_sessions_expires_at'), 'user_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)
    op.create_table('workspace_members',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('workspace_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.Text(), server_default='member', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("role in ('owner', 'admin', 'member', 'viewer')", name='workspace_members_role_check'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'user_id', name='uq_workspace_members_ws_user')
    )
    op.create_index(op.f('ix_workspace_members_user_id'), 'workspace_members', ['user_id'], unique=False)
    op.create_index(op.f('ix_workspace_members_workspace_id'), 'workspace_members', ['workspace_id'], unique=False)
    op.create_index('uq_workspace_members_single_owner', 'workspace_members', ['workspace_id'], unique=True, postgresql_where=sa.text("role = 'owner'"))
    op.create_table('recording_shares',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('invite_email', sa.Text(), nullable=True),
    sa.Column('permission', sa.Text(), server_default='view', nullable=False),
    sa.Column('created_by_user_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("permission in ('view', 'edit')", name='recording_shares_permission_check'),
    sa.CheckConstraint('(user_id is null) <> (invite_email is null)', name='recording_shares_target_check'),
    sa.CheckConstraint('invite_email is null or invite_email = lower(invite_email)', name='recording_shares_email_lower_check'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id')
    )
    op.create_index('ix_recording_shares_invite_email', 'recording_shares', ['invite_email'], unique=False)
    op.create_index(op.f('ix_recording_shares_recording_id'), 'recording_shares', ['recording_id'], unique=False)
    op.create_index('ix_recording_shares_user_id', 'recording_shares', ['user_id'], unique=False)
    op.create_index('uq_recording_shares_email', 'recording_shares', ['recording_id', 'invite_email'], unique=True, postgresql_where=sa.text('invite_email is not null'))
    op.create_index('uq_recording_shares_user', 'recording_shares', ['recording_id', 'user_id'], unique=True, postgresql_where=sa.text('user_id is not null'))
    op.create_table('share_links',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('public_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('recording_id', sa.BigInteger(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('label', sa.Text(), nullable=True),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('allow_audio', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('allow_speaker_names', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('view_count', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_viewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_user_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_share_links_recording_id'), 'share_links', ['recording_id'], unique=False)
    op.add_column('job_log', sa.Column('workspace_id', sa.BigInteger(), nullable=False))
    op.alter_column('job_log', 'recording_id',
               existing_type=sa.BIGINT(),
               nullable=True)
    op.create_index(op.f('ix_job_log_workspace_id'), 'job_log', ['workspace_id'], unique=False)
    op.drop_constraint(op.f('job_log_recording_id_fkey'), 'job_log', type_='foreignkey')
    op.create_foreign_key('job_log_workspace_id_fkey', 'job_log', 'workspaces', ['workspace_id'], ['id'], ondelete='CASCADE')
    op.create_foreign_key('job_log_recording_id_fkey', 'job_log', 'recordings', ['recording_id'], ['id'], ondelete='SET NULL')
    op.add_column('recordings', sa.Column('workspace_id', sa.BigInteger(), nullable=False))
    op.add_column('recordings', sa.Column('created_by_user_id', sa.BigInteger(), nullable=True))
    op.add_column('recordings', sa.Column('source', sa.Text(), server_default='upload', nullable=False))
    op.drop_index(op.f('ix_recordings_partial_hash'), table_name='recordings')
    op.drop_index(op.f('ix_recordings_recorded_at'), table_name='recordings')
    op.drop_index(op.f('ix_recordings_status'), table_name='recordings')
    op.create_index('ix_recordings_status_workspace', 'recordings', ['status', 'workspace_id'], unique=False)
    op.create_index(op.f('ix_recordings_workspace_id'), 'recordings', ['workspace_id'], unique=False)
    op.create_index('ix_recordings_workspace_partial_hash', 'recordings', ['workspace_id', 'partial_hash'], unique=False)
    op.create_index('ix_recordings_workspace_recorded_at', 'recordings', ['workspace_id', sa.literal_column('recorded_at DESC NULLS LAST'), sa.literal_column('id DESC')], unique=False)
    op.create_foreign_key('recordings_created_by_user_id_fkey', 'recordings', 'users', ['created_by_user_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('recordings_workspace_id_fkey', 'recordings', 'workspaces', ['workspace_id'], ['id'], ondelete='CASCADE')
    op.create_check_constraint('recordings_source_check', 'recordings', "source in ('upload', 'scan')")
    op.add_column('tags', sa.Column('workspace_id', sa.BigInteger(), nullable=False))
    op.drop_constraint(op.f('tags_name_key'), 'tags', type_='unique')
    op.create_index(op.f('ix_tags_workspace_id'), 'tags', ['workspace_id'], unique=False)
    op.create_unique_constraint('uq_tags_workspace_name', 'tags', ['workspace_id', 'name'])
    op.create_foreign_key('tags_workspace_id_fkey', 'tags', 'workspaces', ['workspace_id'], ['id'], ondelete='CASCADE')
    # ### end Alembic commands ###
    op.drop_constraint('recordings_status_check', 'recordings', type_='check')
    op.create_check_constraint(
        'recordings_status_check',
        'recordings',
        'status in ({})'.format(', '.join(f"'{s}'" for s in RECORDING_STATUSES)),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 파괴적 downgrade: 사용자·워크스페이스·세션·초대·공유가 전부 사라지고, 녹음과
    # 태그는 소속을 잃는다.
    #
    # 두 워크스페이스가 같은 태그 이름을 가진 뒤에는 실행되지 않는다 — 아래에서
    # 되돌리는 UNIQUE(name)이 중복으로 거절된다. 엔리치먼트가 "회의" 같은 이름을
    # 며칠 안에 양쪽에 만들므로 사실상 일회용 경로다.
    op.execute('drop index if exists ix_recordings_status_workspace')
    op.drop_constraint('recordings_status_check', 'recordings', type_='check')
    op.create_check_constraint(
        'recordings_status_check',
        'recordings',
        "status in ('pending', 'transcribing', 'diarizing', 'enriching', "
        "'done', 'error', 'missing', 'duplicate')",
    )
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint('tags_workspace_id_fkey', 'tags', type_='foreignkey')
    op.drop_constraint('uq_tags_workspace_name', 'tags', type_='unique')
    op.drop_index(op.f('ix_tags_workspace_id'), table_name='tags')
    op.create_unique_constraint(op.f('tags_name_key'), 'tags', ['name'], postgresql_nulls_not_distinct=False)
    op.drop_column('tags', 'workspace_id')
    op.drop_constraint('recordings_source_check', 'recordings', type_='check')
    op.drop_constraint('recordings_workspace_id_fkey', 'recordings', type_='foreignkey')
    op.drop_constraint('recordings_created_by_user_id_fkey', 'recordings', type_='foreignkey')
    op.drop_index('ix_recordings_workspace_recorded_at', table_name='recordings')
    op.drop_index('ix_recordings_workspace_partial_hash', table_name='recordings')
    op.drop_index(op.f('ix_recordings_workspace_id'), table_name='recordings')
    op.create_index(op.f('ix_recordings_status'), 'recordings', ['status'], unique=False)
    op.create_index(op.f('ix_recordings_recorded_at'), 'recordings', [sa.literal_column('recorded_at DESC NULLS LAST')], unique=False)
    op.create_index(op.f('ix_recordings_partial_hash'), 'recordings', ['partial_hash'], unique=False)
    op.drop_column('recordings', 'source')
    op.drop_column('recordings', 'created_by_user_id')
    op.drop_column('recordings', 'workspace_id')
    op.drop_constraint('job_log_recording_id_fkey', 'job_log', type_='foreignkey')
    op.drop_constraint('job_log_workspace_id_fkey', 'job_log', type_='foreignkey')
    op.create_foreign_key(op.f('job_log_recording_id_fkey'), 'job_log', 'recordings', ['recording_id'], ['id'], ondelete='CASCADE')
    op.drop_index(op.f('ix_job_log_workspace_id'), table_name='job_log')
    op.alter_column('job_log', 'recording_id',
               existing_type=sa.BIGINT(),
               nullable=False)
    op.drop_column('job_log', 'workspace_id')
    op.drop_index(op.f('ix_share_links_recording_id'), table_name='share_links')
    op.drop_table('share_links')
    op.drop_index('uq_recording_shares_user', table_name='recording_shares', postgresql_where=sa.text('user_id is not null'))
    op.drop_index('uq_recording_shares_email', table_name='recording_shares', postgresql_where=sa.text('invite_email is not null'))
    op.drop_index('ix_recording_shares_user_id', table_name='recording_shares')
    op.drop_index(op.f('ix_recording_shares_recording_id'), table_name='recording_shares')
    op.drop_index('ix_recording_shares_invite_email', table_name='recording_shares')
    op.drop_table('recording_shares')
    op.drop_index('uq_workspace_members_single_owner', table_name='workspace_members', postgresql_where=sa.text("role = 'owner'"))
    op.drop_index(op.f('ix_workspace_members_workspace_id'), table_name='workspace_members')
    op.drop_index(op.f('ix_workspace_members_user_id'), table_name='workspace_members')
    op.drop_table('workspace_members')
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_expires_at'), table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_index(op.f('ix_invites_workspace_id'), table_name='invites')
    op.drop_index('ix_invites_email', table_name='invites')
    op.drop_table('invites')
    op.drop_table('users')
    op.drop_index(op.f('ix_workspaces_last_claimed_at'), table_name='workspaces')
    op.drop_table('workspaces')
    op.drop_index('ix_auth_attempts_window_start', table_name='auth_attempts')
    op.drop_table('auth_attempts')
    # ### end Alembic commands ###
