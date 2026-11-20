"""화면 다듬기: 저장 확인 문구, 현재 메뉴, 발굴 단계 스텝, 제안 단계, 위험 버튼, 연결 체크 목록."""
from app.models import Customer, Solution

from tests.conftest import make_proposal_world, outlined_proposal, refining_proposal


def test_flash_shows_once_after_save(user_client, db):
    r = user_client.post("/customers", data={"name": "한빛병원"})
    assert r.status_code == 200 and 'class="flash"' in r.text and "고객을 등록했습니다." in r.text
    again = user_client.get(r.url.path).text
    assert "고객을 등록했습니다." not in again


def test_flash_not_consumed_by_htmx_fragment(user_client, db, settings):
    p, *_ = make_proposal_world(db, settings)
    user_client.post(f"/proposals/{p.id}/target", data={"request_memo": "바뀐 메모"}, follow_redirects=False)
    # 조각 응답(상태 조회)은 base.html 을 쓰지 않으므로 문구를 꺼내지 않는다
    user_client.get(f"/proposals/{p.id}/status")
    assert "대상을 저장했습니다." in user_client.get(f"/proposals/{p.id}").text


def test_nav_marks_current_section(user_client, db):
    page = user_client.get("/solutions").text
    assert '<a href="/solutions" aria-current="page">솔루션</a>' in page
    assert '<a href="/" aria-current="page">' not in page
    assert '<a href="/" aria-current="page">홈</a>' in user_client.get("/").text


def test_admin_menu_marked_current(admin_client, db):
    page = admin_client.get("/admin/users").text
    assert 'class="menu current"' in page and '<a href="/admin/users" aria-current="page">사용자</a>' in page


def test_solution_stage_stepper_and_hold(admin_client, db):
    user_client = admin_client  # 삭제 버튼은 만든 사람·관리자에게만 보인다
    s = Solution(name="드론 점검", stage="tapping"); db.add(s); db.commit()
    page = user_client.get(f"/solutions/{s.id}").text
    assert 'class="current" aria-current="step"' in page
    assert page.count('class="done"') == 1  # 아이템 접수만 지남
    assert 'name="stage" value="hold"' in page and "보류로 돌리기" in page
    assert 'class="danger"' in page  # 삭제는 위험 버튼
    r = user_client.post(f"/solutions/{s.id}/stage", data={"stage": "hold"})
    assert "발굴 단계를 바꿨습니다." in r.text and "보류</strong> 상태입니다" in r.text


def test_proposal_flow_steps(admin_client, db, settings):
    user_client = admin_client
    p = refining_proposal(db, settings)
    page = user_client.get(f"/proposals/{p.id}").text
    assert 'class="card step-section is-done" id="directions"' in page
    assert 'class="card step-section is-current" id="chat" open' in page
    assert 'class="card step-section is-upcoming" id="outline" open' in page
    assert "선택: 전력비 절감 패키지" in page
    assert 'class="danger-zone"' in page


def test_outlined_proposal_marks_outline_current(user_client, db, settings):
    p = outlined_proposal(db, settings)
    page = user_client.get(f"/proposals/{p.id}").text
    assert 'is-current" id="outline"' in page and 'is-done" id="chat"' in page and "· 8장" in page


def test_activity_form_uses_checkbox_pickers(user_client, db):
    c = Customer(name="한빛병원"); db.add(c); db.commit()
    page = user_client.get("/activities/new").text
    assert "파일 선택" in page and 'name="memo_file"' in page
    from app.models import Activity
    a = Activity(kind="meeting", title="t", raw_text="메모"); db.add(a); db.commit()
    edit = user_client.get(f"/activities/{a.id}/edit").text
    assert f'type="checkbox" name="customer_ids" value="{c.id}"' in edit and "<select name=\"customer_ids\"" not in edit


def test_solution_list_tag_chips_link_to_search(user_client, db):
    db.add(Solution(name="에너지 BEMS", tags="에너지,AI")); db.commit()
    page = user_client.get("/solutions").text
    assert 'class="chip" href="/solutions?q=%EC%97%90%EB%84%88%EC%A7%80"' in page
    assert 'class="sortable stack-sm"' in page and "전체 1건" in page
