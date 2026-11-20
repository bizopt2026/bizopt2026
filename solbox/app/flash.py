"""저장·삭제 뒤 다음 화면에 한 번 보여 줄 짧은 확인 문구 (세션에 잠깐 담아 둔다)."""
from __future__ import annotations

from fastapi import Request

KEY = "flash"


def flash(request: Request, text: str) -> None:
    request.session[KEY] = text


def pop_flash(request: Request) -> str | None:
    """base.html 이 전체 화면을 그릴 때만 꺼낸다 (HTMX 조각 응답에서는 꺼내지 않는다)."""
    if KEY not in request.session:
        return None
    return request.session.pop(KEY)
