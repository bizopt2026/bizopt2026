"""E3: 구성안 카드 열림 규칙, 장별 모두 확인함, 출처 번호, 방향 카드 묶음, 상단 메뉴 표시."""
import re

from app.proposals import slide_sources
from tests.conftest import make_user, outlined_proposal


def _page(client, p):
    return client.get(f"/proposals/{p.id}/outline").text


def _open_cards(page):
    return [int(m.group(1)) for m in re.finditer(r'<details class="card slide-card[^"]*" id="slide-(\d+)"[^>]*? open', page)]


def _mark(db, p, notes_by_index, checked_by_index=None):
    slides = [dict(s) for s in p.outline["slides"]]
    for i, notes in notes_by_index.items():
        slides[i]["needs_check"] = notes
        slides[i]["checked"] = (checked_by_index or {}).get(i, [])
    p.outline = {**p.outline, "slides": slides}
    db.commit()


def test_first_unresolved_slide_opens_by_default(user_client, db, settings):
    p = outlined_proposal(db, settings)  # 2번 장(index 2)만 확인할 점이 있다
    _mark(db, p, {5: ["값 하나", "값 둘"]})
    page = _page(user_client, p)
    assert _open_cards(page) == [2]  # 위에서부터 처음 남은 장만


def test_next_unresolved_opens_when_first_is_checked(user_client, db, settings):
    p = outlined_proposal(db, settings)
    _mark(db, p, {2: ["a"], 5: ["b"]}, {2: ["a"]})
    assert _open_cards(_page(user_client, p)) == [5]


def test_all_collapsed_when_nothing_is_left(user_client, db, settings):
    p = outlined_proposal(db, settings)
    _mark(db, p, {2: ["a"]}, {2: ["a"]})
    assert _open_cards(_page(user_client, p)) == []


def test_open_param_wins_over_default(user_client, db, settings):
    p = outlined_proposal(db, settings)
    _mark(db, p, {5: ["b"]})
    page = user_client.get(f"/proposals/{p.id}/outline?open=1").text
    assert _open_cards(page) == [1]


def test_slide_badges_use_check_wording(user_client, db, settings):
    p = outlined_proposal(db, settings)
    _mark(db, p, {1: ["근거 자료 없음: x", "값 a", "값 b"], 3: ["c"]}, {3: ["c"]})
    page = _page(user_client, p)
    s = re.findall(r'<summary class="slide-summary">(.*?)</summary>', page, re.S)
    assert "근거 문제 1" in s[1] and "확인할 점 2" in s[1]
    assert "확인 끝" in s[3] and "확인할 점" not in s[3]


def test_check_all_requires_login(client, db, settings):
    p = outlined_proposal(db, settings)
    _mark(db, p, {2: ["a", "b"]})
    r = client.post(f"/proposals/{p.id}/outline/slides/2/check-all", data={"done": "1"}, follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == []


def test_check_all_any_logged_in_user_and_idempotent(app, db, settings):
    from tests.conftest import _logged_in_client
    p = outlined_proposal(db, settings)
    _mark(db, p, {2: ["a", "b", "a"]})
    other = _logged_in_client(app, db, "lee", "user")  # 개별 확인 경로와 같은 규칙: 로그인한 사용자 누구나
    url = f"/proposals/{p.id}/outline/slides/2/check-all"
    assert other.post(url, data={"done": "1"}).status_code == 200
    db.refresh(p)
    first = list(p.outline["slides"][2]["checked"])
    assert first == ["a", "b"]
    other.post(url, data={"done": "1"})
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == first
    assert all(s["checked"] == [] for i, s in enumerate(p.outline["slides"]) if i != 2)


def test_slide_sources_dedupe_and_order():
    assert slide_sources({"sources": ["b.md", "a.md", "b.md", 5, "c.md"]}) == ["b.md", "a.md", "c.md"]
    assert slide_sources({}) == []


def test_slide_card_numbers_sources_once(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[2]["sources"] = ["solutions/1/card.md", "solutions/1/card.md", "solutions/99/card.md"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = _page(user_client, p)
    body = page.split('id="slide-2"')[1].split("</details>")[0]
    assert body.count('<li id="s2-src-') == 2 and 'id="s2-src-1"' in body and 'id="s2-src-2"' in body
    assert body.index('id="s2-src-1"') < body.index('id="s2-src-2"')
    assert 'id="s3-src-1"' in page.split('id="slide-3"')[1]  # 번호는 장마다 새로 시작


def test_slide_without_sources_shows_red_marker(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[3]["sources"] = []
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = _page(user_client, p)
    assert 'class="badge evidence">출처 없음' in page.split('id="slide-3"')[1].split("</details>")[0]
    assert "출처 없음" not in page.split('id="slide-0"')[1].split('id="slide-1"')[0]  # 표지는 해당 없음


def _directions_page(user_client, db, p):
    p.selected_index, p.status = None, "directions"
    db.commit()
    return user_client.get(f"/proposals/{p.id}").text


def test_direction_without_source_keeps_inline_marker(user_client, db, settings):
    p = outlined_proposal(db, settings)
    d = dict(p.directions[0], rationale=[{"text": "근거 있음", "sources": ["solutions/1/card.md"]}, {"text": "근거 없음 문장", "sources": []}])
    p.directions = [d, p.directions[1]]
    page = _directions_page(user_client, db, p)
    assert re.search(r"근거 없음 문장\s*<span class=\"badge evidence\">출처 없음</span>", page)
    assert '<sup><a href="#d0-src-1">1</a></sup>' in page


def test_direction_anchors_are_per_card(user_client, db, settings):
    p = outlined_proposal(db, settings)
    ev = [{"text": "t", "sources": ["solutions/1/card.md"]}]
    p.directions = [dict(p.directions[0], rationale=ev), dict(p.directions[1], rationale=ev)]
    page = _directions_page(user_client, db, p)
    assert 'id="d0-src-1"' in page and 'id="d1-src-1"' in page and page.count('id="d0-src-1"') == 1


def test_direction_summary_markup(user_client, db, settings):
    p = outlined_proposal(db, settings)
    page = _directions_page(user_client, db, p)
    assert page.count("근거와 확인할 점 보기") == len(p.directions)
    assert re.search(r'class="dir-grid" style="--cols:2"', page)
    css = user_client.get("/static/app.css").text
    assert "@media (min-width: 1100px)" in css and "align-items:start" in css.split("제안 방향 카드")[1].split("}")[0]


def test_nav_marks_active_and_css_gives_hint(user_client):
    page = user_client.get("/proposals").text
    assert re.search(r'<a href="/proposals" aria-current="page">제안</a>', page)
    css = user_client.get("/static/app.css").text
    tail = css.split("상단 메뉴 항목은 누르기 쉽게")[1]
    assert "@media (max-width: 480px)" in tail and ".topbar nav > a" in tail and "min-height:40px" in tail
