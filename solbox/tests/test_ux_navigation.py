"""현재 위치 표시: 상단 메뉴의 aria-current 와 화면 제목 위 경로."""
import re

from app.models import Customer, Partner, ResearchRun, Solution
from tests.conftest import refining_proposal


def _nav(html):
    return re.search(r"<nav>.*?</nav>", html, re.S).group(0)


def test_top_menu_marks_current_section(user_client):
    assert 'href="/" aria-current="page"' in _nav(user_client.get("/").text)
    nav = _nav(user_client.get("/solutions").text)
    assert 'href="/solutions" aria-current="page"' in nav
    assert nav.count("aria-current") == 1


def test_detail_page_belongs_to_its_section(user_client, db):
    c = Customer(name="한빛")
    db.add(c)
    db.commit()
    nav = _nav(user_client.get(f"/customers/{c.id}").text)
    assert 'href="/customers" aria-current="page"' in nav


def test_admin_menu_is_current_under_admin(admin_client):
    html = admin_client.get("/admin/research").text
    assert '<summary aria-current="page">관리</summary>' in html
    assert 'href="/admin/research" aria-current="page"' in html


def _crumbs(html):
    m = re.search(r'<nav class="breadcrumb" aria-label="경로">(.*?)</nav>', html, re.S)
    assert m, "경로가 없다"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def test_breadcrumbs_on_detail_and_form_pages(user_client, db):
    s = Solution(name="(예시) AI 공조", created_by_id=1)
    db.add(s)
    db.commit()
    html = user_client.get(f"/solutions/{s.id}").text
    assert _crumbs(html) == "솔루션 › (예시) AI 공조"
    assert '<a href="/solutions">솔루션</a>' in html
    assert "&larr; 목록" not in html
    assert _crumbs(user_client.get(f"/solutions/{s.id}/edit").text) == "솔루션 › (예시) AI 공조 › 수정"
    assert _crumbs(user_client.get("/solutions/new").text) == "솔루션 › 등록"
    assert _crumbs(user_client.get("/customers/new").text) == "고객 › 등록"
    assert _crumbs(user_client.get("/activities/new").text) == "활동 기록 › 등록"


def test_breadcrumb_escapes_names(user_client, db):
    p = Partner(name="<b>x</b>")
    db.add(p)
    db.commit()
    html = user_client.get(f"/partners/{p.id}").text
    assert "<b>x</b>" not in _crumbs(html) and "&lt;b&gt;" in html


def test_list_pages_have_no_breadcrumb(user_client):
    assert "breadcrumb" not in user_client.get("/solutions").text


def test_proposal_outline_research_and_admin_breadcrumbs(admin_client, db):
    assert _crumbs(admin_client.get("/admin/research").text) == "관리 › 리서치 설정"
    assert _crumbs(admin_client.get("/admin/users").text) == "관리 › 사용자"
    run = ResearchRun(status="done", trigger="manual")
    db.add(run)
    db.commit()
    day = run.started_at.strftime("%Y-%m-%d")
    assert _crumbs(admin_client.get(f"/research/runs/{run.id}").text) == f"리서치 › {day}"


def test_outline_breadcrumb_links_back_to_the_proposal(user_client, db, settings):
    p = refining_proposal(db, settings)
    p.outline = {"title": "t", "slides": [{"kind": "cover", "title": "표지"}]}
    db.commit()
    html = user_client.get(f"/proposals/{p.id}/outline").text
    assert _crumbs(html).startswith("제안 › ") and _crumbs(html).endswith("› 구성안")
    assert f'<a href="/proposals/{p.id}">' in html
