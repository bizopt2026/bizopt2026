"""목록 화면: 제목 줄(건수·등록 버튼), 페이지 나눔, 빈 화면 문구."""
import re

import pytest

from app.models import Activity, Customer, Solution
from app.routers._common import paginate


def test_paginate_slices_and_clamps():
    items = list(range(120))
    p = paginate(items, "1")
    assert (p.page, p.pages, p.total, len(p.items)) == (1, 3, 120, 50)
    assert paginate(items, "3").items == list(range(100, 120))
    for bad in ("0", "4", "-1", "abc", "", None, "2.5"):
        assert paginate(items, bad).page == 1
    assert paginate(items, 2).page == 2
    assert paginate([], "5").pages == 1 and paginate([], "5").items == []
    assert paginate(items, "2", per_page=100).items == items[100:]


def test_paginate_exact_multiple():
    p = paginate(list(range(100)), "2")
    assert p.pages == 2 and len(p.items) == 50 and p.has_pages
    assert not paginate(list(range(50)), "1").has_pages


def test_solution_list_header_count_and_register_button(user_client, db):
    for i in range(3):
        db.add(Solution(name=f"S{i}", created_by_id=1))
    db.commit()
    html = user_client.get("/solutions").text
    assert '<span class="count">3건</span>' in html
    assert 'class="button primary" href="/intake/new">올리기</a>' in html and 'href="/solutions/new">직접 입력</a>' in html
    assert '<span class="count">검색 결과 1건</span>' in user_client.get("/solutions?q=S1").text


def test_empty_states(user_client, db):
    html = user_client.get("/solutions").text
    assert "아직 등록된 솔루션이 없습니다." in html and "<table" not in html
    db.add(Solution(name="A", created_by_id=1))
    db.commit()
    assert "조건에 맞는 항목이 없습니다." in user_client.get("/solutions?q=zzz").text
    assert "아직 등록된 고객이 없습니다." in user_client.get("/customers").text
    assert "아직 등록된 제안 건이 없습니다." in user_client.get("/proposals").text


def test_pagination_links_keep_filters(user_client, db):
    for i in range(120):
        db.add(Customer(name=f"고객{i:03d}"))
    db.commit()
    html = user_client.get("/customers?q=고객&page=2").text
    assert html.count("<tr>") == 51  # 머리 1 + 50
    assert "2 / 3" in html
    assert re.search(r'href="/customers\?q=%EA%B3%A0%EA%B0%9D&amp;page=1"[^>]*>이전', html)
    assert re.search(r'href="/customers\?q=%EA%B3%A0%EA%B0%9D&amp;page=3"[^>]*>다음', html)
    assert "고객050" in html and "고객000" not in html


def test_no_pager_for_small_lists(user_client, db):
    db.add(Customer(name="a"))
    db.commit()
    assert 'class="pager"' not in user_client.get("/customers").text


def test_out_of_range_page_falls_back_to_first(user_client, db):
    for i in range(60):
        db.add(Activity(raw_text=f"m{i}", title=f"t{i}"))
    db.commit()
    for bad in ("9", "x", "0"):
        assert "1 / 2" in user_client.get(f"/activities?page={bad}").text
