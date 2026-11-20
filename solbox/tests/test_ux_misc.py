"""빈 값 표시, 오류 화면 제목, 시스템 화면의 긴 경로."""
import re

import pytest

from app.models import Customer, Solution


def test_empty_detail_values_show_none(user_client, db):
    s = Solution(name="A", summary="소개", created_by_id=1)
    db.add(s)
    db.commit()
    html = user_client.get(f"/solutions/{s.id}").text
    dl = re.search(r"<dl>.*?</dl>", html, re.S).group(0)
    assert dl.count('<span class="muted">없음</span>') == 5   # 설명, 파트너, 활용 방향, 유형, 태그
    assert "소개" in dl


def test_customer_and_activity_empty_values(user_client, db):
    c = Customer(name="C")
    db.add(c)
    db.commit()
    assert user_client.get(f"/customers/{c.id}").text.count('<span class="muted">없음</span>') >= 3


@pytest.mark.parametrize("url, title", [
    ("/solutions/9999", "항목을 찾을 수 없습니다"),
    ("/admin/users", "권한이 없습니다"),
])
def test_error_title_follows_status(user_client, url, title):
    r = user_client.get(url)
    assert f"<h1>{title}</h1>" in r.text
    assert 'href="/"' in r.text


def test_error_title_400_and_other(user_client, admin_client):
    r = user_client.post("/solutions/9999/stage", data={"stage": "nope"})
    assert "<h1>항목을 찾을 수 없습니다</h1>" in r.text  # 404 가 먼저
    from app.main import error_title
    assert error_title(400) == "요청을 확인해 주세요"
    assert error_title(500) == "요청을 처리하지 못했습니다"
    assert error_title(403) == "권한이 없습니다"


def test_system_shows_long_paths_in_a_wrapping_code_block(admin_client):
    html = admin_client.get("/admin/system").text
    assert 'class="path"' in html
