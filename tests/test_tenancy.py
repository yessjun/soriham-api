from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from soriham_api import auth
from soriham_api.models import Invite, Recording, RecordingShare, User, Workspace, WorkspaceMember
from soriham_api.tenancy import (
    EmailTaken,
    InviteInvalid,
    accept_invite,
    add_member,
    approve,
    bootstrap,
    claim_pending,
    create_invite,
    create_user,
    create_workspace,
    reject,
    signup,
    unique_slug,
)


def make_recording(db, workspace, name: str = "a.wav") -> Recording:
    recording = Recording(
        workspace_id=workspace.id,
        source="upload",
        path=f"/tmp/{workspace.slug}/{name}",
        filename=name,
        size_bytes=10,
        partial_hash=f"{workspace.slug}-{name}",
        status="pending",
    )
    db.add(recording)
    db.flush()
    return recording


def test_가입하면_대기_상태로_개인_워크스페이스가_함께_생긴다(db):
    result = signup(db, email="New@Example.com", password="암구호 여덟", display_name="새사람")
    db.commit()

    assert result.user.status == "pending"
    assert result.user.email == "new@example.com"
    assert result.workspace.kind == "personal"
    assert result.user.default_workspace_id == result.workspace.id
    role = db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == result.workspace.id,
            WorkspaceMember.user_id == result.user.id,
        )
    )
    assert role == "owner"


def test_같은_이메일로_두_번_가입할_수_없다(db):
    signup(db, email="dup@example.com", password="암구호", display_name="하나")
    db.commit()
    with pytest.raises(EmailTaken):
        signup(db, email="DUP@example.com", password="암구호", display_name="둘")


def test_자동_승인을_켜면_바로_쓸_수_있다(db):
    """가입 개방으로 바꾸는 비용이 설정 하나여야 한다."""
    result = signup(
        db, email="auto@example.com", password="암구호", display_name="자동", auto_approve=True
    )
    db.commit()
    assert result.user.status == "active"


def test_슬러그가_겹치면_다른_이름을_찾는다(db):
    create_workspace(db, slug="taken", name="이미 있음")
    db.flush()
    assert unique_slug(db, "taken") != "taken"
    assert unique_slug(db, "free") == "free"


def test_승인하면_같은_계정_그대로_활성이_된다(db):
    result = signup(db, email="p@example.com", password="암구호", display_name="신청자")
    reviewer = create_user(
        db,
        email="admin@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="운영자",
        status="active",
        is_service_admin=True,
    )
    db.commit()

    approve(db, result.user, reviewer=reviewer)
    db.commit()

    assert result.user.status == "active"
    assert result.user.reviewed_by_user_id == reviewer.id
    assert result.user.reviewed_at is not None


def test_거절하면_개인_워크스페이스가_남지_않는다(db):
    """안 지우면 거절된 계정의 빈 워크스페이스가 영구히 쌓인다."""
    result = signup(db, email="no@example.com", password="암구호", display_name="거절될 사람")
    db.commit()
    workspace_id = result.workspace.id

    reject(db, result.user)
    db.commit()

    assert result.user.status == "rejected"
    assert db.get(Workspace, workspace_id) is None
    assert result.user.default_workspace_id is None


def test_녹음이_남은_워크스페이스는_거절해도_지우지_않는다(db):
    """조용히 없애면 안 되는 것이 있으면 남긴다."""
    result = signup(db, email="has@example.com", password="암구호", display_name="가진 사람")
    make_recording(db, result.workspace)
    db.commit()

    reject(db, result.user)
    db.commit()

    assert db.get(Workspace, result.workspace.id) is not None


def test_팀_워크스페이스는_거절로_지워지지_않는다(db, workspace):
    result = signup(db, email="member@example.com", password="암구호", display_name="구성원")
    add_member(db, workspace, result.user, "member")
    db.commit()

    reject(db, result.user)
    db.commit()

    assert db.get(Workspace, workspace.id) is not None


def test_가입_전에_이메일로_건_공유가_승인_때_이어진다(db, workspace):
    """상대가 가입하기 전에 미리 공유해 두는 것이 이 서비스의 흔한 쓰임이다."""
    recording = make_recording(db, workspace)
    db.add(
        RecordingShare(
            recording_id=recording.id, invite_email="later@example.com", permission="edit"
        )
    )
    db.commit()

    result = signup(db, email="later@example.com", password="암구호", display_name="나중")
    db.commit()
    approve(db, result.user)
    db.commit()

    share = db.scalar(select(RecordingShare).where(RecordingShare.recording_id == recording.id))
    assert share.user_id == result.user.id
    assert share.invite_email is None
    assert share.permission == "edit"


