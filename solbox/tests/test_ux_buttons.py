"""버튼 위계: 수정은 보조 모양의 링크, 삭제는 위험 모양, 저장·만들기는 주 동작."""
import re

from app.models import Solution


def test_detail_head_puts_edit_then_delete_in_title_row(user_client, db):
    s = Solution(name="AI 에너지 절감", created_by_id=1)
    db.add(s)
    db.commit()
    html = user_client.get(f"/solutions/{s.id}").text
    head = re.search(r'<div class="page-head">(.*?)</div>\s*</div>', html, re.S).group(1)
    assert "<h1>AI 에너지 절감</h1>" in head
    assert head.index(f'class="button" href="/solutions/{s.id}/edit"') < head.index('class="danger"')


def test_forms_have_one_primary_button(user_client):
    html = user_client.get("/customers/new").text
    assert html.count('class="primary"') == 1
    assert 'class="primary">저장' in html


def test_run_now_is_primary_on_research_settings(admin_client):
    assert 'class="primary">지금 조사' in admin_client.get("/admin/research").text


def test_each_form_has_exactly_one_primary(user_client, db):
    from app.models import Customer
    s = Solution(name="S", created_by_id=1)
    db.add_all([s, Customer(name="C")])
    db.commit()
    for url in ("/customers/new", f"/solutions/{s.id}/edit", "/activities/new", "/proposals/new"):
        html = user_client.get(url).text
        card = re.search(r'<form[^>]*class="card stack".*?</form>', html, re.S).group(0)
        assert card.count('class="primary"') + card.count(' primary"') == 1, url
    from fastapi.testclient import TestClient
    login = TestClient(user_client.app).get("/login").text
    assert login.count('class="primary"') == 1


def test_chat_section_has_one_primary_before_and_after_outline(user_client, db, settings):
    from tests.conftest import refining_proposal
    p = refining_proposal(db, settings)
    chat = lambda: re.search(r'<details class="card section" id="chat".*?</details>', user_client.get(f"/proposals/{p.id}").text, re.S).group(0)
    before = chat()
    assert before.count('class="primary"') == 1 and 'class="primary">제안서 만들기' in before
    p.outline = {"title": "t", "slides": [{"kind": "cover", "title": "표지"}]}
    db.commit()
    after = chat()
    assert after.count('class="primary"') == 1 and 'class="primary">보내기' in after
