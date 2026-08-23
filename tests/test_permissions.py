from __future__ import annotations

from dataclasses import replace

import pytest

from soriham_api import auth
from soriham_api.models import Recording, RecordingShare
from soriham_api.permissions import (
    Perm,
    Principal,
    can_play_audio,
    can_see_speaker_names,
    resolve_own_perm,
    resolve_recording_perm,
    resolve_workspace_perm,
)
from soriham_api.tenancy import add_member, create_user


def make_user(db, email: str, *, admin: bool = False):
    user = create_user(
        db,
        email=email,
        password_hash=auth.hash_password("암구호"),
        display_name=email.split("@")[0],
        status="active",
        is_service_admin=admin,
    )
    db.flush()
    return user


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


@pytest.fixture
def rec(db, workspace):
    return make_recording(db, workspace)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("owner", Perm.ADMIN),
        ("admin", Perm.ADMIN),
        ("member", Perm.MANAGE),
        ("viewer", Perm.VIEW),
    ],
)
def test_역할이_권한_등급을_정한다(db, workspace, rec, role, expected):
    user = make_user(db, f"{role}@example.com")
    add_member(db, workspace, user, role)
    assert resolve_recording_perm(db, Principal.for_user(user), rec) == expected


def test_남의_워크스페이스_녹음에는_아무_권한이_없다(db, workspace, other_workspace, rec):
    outsider = make_user(db, "outsider@example.com")
    add_member(db, other_workspace, outsider, "owner")
    assert resolve_recording_perm(db, Principal.for_user(outsider), rec) == Perm.NONE


def test_비로그인_주체는_아무_권한이_없다(db, rec):
    assert resolve_recording_perm(db, Principal.anonymous(), rec) == Perm.NONE


@pytest.mark.parametrize(("permission", "expected"), [("view", Perm.VIEW), ("edit", Perm.EDIT)])
def test_공유받은_권한은_고른_대로_붙는다(db, workspace, rec, permission, expected):
    guest = make_user(db, "guest@example.com")
    db.add(RecordingShare(recording_id=rec.id, user_id=guest.id, permission=permission))
    db.flush()
    assert resolve_recording_perm(db, Principal.for_user(guest), rec) == expected


def test_공유받은_사람은_다시_공유할_수_없다(db, workspace, rec):
    """EDIT가 상한이다. MANAGE가 붙으면 공유가 무한히 번진다."""
    guest = make_user(db, "guest@example.com")
    db.add(RecordingShare(recording_id=rec.id, user_id=guest.id, permission="edit"))
    db.flush()
    assert resolve_recording_perm(db, Principal.for_user(guest), rec) < Perm.MANAGE


def test_공유는_그_녹음_하나에만_붙는다(db, workspace, rec):
    other = make_recording(db, workspace, "b.wav")
    guest = make_user(db, "guest@example.com")
    db.add(RecordingShare(recording_id=rec.id, user_id=guest.id, permission="edit"))
    db.flush()
    principal = Principal.for_user(guest)
    assert resolve_recording_perm(db, principal, rec) == Perm.EDIT
    assert resolve_recording_perm(db, principal, other) == Perm.NONE


def test_여러_경로의_권한은_높은_쪽이_이긴다(db, workspace, rec):
    """구성원이면서 열람 공유도 받은 경우 — 공유가 역할을 끌어내리지 않는다."""
    user = make_user(db, "both@example.com")
    add_member(db, workspace, user, "member")
    db.add(RecordingShare(recording_id=rec.id, user_id=user.id, permission="view"))
    db.flush()
    assert resolve_recording_perm(db, Principal.for_user(user), rec) == Perm.MANAGE


def test_링크_주체는_그_녹음만_열람한다(db, workspace, rec):
    other = make_recording(db, workspace, "b.wav")
    principal = Principal(link_recording_id=rec.id)
    assert resolve_recording_perm(db, principal, rec) == Perm.VIEW
    assert resolve_recording_perm(db, principal, other) == Perm.NONE


def test_링크로는_고칠_수_없다(db, rec):
    assert resolve_recording_perm(db, Principal(link_recording_id=rec.id), rec) < Perm.EDIT


def test_로그인_상태로_링크를_열면_높은_쪽이_이긴다(db, workspace, rec):
    user = make_user(db, "member@example.com")
    add_member(db, workspace, user, "member")
    principal = replace(Principal.for_user(user), link_recording_id=rec.id)
    assert resolve_recording_perm(db, principal, rec) == Perm.MANAGE


def test_서비스_관리자는_어디든_본다(db, other_workspace, rec):
    admin = make_user(db, "admin@example.com", admin=True)
    assert resolve_recording_perm(db, Principal.for_user(admin), rec) == Perm.ADMIN


def test_링크_주체는_워크스페이스에_권한이_없다(db, workspace, rec):
    """목록·검색·통계로 새어 나가지 않게 하는 자리."""
    principal = Principal(link_recording_id=rec.id)
    assert resolve_workspace_perm(db, principal, workspace.id) == Perm.NONE


def test_워크스페이스_권한도_역할을_따른다(db, workspace, other_workspace):
    user = make_user(db, "member@example.com")
    add_member(db, workspace, user, "viewer")
    principal = Principal.for_user(user)
    assert resolve_workspace_perm(db, principal, workspace.id) == Perm.VIEW
    assert resolve_workspace_perm(db, principal, other_workspace.id) == Perm.NONE