def test_예약_공유와_직접_공유가_겹치면_높은_쪽이_남는다(db, workspace):
    recording = make_recording(db, workspace)
    result = signup(db, email="both@example.com", password="암구호", display_name="둘 다")
    db.flush()
    db.add(RecordingShare(recording_id=recording.id, user_id=result.user.id, permission="view"))
    db.add(
        RecordingShare(
            recording_id=recording.id, invite_email="both@example.com", permission="edit"
        )
    )
    db.commit()

    claim_pending(db, result.user)
    db.commit()

    shares = db.scalars(
        select(RecordingShare).where(RecordingShare.recording_id == recording.id)
    ).all()
    assert len(shares) == 1
    assert shares[0].permission == "edit"


def test_가입_전에_건_초대가_승인_때_구성원으로_바뀐다(db, workspace):
    create_invite(db, workspace=workspace, role="member", email="invitee@example.com")
    db.commit()

    result = signup(db, email="invitee@example.com", password="암구호", display_name="초대받음")
    db.commit()
    approve(db, result.user)
    db.commit()

    role = db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == result.user.id,
        )
    )
    assert role == "member"


def test_만료된_초대는_승인_때도_이어지지_않는다(db, workspace):
    issued = create_invite(db, workspace=workspace, email="late@example.com")
    issued.invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    result = signup(db, email="late@example.com", password="암구호", display_name="늦음")
    db.commit()
    approve(db, result.user)
    db.commit()

    assert (
        db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == result.user.id,
                WorkspaceMember.workspace_id == workspace.id,
            )
        )
        is None
    )


def test_초대는_토큰으로_받고_한_번만_쓰인다(db, workspace):
    issued = create_invite(db, workspace=workspace, role="viewer")
    user = create_user(
        db,
        email="guest@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="손님",
        status="active",
    )
    db.commit()

    joined = accept_invite(db, issued.token, user)
    db.commit()
    assert joined.id == workspace.id

    with pytest.raises(InviteInvalid):
        accept_invite(db, issued.token, user)


def test_철회된_초대는_받을_수_없다(db, workspace):
    issued = create_invite(db, workspace=workspace)
    issued.invite.revoked_at = datetime.now(UTC)
    user = create_user(
        db,
        email="guest@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="손님",
        status="active",
    )
    db.commit()
    with pytest.raises(InviteInvalid):
        accept_invite(db, issued.token, user)


def test_이메일이_지정된_초대는_그_사람만_받는다(db, workspace):
    issued = create_invite(db, workspace=workspace, email="named@example.com")
    other = create_user(
        db,
        email="other@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="다른 사람",
        status="active",
    )
    db.commit()
    with pytest.raises(InviteInvalid):
        accept_invite(db, issued.token, other)


def test_없는_초대와_철회된_초대를_구분해_알려주지_않는다(db, workspace):
    """구분해 주면 토큰을 찔러보는 것만으로 존재 여부를 알 수 있다."""
    issued = create_invite(db, workspace=workspace)
    issued.invite.revoked_at = datetime.now(UTC)
    user = create_user(
        db,
        email="guest@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="손님",
        status="active",
    )
    db.commit()

    with pytest.raises(InviteInvalid) as revoked:
        accept_invite(db, issued.token, user)
    with pytest.raises(InviteInvalid) as missing:
        accept_invite(db, auth.new_token(), user)
    assert str(revoked.value) == str(missing.value)


def test_DB에는_초대_원문_토큰이_남지_않는다(db, workspace):
    issued = create_invite(db, workspace=workspace)
    db.commit()
    stored = db.scalars(select(Invite.token_hash)).all()
    assert issued.token not in stored
    assert auth.token_hash(issued.token) in stored


def test_워크스페이스마다_소유자는_하나뿐이다(db, workspace):
    """부분 유니크 인덱스가 강제한다. 소유자를 별도 컬럼으로 두지 않는 근거다."""
    first = create_user(
        db,
        email="a@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="가",
        status="active",
    )
    second = create_user(
        db,
        email="b@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="나",
        status="active",
    )
    add_member(db, workspace, first, "owner")
    db.commit()

    with pytest.raises(IntegrityError):
        add_member(db, workspace, second, "owner")
        db.commit()
    db.rollback()


