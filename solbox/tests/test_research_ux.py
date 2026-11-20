"""리서치 상세: 접히는 카드, 분야 탭, 내부 번호 숨기기."""
import re

from conftest import finished_run
from app.models import ResearchReport, Solution
from app.research import humanize_ids


def test_humanize_ids_replaces_known_forms(db):
    s = Solution(name="AI 공조", stage="boxed")
    db.add(s)
    db.commit()
    n = s.id
    assert humanize_ids(db, f"솔루션 (id {n})를 민다") == "솔루션 (AI 공조)를 민다"
    assert humanize_ids(db, f"id {n} 와 ID: {n}, Id:{n}") == "AI 공조 와 AI 공조, AI 공조"
    assert humanize_ids(db, "id 9999 는 없음") == "id 9999 는 없음"
    assert humanize_ids(db, f"valid {n} 그대로, id{n}0 그대로") == f"valid {n} 그대로, id{n}0 그대로"
    assert humanize_ids(db, "") == ""


def test_prompt_forbids_ids_in_body():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "app/ai/prompts/research_suggest.md").read_text(encoding="utf-8")
    assert "솔루션 번호를 쓰지 말고 이름으로 쓴다" in text and "`solution_ids` 에만" in text


def test_run_page_humanizes_suggestion_text(user_client, db, settings):
    run = finished_run(db, settings)
    sug = db.query(ResearchReport).filter_by(run_id=run.id, kind="suggest").one()
    sid = db.query(Solution).first().id
    sug.suggestions = [{**sug.suggestions[1], "title": f"밀기 (id {sid})", "detail": f"id {sid} 를 민다", "reason": f"ID: {sid} 수요"}]
    db.commit()
    page = user_client.get(f"/research/runs/{run.id}").text
    assert f"id {sid}" not in page and f"ID: {sid}" not in page
    assert "밀기 (AI 공조 최적제어)" in page


def test_run_page_cards_are_details_first_three_open(user_client, db, settings):
    run = finished_run(db, settings)
    sug = db.query(ResearchReport).filter_by(run_id=run.id, kind="suggest").one()
    sug.suggestions = [{**sug.suggestions[0], "title": f"제안{i}"} for i in range(5)]
    db.commit()
    page = user_client.get(f"/research/runs/{run.id}").text
    cards = re.findall(r'<details class="trend-card( suggestion)?"( open)?>', page)
    sugs = [c for c in cards if c[0]]
    assert len(sugs) == 5 and [bool(c[1]) for c in sugs] == [True, True, True, False, False]
    assert all(not c[1] for c in cards if not c[0])  # 동향 카드는 접힌 채
    assert "후보 등록" in page and "모두 펼치기" in page and "모두 접기" in page


def test_trend_summary_shows_link_count_and_needs_check(user_client, db, settings):
    run = finished_run(db, settings)
    page = user_client.get(f"/research/runs/{run.id}").text
    assert "링크 1개" in page and "링크 0개" in page
    summary = re.search(r"<summary[^>]*>[^<]*<strong>출처 없는 동향</strong>.*?</summary>", page, re.S).group(0)
    assert "근거 문제" in summary  # 출처 없음은 근거 문제(C1)


def test_area_tabs_markup_has_no_roles_without_script(user_client, db, settings):
    run = finished_run(db, settings)
    page = user_client.get(f"/research/runs/{run.id}").text
    tabs = re.search(r'<div class="tabs">(.*?)</div>', page, re.S).group(1)
    assert 'href="#tech"' in tabs and 'id="tab-tech"' in tabs and "기술" in tabs and "2" in tabs
    assert 'href="#policy"' in tabs and "실패" in tabs
    assert "role=" not in tabs and "aria-selected" not in tabs  # 역할은 스크립트가 붙인다
    assert 'id="tech"' in page and 'id="policy"' in page and 'role="tabpanel"' not in page


def test_page_loads_shared_script(user_client, db, settings):
    page = user_client.get("/proposals/new").text
    assert '<script src="/static/app.js" defer></script>' in page
    assert user_client.get("/static/app.js").status_code == 200


def test_insights_come_before_trends_in_area_panel(user_client, db, settings):
    run = finished_run(db, settings)
    page = user_client.get(f"/research/runs/{run.id}").text
    panel = page[page.index('id="tech"'):]
    assert panel.index("설비 운영 고객에 제안 기회") < panel.index("AI 공조 제어 확산")


def test_humanize_ids_drops_reference_right_after_the_name(db):
    s = Solution(name="AI 공조", stage="boxed")
    db.add(s)
    db.commit()
    assert humanize_ids(db, f"AI 공조(id {s.id})로 절감") == "AI 공조로 절감"
    assert humanize_ids(db, f"AI 공조 (id {s.id}) 로") == "AI 공조 로"


def _example(db):
    s = Solution(name="(예시) AI 공조 최적제어", stage="boxed")
    db.add(s)
    db.commit()
    return s.id


def test_humanize_ids_example_prefix_and_shortened_name(db):
    n = _example(db)
    assert humanize_ids(db, f"AI 공조 최적제어(id {n})로 절감") == "AI 공조 최적제어로 절감"
    assert humanize_ids(db, f"공조 최적제어 (id {n}) 로") == "공조 최적제어 로"
    assert humanize_ids(db, f"(예시) AI 공조 최적제어 (id {n})") == "(예시) AI 공조 최적제어"
    assert humanize_ids(db, f"그래서 센서(id {n})를 민다") == f"그래서 센서((예시) AI 공조 최적제어)를 민다"


def test_humanize_ids_unparenthesised_form(db):
    n = _example(db)
    assert humanize_ids(db, f"AI 공조 최적제어 id {n} 를 민다") == "AI 공조 최적제어 를 민다"
    assert humanize_ids(db, f"솔루션 id {n} 를 민다") == "솔루션 (예시) AI 공조 최적제어 를 민다"
    assert humanize_ids(db, f"ID: {n}") == "(예시) AI 공조 최적제어"


def test_humanize_ids_tolerates_non_strings(db):
    assert humanize_ids(db, None) == "" and humanize_ids(db, 5) == "5"


def test_run_page_humanizes_insights(user_client, db, settings):
    run = finished_run(db, settings)
    sid = db.query(Solution).first().id
    tech = db.query(ResearchReport).filter_by(run_id=run.id, area="tech").one()
    sug = db.query(ResearchReport).filter_by(run_id=run.id, kind="suggest").one()
    tech.insights, sug.insights = [f"솔루션 id {sid} 를 민다"], [f"ID: {sid} 우선"]
    db.commit()
    page = user_client.get(f"/research/runs/{run.id}").text
    assert f"id {sid}" not in page and f"ID: {sid}" not in page and "AI 공조 최적제어 우선" in page


def test_candidate_registration_hides_ids(user_client, db, settings):
    run = finished_run(db, settings)
    sid = db.query(Solution).first().id
    sug = db.query(ResearchReport).filter_by(run_id=run.id, kind="suggest").one()
    sug.suggestions = [{**sug.suggestions[0], "title": f"제안 id {sid}", "detail": f"(id {sid}) 를 본다", "reason": f"ID: {sid} 때문"}]
    db.commit()
    r = user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "suggestion", "report_id": sug.id, "index": 0}, follow_redirects=False)
    assert r.status_code == 303
    new = db.query(Solution).order_by(Solution.id.desc()).first()
    text = new.name + new.summary + new.description
    assert f"id {sid}" not in text.lower() and f"id: {sid}" not in text.lower()
    assert "AI 공조 최적제어" in new.name
