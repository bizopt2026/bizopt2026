from datetime import datetime, timedelta

from app.models import ResearchReport, ResearchRun, Solution
from tests.conftest import finished_run


def _suggest(run):
    return next(r for r in run.reports if r.kind == "suggest")


def test_run_page_shows_everything(user_client, db, settings):
    run = finished_run(db, settings)
    page = user_client.get(f"/research/runs/{run.id}").text
    assert page.index("발굴 방향 제안") < page.index("AI 공조 제어 확산")
    assert 'href="https://example.com/a"' in page and 'rel="noopener noreferrer"' in page and "example.com" in page
    assert "출처 링크 없음, 확인 필요" in page and "AI가 웹에서 수집한 내용입니다" in page
    assert "설비 운영 고객에 제안 기회" in page and "조사 결과가 비어 있습니다." in page
    assert "다시 조사" not in page  # 일반 사용자


def test_run_page_unknown_is_404(user_client):
    assert user_client.get("/research/runs/999").status_code == 404


def test_admin_sees_retry_on_failed_area(admin_client, db, settings):
    run = finished_run(db, settings)
    policy = next(r for r in run.reports if r.area == "policy")
    page = admin_client.get(f"/research/runs/{run.id}").text
    assert "다시 조사" in page and f'action="/research/reports/{policy.id}/retry"' in page
    assert "삭제" in page


def test_push_suggestion_links_solution_not_candidate(user_client, db, settings):
    run = finished_run(db, settings)
    s1 = db.query(Solution).filter_by(name="AI 공조 최적제어").one()
    page = user_client.get(f"/research/runs/{run.id}").text
    assert f'href="/solutions/{s1.id}"' in page
    assert page.count("후보 등록") >= 3  # new 제안 1 + 동향 2, push 제안은 없음
    sug = _suggest(run)
    assert f'value="{sug.id}"' in page


def test_unsafe_url_not_linked_at_render(user_client, db, settings):
    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    tech.trends = [{"headline": "위험", "summary": "", "needs_check": [],
                    "sources": [{"title": "나쁜 링크", "url": "javascript:alert(1)"}]}]
    db.commit()
    page = user_client.get(f"/research/runs/{run.id}").text
    assert 'href="javascript' not in page and "나쁜 링크" in page


def test_candidate_from_suggestion(user_client, db, settings):
    run = finished_run(db, settings)
    sug = _suggest(run)
    r = user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "suggestion", "report_id": sug.id, "index": 0}, follow_redirects=False)
    s = db.query(Solution).filter_by(name="수요반응(DR) 솔루션 발굴").one()
    assert r.headers["location"] == f"/solutions/{s.id}/edit" and s.stage == "intake" and "리서치(" in s.description
    assert s.summary == "전기요금 정책 변화" and s.created_by_id is not None
    assert "- 한빛병원 미팅 (" in s.description and "activities/" not in s.description and "DR 사업자 탐색" in s.description
    assert (settings.knowledge_dir / "solutions" / str(s.id) / "card.md").exists()


def test_candidate_from_trend_and_bad_index(user_client, db, settings):
    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    r = user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "trend", "report_id": tech.id, "index": 0}, follow_redirects=False)
    s = db.query(Solution).filter_by(name="AI 공조 제어 확산").one()
    assert r.status_code == 303 and s.summary == "병원 적용 증가"
    assert "- 기사: https://example.com/a" in s.description and "리서치(" in s.description
    for data in ({"source": "trend", "report_id": tech.id, "index": 99},
                 {"source": "suggestion", "report_id": _suggest(run).id, "index": -1},
                 {"source": "trend", "report_id": 99999, "index": 0}):
        assert user_client.post(f"/research/runs/{run.id}/candidate", data=data).status_code == 404


def test_candidate_validation(user_client, db, settings):
    run = finished_run(db, settings)
    other = ResearchRun(trigger="manual", status="done")
    db.add(other); db.flush()
    foreign = ResearchReport(run_id=other.id, kind="suggest", status="done",
                             suggestions=[{"kind": "new", "title": "남의 제안", "detail": "", "reason": "", "solution_ids": [], "sources": []}])
    db.add(foreign); db.commit()
    r = user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "suggestion", "report_id": foreign.id, "index": 0})
    assert r.status_code == 404 and db.query(Solution).filter_by(name="남의 제안").count() == 0
    r = user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "x", "report_id": _suggest(run).id, "index": 0})
    assert r.status_code == 400


def test_candidate_name_capped_at_80(user_client, db, settings):
    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    tech.trends = [{"headline": "가" * 120, "summary": "", "sources": [], "needs_check": []}]
    db.commit()
    user_client.post(f"/research/runs/{run.id}/candidate", data={"source": "trend", "report_id": tech.id, "index": 0})
    assert db.query(Solution).filter_by(name="가" * 80).count() == 1


