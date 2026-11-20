"""좁은 화면: 모든 표는 .tablewrap 안에 있고, 좁은 화면 규칙이 stylesheet 에 있다."""
import re
from datetime import date

from app.models import Activity, Customer, Partner, Solution
from tests.conftest import refining_proposal


def _unwrapped_tables(html):
    return [m.start() for m in re.finditer(r"<table", html) if not html[:m.start()].endswith('<div class="tablewrap">')]


def test_every_table_is_inside_a_tablewrap(admin_client, db, settings):
    p = refining_proposal(db, settings)
    p.outline = {"title": "t", "slides": [
        {"kind": "cover", "title": "표지"},
        {"kind": "table", "title": "표", "columns": ["a", "b"], "rows": [["1", "2"]]}]}
    s = Solution(name="S", created_by_id=1)
    db.add_all([s, Partner(name="P"), Customer(name="C"), Activity(raw_text="x", title="t", date=date.today())])
    db.commit()
    urls = ["/", "/solutions", "/customers", "/businesses", "/partners", "/proposals", "/activities", "/research",
            f"/solutions/{s.id}", f"/proposals/{p.id}/outline", "/admin/users", "/admin/jobs", "/admin/research", "/admin/system"]
    for url in urls:
        html = admin_client.get(url).text
        assert _unwrapped_tables(html) == [], url


def test_stylesheet_has_narrow_screen_rules(client):
    css = client.get("/static/app.css").text
    assert "@media (max-width: 900px)" in css and ".tablewrap" in css and "white-space:nowrap" in css


def test_topbar_is_sticky_and_menu_closes_on_outside_click_and_escape(admin_client):
    css = admin_client.get("/static/app.css").text
    assert re.search(r"\.topbar \{ position:sticky; top:0; z-index:10;", css)
    html = admin_client.get("/").text
    assert 'details.menu[open]' in html and '"Escape"' in html and 'addEventListener("click"' in html


def test_narrow_menu_cue_and_tap_target_rules():
    from app.main import APP_DIR
    css = (APP_DIR / "static" / "app.css").read_text(encoding="utf-8")
    js = (APP_DIR / "static" / "app.js").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 900px)"):]
    assert ".topbar nav::after" in narrow and "linear-gradient" in narrow and ".topbar nav.at-end::after" in narrow
    assert "min-height:36px" in css.split("좁은 화면: 누르는 곳은")[1]
    menu_js = js.split("상단 메뉴")[1].split("체크 상자 목록")[0]
    assert "topNav.scrollLeft" in menu_js and "scrollIntoView" not in menu_js
