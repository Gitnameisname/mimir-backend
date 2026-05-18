"""S3 Phase 6 FG 6-1: rate-limit 일괄 적용 회귀.

회귀 시나리오:
  L1. annotations / contributors / notifications 라우터 핸들러가 limiter 마크 가짐.
  L2. _ANNOTATION_WRITE_LIMIT / _ANNOTATION_READ_LIMIT 등 상수가 plan §2 와 일치.
  L3. notifications polling limit (120/min) 가 plan 과 일치.
"""
from __future__ import annotations


def _has_limit_marker(fn) -> bool:
    """slowapi @limiter.limit 가 적용되면 함수에 _rate_limit 또는 wrapper 가 생긴다.

    slowapi 는 ``__wrapped__`` 와 함수 closure 안 limit 등록을 사용. 단순히 함수
    이름 외에 추가 attribute 또는 wrapper 가 부착되었는지 확인.
    """
    # slowapi 는 decorator 적용 시 wrapper 함수 (async_wrapper / sync_wrapper) 로 래핑.
    return hasattr(fn, "__wrapped__") or fn.__name__ in ("async_wrapper", "sync_wrapper")


def test_annotations_router_handlers_rate_limited():
    from app.api.v1 import annotations as ann_mod
    # 모듈 내 핸들러 직접 검증.
    assert _has_limit_marker(ann_mod.create_annotation)
    assert _has_limit_marker(ann_mod.list_annotations)
    assert _has_limit_marker(ann_mod.get_annotation)
    assert _has_limit_marker(ann_mod.update_annotation)
    assert _has_limit_marker(ann_mod.resolve_annotation)
    assert _has_limit_marker(ann_mod.reopen_annotation)
    assert _has_limit_marker(ann_mod.delete_annotation)


def test_contributors_router_handler_rate_limited():
    from app.api.v1 import contributors
    assert _has_limit_marker(contributors.get_contributors)


def test_notifications_router_handlers_rate_limited():
    from app.api.v1 import notifications
    assert _has_limit_marker(notifications.list_notifications)
    assert _has_limit_marker(notifications.unread_count)
    assert _has_limit_marker(notifications.mark_read)


def test_annotation_limits_match_plan():
    from app.api.v1 import annotations as ann_mod
    # Phase 6 §2 표: 30/min 쓰기, 60/min 읽기.
    assert ann_mod._ANNOTATION_WRITE_LIMIT == "30/minute"
    assert ann_mod._ANNOTATION_READ_LIMIT == "60/minute"


def test_notifications_polling_limit_matches_plan():
    from app.api.v1 import notifications
    assert notifications._NOTIFICATIONS_POLL_LIMIT == "120/minute"


def test_contributors_limit_matches_plan():
    from app.api.v1 import contributors
    assert contributors._CONTRIBUTORS_LIMIT == "60/minute"