def test_list_search_and_area_filter(user_client, db, settings):
    run = finished_run(db, settings)
    old = ResearchRun(trigger="schedule", status="done", started_at=datetime.now() - timedelta(days=7), finished_at=datetime.now())
    db.add(old); db.flush()
    db.add(ResearchReport(run_id=old.id, kind="area", area="market", status="done", title="시장 보고",
                          trends=[{"headline": "충전 인프라 수요", "summary": "", "sources": [], "needs_check": []}],
                          insights=["충전 사업 연계"]))
    db.commit()
    page = user_client.get("/research").text
    assert f'href="/research/runs/{run.id}"' in page and f'href="/research/runs/{old.id}"' in page
    assert "설비 운영 고객에 제안 기회" in page and "일부 실패" in page
    page = user_client.get("/research?q=충전").text
    assert f'href="/research/runs/{old.id}"' in page and f'href="/research/runs/{run.id}"' not in page
    page = user_client.get("/research?q=수요반응").text  # 제안 제목
    assert f'href="/research/runs/{run.id}"' in page and f'href="/research/runs/{old.id}"' not in page
    page = user_client.get("/research?area=market").text
    assert f'href="/research/runs/{old.id}"' in page and f'href="/research/runs/{run.id}"' not in page
    assert "조건에 맞는 항목이 없습니다." in user_client.get("/research?q=없는말").text


def test_list_empty_and_running(user_client, db, settings):
    assert "아직 리서치 결과가 없습니다." in user_client.get("/research").text
    run = ResearchRun(trigger="manual", status="running")
    db.add(run); db.flush()
    db.add(ResearchReport(run_id=run.id, kind="area", area="tech", status="pending"))
    db.commit()
    page = user_client.get("/research").text
    assert f'hx-get="/research/runs/{run.id}/status"' in page
    page = user_client.get(f"/research/runs/{run.id}").text
    assert f'hx-get="/research/runs/{run.id}/status"' in page


def test_escaping(user_client, db, settings):
    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    tech.trends = [{"headline": "<script>x</script>", "summary": "<b>굵게</b>", "needs_check": [],
                    "sources": [{"title": "<i>t</i>", "url": "https://example.com/?a=1&b=<2>"}]}]
    db.commit()
    for url in (f"/research/runs/{run.id}", "/research?q=script"):
        page = user_client.get(url).text
        assert "<script>x</script>" not in page and "<b>굵게</b>" not in page
    page = user_client.get(f"/research/runs/{run.id}").text
    assert "&lt;script&gt;" in page and "<i>t</i>" not in page


def test_delete_run(admin_client, user_client, db, settings):
    run = finished_run(db, settings)
    run_id = run.id
    copies = sorted((settings.knowledge_dir / "research").glob("*.md"))
    assert len(copies) == 2
    assert user_client.post(f"/research/runs/{run_id}/delete").status_code == 403
    r = admin_client.post(f"/research/runs/{run_id}/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/research"
    db.expire_all()
    assert db.get(ResearchRun, run_id) is None and db.query(ResearchReport).count() == 0
    assert not any(p.exists() for p in copies)


def test_delete_running_run_is_400(admin_client, db):
    run = ResearchRun(trigger="manual", status="running")
    db.add(run); db.commit()
    r = admin_client.post(f"/research/runs/{run.id}/delete")
    assert r.status_code == 400 and "진행 중인 조사는 삭제할 수 없습니다." in r.text
    assert db.get(ResearchRun, run.id) is not None
    assert admin_client.post("/research/runs/999/delete").status_code == 404


def test_nav_has_research_for_everyone(user_client):
    assert 'href="/research"' in user_client.get("/").text


def test_unopened_links_are_labelled_old_ones_are_not(user_client, db, settings):
    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    tech.trends = [{"headline": "동향", "summary": "", "needs_check": ["본문을 열어 확인하지 못함"], "sources": [
        {"title": "검색만", "url": "https://example.com/s", "opened": False},
        {"title": "열어 봄", "url": "https://example.com/o", "opened": True},
        {"title": "옛 기록", "url": "https://example.com/old"}]}]
    db.commit()
    page = user_client.get(f"/research/runs/{run.id}").text
    assert page.count("검색 결과 링크 (본문 미확인)") == 1
    assert page.index("검색만") < page.index("검색 결과 링크 (본문 미확인)") < page.index("열어 봄")
    assert "본문을 열어 확인하지 못함" in page


def test_unopened_links_are_labelled_in_knowledge_copy(db, settings):
    from app.knowledge import KnowledgeMirror

    run = finished_run(db, settings)
    tech = next(r for r in run.reports if r.area == "tech")
    tech.trends = [{"headline": "동향", "summary": "", "needs_check": [], "sources": [
        {"title": "검색만", "url": "https://example.com/s", "opened": False},
        {"title": "열어 봄", "url": "https://example.com/o", "opened": True}]}]
    db.commit()
    mirror = KnowledgeMirror(settings.knowledge_dir)
    mirror.sync(tech)
    text = mirror.path_for(tech).read_text(encoding="utf-8")
    assert "[검색만](https://example.com/s) (검색 결과 링크, 본문 미확인)" in text
    assert "[열어 봄](https://example.com/o)\n" in text + "\n"
