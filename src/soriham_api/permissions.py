"""누가 무엇을 할 수 있는가.

FastAPI를 임포트하지 않는다 — 순수 함수라 세션 하나로 단위 시험이 되고, 라우트가
바뀌어도 이 규칙은 그대로 남는다.

해석은 **독립적인 권한들의 최댓값**이다. 거부 규칙도 우선 재정의도 없다. 아래 순서는
질의를 아끼기 위한 단축 평가일 뿐 재정의 의미가 아니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import IntEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Recording, RecordingShare, User, WorkspaceMember

logger = logging.getLogger(__name__)


class Perm(IntEnum):
    NONE = 0
    VIEW = 1  # 목록·상세·검색·오디오
    EDIT = 2  # 제목·화자 이름·태그
    MANAGE = 3  # 업로드·삭제·공유 발급과 철회
    ADMIN = 4  # 워크스페이스 설정·구성원 관리


ROLE_PERMS: dict[str, Perm] = {
    "owner": Perm.ADMIN,
    "admin": Perm.ADMIN,
    "member": Perm.MANAGE,
    "viewer": Perm.VIEW,
}
# 공유로는 MANAGE를 얻지 못한다 — 공유받은 사람이 다시 공유할 수 없다는 뜻이다
SHARE_PERMS: dict[str, Perm] = {"view": Perm.VIEW, "edit": Perm.EDIT}


@dataclass(frozen=True)
class Principal:
    """요청을 낸 쪽.

    로그인 사용자와 비로그인 링크 열람자를 같은 타입으로 다룬다. 둘을 다른 타입으로
    두면 권한을 보는 자리마다 분기가 생기고, 그 분기 하나를 빠뜨리면 그대로 구멍이다.
    """

    user_id: int | None = None
    # 승인 대기·거절·중지된 계정은 스스로 얻은 권한이 전부 죽는다. 기본값이 False인
    # 것은 잠긴 쪽이 기본이기 때문이다 — 새 호출부가 이 값을 빠뜨리면 열리는 대신 막힌다
    is_active: bool = False
    is_service_admin: bool = False
    # 링크로 들어온 경우: 그 링크가 가리키는 녹음 하나에만 열람 권한이 생긴다
    link_recording_id: int | None = None
    # 잠금 기본값은 잠긴 쪽이다. 링크를 만들 때 값을 옮기는 걸 빠뜨리면 열려 버리는
    # 것보다 닫혀 버리는 편이 낫다
    link_allows_audio: bool = False
    link_allows_speaker_names: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.user_id is not None

    @classmethod
    def anonymous(cls) -> Principal:
        return cls()

    @classmethod
    def for_user(cls, user: User) -> Principal:
        return cls(
            user_id=user.id,
            is_active=user.status == "active",
            is_service_admin=user.is_service_admin,
        )

    @property
    def has_own_access(self) -> bool:
        """스스로 권한을 가질 수 있는 상태인가. 링크로 받는 권한은 여기 걸리지 않는다."""
        return self.user_id is not None and self.is_active


def resolve_workspace_perm(db: Session, principal: Principal, workspace_id: int) -> Perm:
    """워크스페이스 단위 권한. 링크 열람자는 워크스페이스에 아무 권한이 없다."""
    if not principal.has_own_access:
        return Perm.NONE
    if principal.is_service_admin:
        return Perm.ADMIN
    role = db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == principal.user_id,
        )
    )
    return ROLE_PERMS.get(role, Perm.NONE) if role else Perm.NONE


def resolve_recording_perm(db: Session, principal: Principal, recording: Recording) -> Perm:
    """녹음 하나에 대한 유효 권한. 모든 경로의 최댓값이다.

    계정이 활성이 아니면 스스로 얻은 권한은 전부 없는 것으로 본다. 링크로 받는
    VIEW는 남는다 — 링크는 누구에게나 열리는 것이고, 중지된 계정이라고 남들보다
    적게 볼 이유는 없다.
    """
    if principal.is_service_admin and principal.is_active:
        # 이것이 결정 근거가 되는 경우는 드물어야 한다 — 드물지 않으면 역할이 잘못된 것
        logger.warning(
            "서비스 관리자 권한으로 접근: user_id=%s recording=%s",
            principal.user_id,
            recording.public_id,
        )
        return Perm.ADMIN

    best = Perm.NONE
    if principal.link_recording_id is not None and principal.link_recording_id == recording.id:
        best = Perm.VIEW
    if not principal.has_own_access:
        return best

    role = db.scalar(
        select(WorkspaceMember.role).where(
            WorkspaceMember.workspace_id == recording.workspace_id,
            WorkspaceMember.user_id == principal.user_id,
        )
    )
    if role:
        best = max(best, ROLE_PERMS.get(role, Perm.NONE))

    shared = db.scalar(
        select(RecordingShare.permission).where(
            RecordingShare.recording_id == recording.id,
            RecordingShare.user_id == principal.user_id,
        )
    )
    if shared:
        best = max(best, SHARE_PERMS.get(shared, Perm.NONE))
    return best


def resolve_own_perm(db: Session, principal: Principal, recording: Recording) -> Perm:
    """링크를 빼고 스스로 얻은 권한.

    링크의 잠금이 누구에게 적용되는지 가르는 값이다. 로그인 여부로 가르면 안 된다 —
    승인제라 링크를 받는 사람은 거의 다 로그인 상태이고, 그러면 잠금이 아무도 못 막는다.
    """
    return resolve_recording_perm(db, replace(principal, link_recording_id=None), recording)


def can_play_audio(principal: Principal, own_perm: Perm) -> bool:
    """오디오를 들려줄지. `own_perm`은 링크를 뺀 자기 권한이다.

    전사만 넘기고 목소리는 넘기지 않는 것이 회의 녹음에서는 흔한 요구다. 스스로
    볼 권한이 있는 사람에게는 링크의 잠금이 의미가 없다 — 어차피 다른 길로 듣는다.
    """
    if own_perm >= Perm.VIEW:
        return True
    if principal.link_recording_id is not None:
        return principal.link_allows_audio
    return False


def can_see_speaker_names(principal: Principal, own_perm: Perm) -> bool:
    """화자 이름은 소유자가 손으로 넣은 실명이라 링크마다 따로 고른다."""
    if own_perm >= Perm.VIEW:
        return True
    if principal.link_recording_id is not None:
        return principal.link_allows_speaker_names
    return False