def test_부트스트랩은_두_번_돌려도_안전하다(db):
    """승인할 사람이 먼저 있어야 하고, 마이그레이션은 행을 넣지 않는다."""
    user, workspace, created = bootstrap(
        db,
        email="owner@example.com",
        password="암구호",
        display_name="소유자",
        workspace_slug="mine",
        workspace_name="내 보관함",
    )
    db.commit()
    assert created and user.is_service_admin and user.status == "active"
    assert workspace.quota_minutes is None and workspace.quota_bytes is None

    again_user, again_ws, created_again = bootstrap(
        db,
        email="owner@example.com",
        password="다른 암구호",
        display_name="소유자",
        workspace_slug="mine",
        workspace_name="내 보관함",
    )
    db.commit()
    assert not created_again
    assert again_user.id == user.id and again_ws.id == workspace.id
    assert db.scalar(select(User.password_hash).where(User.id == user.id)) == user.password_hash


def test_이미_구성원인_사람이_눌러도_초대가_소모되지_않는다(db, workspace):
    """한 번 쓸 수 있는 초대를 구성원이 실수로 누르면 정작 부를 사람이 못 들어온다."""
    member = create_user(
        db,
        email="member@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="이미 구성원",
        status="active",
    )
    add_member(db, workspace, member, "member")
    newcomer = create_user(
        db,
        email="newcomer@example.com",
        password_hash=auth.hash_password("암구호"),
        display_name="새 사람",
        status="active",
    )
    issued = create_invite(db, workspace=workspace, max_uses=1)
    db.commit()

    accept_invite(db, issued.token, member)
    db.commit()
    assert issued.invite.uses == 0

    accept_invite(db, issued.token, newcomer)
    db.commit()
    assert issued.invite.uses == 1


def test_거절했다_승인하면_쓸_워크스페이스가_다시_생긴다(db):
    """거절이 개인 워크스페이스를 지우므로, 되돌릴 때 그것도 되돌아와야 한다.

    이메일이 남아 재가입도 막히므로 승인이 유일한 복구 경로다.
    """
    result = signup(db, email="oops@example.com", password="암구호", display_name="오해")
    db.commit()
    reject(db, result.user)
    db.commit()

    approve(db, result.user)
    db.commit()

    assert result.user.status == "active"
    assert result.user.default_workspace_id is not None
    workspace = db.get(Workspace, result.user.default_workspace_id)
    assert workspace is not None and workspace.kind == "personal"
    role = db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == result.user.id,
        )
    )
    assert role == "owner"


def test_예약_공유가_이미_가진_권한을_끌어내리지_않는다(db, workspace):
    """앞의 시험은 올리는 방향만 봤다. 내리는 방향이 더 나쁘다."""
    recording = make_recording(db, workspace)
    result = signup(db, email="down@example.com", password="암구호", display_name="내려감")
    db.flush()
    db.add(RecordingShare(recording_id=recording.id, user_id=result.user.id, permission="edit"))
    db.add(
        RecordingShare(
            recording_id=recording.id, invite_email="down@example.com", permission="view"
        )
    )
    db.commit()

    claim_pending(db, result.user)
    db.commit()

    shares = db.scalars(
        select(RecordingShare).where(RecordingShare.recording_id == recording.id)
    ).all()
    assert len(shares) == 1
    assert shares[0].permission == "edit"


def test_다_쓴_초대는_승인_때도_이어지지_않는다(db, workspace):
    """막지 않으면 사용 횟수가 상한을 넘어 제약 위반으로 승인 중간에 터진다."""
    issued = create_invite(db, workspace=workspace, email="used@example.com", max_uses=1)
    issued.invite.uses = 1
    db.commit()

    result = signup(db, email="used@example.com", password="암구호", display_name="다 씀")
    db.commit()
    approve(db, result.user)
    db.commit()

    assert issued.invite.uses == 1
    assert (
        db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == result.user.id,
                WorkspaceMember.workspace_id == workspace.id,
            )
        )
        is None
    )


def test_형식이_아닌_이메일로는_가입할_수_없다(db):
    """가입은 형식을 안 보고 공유만 봐서, @ 없는 주소로 가입한 실존 계정에 공유하면
    422가 나는 조합이 있었다."""
    from soriham_api.tenancy import MemberInvalid

    with pytest.raises(MemberInvalid):
        signup(db, email="골뱅이없음", password="암구호", display_name="이상")


def test_형식이_아닌_이메일로는_초대할_수_없다(db, workspace):
    from soriham_api.tenancy import MemberInvalid

    with pytest.raises(MemberInvalid):
        create_invite(db, workspace=workspace, email="골뱅이없음")