def test_오디오를_잠근_링크는_전사만_보여준다(db, rec):
    """전사만 넘기고 목소리는 넘기지 않는 요구가 회의 녹음에서는 흔하다."""
    locked = Principal(link_recording_id=rec.id, link_allows_audio=False)
    open_link = Principal(link_recording_id=rec.id, link_allows_audio=True)
    assert can_play_audio(locked, Perm.NONE) is False
    assert can_play_audio(open_link, Perm.NONE) is True


def test_로그인만_했다고_링크의_잠금이_풀리지_않는다(db, workspace, rec):
    """승인제라 링크를 받는 사람은 거의 다 로그인 상태다. 로그인 여부로 가르면
    잠금이 아무도 못 막는다."""
    bystander = make_user(db, "bystander@example.com")
    principal = replace(
        Principal.for_user(bystander), link_recording_id=rec.id, link_allows_audio=False
    )
    own = resolve_own_perm(db, principal, rec)

    assert own == Perm.NONE
    assert can_play_audio(principal, own) is False
    assert can_see_speaker_names(principal, own) is False


def test_스스로_볼_권한이_있으면_링크_잠금과_무관하다(db, workspace, rec):
    """어차피 다른 길로 듣는 사람에게 링크의 잠금은 의미가 없다."""
    member = make_user(db, "member@example.com")
    add_member(db, workspace, member, "member")
    principal = replace(
        Principal.for_user(member), link_recording_id=rec.id, link_allows_audio=False
    )
    own = resolve_own_perm(db, principal, rec)

    assert own == Perm.MANAGE
    assert can_play_audio(principal, own) is True


def test_공유받은_사람도_링크_잠금과_무관하다(db, workspace, rec):
    guest = make_user(db, "guest@example.com")
    db.add(RecordingShare(recording_id=rec.id, user_id=guest.id, permission="view"))
    db.flush()
    principal = replace(
        Principal.for_user(guest), link_recording_id=rec.id, link_allows_speaker_names=False
    )
    own = resolve_own_perm(db, principal, rec)

    assert own == Perm.VIEW
    assert can_see_speaker_names(principal, own) is True


def test_링크를_뺀_권한이_링크_권한을_섞지_않는다(db, rec):
    """resolve_own_perm이 링크에서 온 VIEW를 지우지 못하면 잠금 판정이 전부 틀어진다."""
    principal = Principal(link_recording_id=rec.id)
    assert resolve_recording_perm(db, principal, rec) == Perm.VIEW
    assert resolve_own_perm(db, principal, rec) == Perm.NONE


def test_화자_이름_노출은_링크마다_고른다(db, rec):
    """화자 이름은 소유자가 손으로 넣은 실명이다."""
    hidden = Principal(link_recording_id=rec.id, link_allows_speaker_names=False)
    shown = Principal(link_recording_id=rec.id, link_allows_speaker_names=True)
    assert can_see_speaker_names(hidden, Perm.NONE) is False
    assert can_see_speaker_names(shown, Perm.NONE) is True


def test_링크도_권한도_없으면_아무것도_못_본다(db, rec):
    anon = Principal.anonymous()
    assert can_play_audio(anon, Perm.NONE) is False
    assert can_see_speaker_names(anon, Perm.NONE) is False


def test_잠금_기본값은_잠긴_쪽이다():
    """링크를 만들 때 값 옮기는 걸 빠뜨리면 열리는 것보다 닫히는 편이 낫다."""
    principal = Principal(link_recording_id=1)
    assert principal.link_allows_audio is False
    assert principal.link_allows_speaker_names is False


def test_활성이_아닌_계정은_스스로_얻은_권한이_전부_죽는다(db, workspace, rec):
    """중지·대기 계정이 워크스페이스 역할로 링크의 잠금을 우회하고 있었다.

    라우트 의존성이 계정 상태를 보지만 공유 링크는 그 의존성을 지나지 않는다.
    상태를 보는 자리가 규칙 함수 밖에 있으면 새 라우트마다 같은 구멍이 다시 생긴다.
    """
    member = make_user(db, "stopped@example.com")
    add_member(db, workspace, member, "owner")
    member.status = "disabled"
    db.flush()
    principal = Principal.for_user(member)

    assert resolve_recording_perm(db, principal, rec) == Perm.NONE
    assert resolve_workspace_perm(db, principal, workspace.id) == Perm.NONE


def test_활성이_아니면_서비스_관리자도_통하지_않는다(db, other_workspace, rec):
    admin = make_user(db, "stopped-admin@example.com", admin=True)
    admin.status = "disabled"
    db.flush()

    assert resolve_recording_perm(db, Principal.for_user(admin), rec) == Perm.NONE


def test_활성이_아닌_계정도_링크로는_남들만큼_본다(db, workspace, rec):
    """링크는 누구에게나 열린다. 중지된 계정이라고 익명보다 적게 볼 이유는 없다."""
    member = make_user(db, "pending@example.com")
    add_member(db, workspace, member, "owner")
    member.status = "pending"
    db.flush()
    principal = replace(
        Principal.for_user(member), link_recording_id=rec.id, link_allows_audio=False
    )

    assert resolve_recording_perm(db, principal, rec) == Perm.VIEW
    own = resolve_own_perm(db, principal, rec)
    assert own == Perm.NONE
    # 자기 권한이 죽었으므로 링크의 잠금이 이제 이 사람에게도 걸린다
    assert can_play_audio(principal, own) is False
