"""3묶음: 확인 필요 두 종류와 확인함(C1), 출처를 자료 이름과 링크로(C2)."""
import re
from datetime import date
from io import BytesIO

import pytest
from pptx import Presentation

from app.checks import check_kind
from app.deck import build_deck
from app.knowledge import KnowledgeMirror
from app.models import Activity, Business, Customer, Partner, Proposal, ResearchReport, ResearchRun, Solution, SolutionFile
from app.proposals import deck_is_stale, outline_check_summary, section_summaries, slide_check_state
from app.sources import SourceLookup, describe_source
from tests.conftest import finished_run, outlined_proposal, sample_outline

EVIDENCE = ["근거 자료 없음: 내용 확인 필요", "출처 확인 불가: customers/9.md", "출처 링크 없음, 확인 필요", "본문을 열어 확인하지 못함", "후보에 없는 솔루션이 언급됨"]


# ---------------------------------------------------------------- C1
def test_check_kind():
    assert all(check_kind(t) == "evidence" for t in EVIDENCE)
    assert check_kind("절감률 수치") == "decide"
    assert check_kind("") == "decide" and check_kind("수치 근거 자료 없음") == "decide"


def test_phrases_made_by_code_are_evidence(db, settings):
    from app.ai.proposal_tasks import Outline
    from app.proposals import clean_outline
    out = clean_outline(Outline.model_validate({"title": "t", "slides": [
        {"kind": "cover", "title": "t"}, {"kind": "bullets", "title": "a"},
        {"kind": "bullets", "title": "b", "sources": ["customers/99.md"]}]}), settings.knowledge_dir)
    notes = [n for s in out["slides"] for n in s["needs_check"]]
    assert len(notes) == 2 and all(check_kind(n) == "evidence" for n in notes)
    assert all(s["checked"] == [] for s in out["slides"])


def test_slide_check_state_and_summary():
    s = {"needs_check": ["근거 자료 없음", "값 정하기", "다른 값"], "checked": ["근거 자료 없음"]}
    assert slide_check_state(s) == {"evidence": 0, "decide": 2, "done": 1, "level": "decide"}
    assert slide_check_state({"needs_check": ["근거 자료 없음"]})["level"] == "evidence"
    assert slide_check_state({"needs_check": ["x"], "checked": ["x"]}) == {"evidence": 0, "decide": 0, "done": 1, "level": "clear"}
    assert slide_check_state({})["level"] == "clear"
    outline = {"slides": [s, {"needs_check": ["근거 자료 없음"]}, {"needs_check": ["x"], "checked": ["x"]}, {}]}
    assert outline_check_summary(outline) == {"evidence": 1, "decide": 1, "done": 1, "open": 2}
    assert outline_check_summary(None)["open"] == 0


def test_check_route_toggles_and_redirects(user_client, db, settings):
    p = outlined_proposal(db, settings)
    before = p.outline_updated_at
    url = f"/proposals/{p.id}/outline/slides/2/check"
    r = user_client.post(url, data={"item": "0", "text": "절감률 수치", "done": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proposals/{p.id}/outline?open=2#slide-2"
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == ["절감률 수치"] and p.outline_updated_at > before
    user_client.post(url, data={"item": "0", "text": "절감률 수치", "done": "1"})
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == ["절감률 수치"]
    user_client.post(url, data={"item": "0", "text": "절감률 수치", "done": "0"})
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == []
    assert user_client.post(url, data={"item": "0", "text": "없는 문구", "done": "1"}).status_code == 404
    assert user_client.post(url, data={"item": "0", "text": "절감률 수치", "done": "x"}).status_code == 400
    assert user_client.post(f"/proposals/{p.id}/outline/slides/99/check", data={"item": "0", "text": "a"}).status_code == 404


def test_check_marks_deck_stale_and_busy_is_rejected(user_client, db, settings):
    from datetime import datetime
    from app.models import AIJob
    p = outlined_proposal(db, settings)
    p.deck_name, p.deck_built_at, p.status = "x.pptx", datetime.now(), "deck"
    p.outline_updated_at = p.deck_built_at.replace(microsecond=0)
    db.commit()
    user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "1"})
    db.refresh(p)
    assert deck_is_stale(p) and p.status == "outlined"
    job = AIJob(kind="proposal_outline", input={}, status="running", priority=0)
    db.add(job); db.flush(); p.active_job_id = job.id; db.commit()
    assert user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "0"}).status_code == 400


def test_old_outline_without_checked_key_renders(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [{k: v for k, v in s.items() if k != "checked"} for s in p.outline["slides"]]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert 'aria-label="확인함"' in page
    assert "확인할 항목이 남은 장: 확인할 점 1장" in re.sub(r"<[^>]+>", "", page)


def test_edit_drops_checked_for_removed_text(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "1"})
    base = {"kind": "bullets", "title": "t", "bullets": "a"}
    user_client.post(f"/proposals/{p.id}/outline/slides/2", data={**base, "needs_check": "절감률 수치\n새 항목"})
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == ["절감률 수치"]
    user_client.post(f"/proposals/{p.id}/outline/slides/2", data={**base, "needs_check": "새 항목"})
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == []


def test_outline_page_levels_and_summary(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["needs_check"] = ["근거 자료 없음: 값"]
    slides[3]["needs_check"] = ["a", "b"]
    slides[4]["needs_check"] = ["c"]; slides[4]["checked"] = ["c"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    text = re.sub(r"<[^>]+>", "", page)
    assert "근거 문제 1장 · 확인할 점 2장 · 확인 끝 1장" in text
    card = lambda i: re.search(rf'<details class="card slide-card([^"]*)" id="slide-{i}"', page).group(1)
    assert "check-evidence" in card(1) and "check-decide" in card(2) and "check-decide" in card(3) and "check" not in card(4)
    assert page.count('aria-label="확인함"') == 5 and page.count('aria-pressed="true"') == 1 and page.count('aria-pressed="false"') >= 4
    assert "남은 장만 보기" in page and "고객에게 보내기 전에" in page
    assert "근거 문제가 남은 장이 1장 있습니다. 고객에게 보내기 전에 확인하세요." in page
    assert "확인할 항목이 남은 장: 근거 문제 1장 · 확인할 점 2장" in text
    assert section_summaries(p, [])["outline"] == "8장 · 근거 문제 1장 · 확인할 점 2장"


def test_detail_page_notice_counts_unchecked_only(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "1"})
    page = user_client.get(f"/proposals/{p.id}").text
    assert "확인할 항목이 남은 장" not in page and "8장" in page and "확인할 점 0장" not in page


def test_direction_and_trend_colors(user_client, db, settings):
    p = outlined_proposal(db, settings)
    p.directions = [{**p.directions[0], "needs_check": ["값 정하기", "근거 자료 없음: x"]}, p.directions[1]]
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert page.index("근거 자료 없음: x") < page.index("값 정하기")
    assert 'class="evidence"' in page and 'class="decide"' in page and 'aria-label="확인함"' not in page


# ---------------------------------------------------------------- deck notes
def _notes(path):
    prs = Presentation(path)
    return [s.notes_slide.notes_text_frame.text if s.has_notes_slide else "" for s in prs.slides]


def test_deck_notes_checked_and_labels(tmp_path):
    slide = {"kind": "bullets", "title": "a", "bullets": ["x"], "sources": ["solutions/1/card.md", "x.md"],
             "needs_check": ["근거 자료 없음", "값", "다른 값"], "checked": ["값"]}
    build_deck({"title": "t", "slides": [slide]}, tmp_path / "a.pptx", source_labels={"solutions/1/card.md": "에너지 솔루션"})
    assert _notes(tmp_path / "a.pptx") == ["[출처]\n에너지 솔루션\nx.md\n\n[확인 필요]\n근거 자료 없음\n다른 값\n\n[확인함]\n값"]
    build_deck({"title": "t", "slides": [{**slide, "checked": []}]}, tmp_path / "b.pptx")
    assert _notes(tmp_path / "b.pptx")[0].startswith("[출처]\nsolutions/1/card.md\nx.md\n\n[확인 필요]\n근거 자료 없음\n값\n다른 값")
    assert "[확인함]" not in _notes(tmp_path / "b.pptx")[0]


def test_deck_route_writes_names_and_checked(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "1"})
    assert user_client.post(f"/proposals/{p.id}/deck", follow_redirects=False).status_code == 303
    db.refresh(p)
    notes = _notes(settings.decks_dir / str(p.id) / p.deck_name)
    assert any("[출처]\nAI 에너지 절감" in n and "[확인함]\n절감률 수치" in n for n in notes)
    assert not any("solutions/1/card.md" in n for n in notes)


# ---------------------------------------------------------------- C2
def _world(db, settings):
    from datetime import datetime
    partner = Partner(name="파트너사")
    cust = Customer(name="한빛병원")
    sol = Solution(name="AI 에너지 절감", stage="boxed")
    db.add_all([partner, cust, sol]); db.flush()
    biz = Business(name="설비 유지보수", customer_id=cust.id)
    act = Activity(kind="meeting", date=date(2026, 3, 4), title="킥오프", raw_text="x")
    undated = Activity(kind="meeting", title="", raw_text="x")
    f = SolutionFile(solution_id=sol.id, kind="brochure", original_name="소개서 v2.pdf", stored_name="s.pdf", size=1)
    run = ResearchRun(trigger="manual", status="done")
    db.add_all([biz, act, undated, f, run]); db.flush()
    area = ResearchReport(run_id=run.id, kind="area", area="tech", status="done", created_at=datetime(2026, 5, 6, 9))
    sug = ResearchReport(run_id=run.id, kind="suggest", status="done", created_at=datetime(2026, 5, 6, 9))
    db.add_all([area, sug]); db.commit()
    return [partner, cust, sol, biz, act, undated, f, area, sug], run


def test_describe_source_for_every_record_type(db, settings):
    objs, run = _world(db, settings)
    partner, cust, sol, biz, act, undated, f, area, sug = objs
    m = KnowledgeMirror(settings.knowledge_dir)
    got = {type(o).__name__ + str(o.id): describe_source(db, m.rel_path(o)) for o in objs}
    assert got[f"Solution{sol.id}"] == {"label": "AI 에너지 절감", "url": f"/solutions/{sol.id}", "kind": "솔루션"}
    assert got[f"Customer{cust.id}"] == {"label": "한빛병원", "url": f"/customers/{cust.id}", "kind": "고객"}
    assert got[f"Business{biz.id}"] == {"label": "설비 유지보수", "url": f"/businesses/{biz.id}", "kind": "기존 사업"}
    assert got[f"Partner{partner.id}"] == {"label": "파트너사", "url": f"/partners/{partner.id}", "kind": "파트너"}
    assert got[f"Activity{act.id}"] == {"label": "킥오프 (2026-03-04)", "url": f"/activities/{act.id}", "kind": "활동 기록"}
    assert got[f"Activity{undated.id}"]["url"] == f"/activities/{undated.id}"
    assert got[f"SolutionFile{f.id}"] == {"label": "소개서 v2.pdf (AI 에너지 절감)", "url": f"/solutions/{sol.id}", "kind": "자료"}
    assert got[f"ResearchReport{area.id}"] == {"label": "리서치 2026-05-06 기술", "url": f"/research/runs/{run.id}", "kind": "리서치"}
    assert got[f"ResearchReport{sug.id}"]["label"] == "리서치 2026-05-06 발굴 방향 제안"


@pytest.mark.parametrize("path", ["", "x", "solutions/999/card.md", "solutions/abc/card.md", "customers/99999999999999999999999.md",
                                  "solutions/1/files/77-a.md", "research/2026-01-01-tech-5.md", "../etc/passwd", "solutions/1/card.md/x"])
def test_describe_source_falls_back_to_path(db, path):
    assert describe_source(db, path) == {"label": path, "url": None, "kind": ""}


def test_describe_source_backslash_and_wrong_owner(db, settings):
    objs, _ = _world(db, settings)
    sol, f = objs[2], objs[6]
    assert describe_source(db, f"solutions\\{sol.id}\\card.md")["label"] == "AI 에너지 절감"
    assert describe_source(db, f"solutions/{sol.id + 5}/files/{f.id}-a.md")["url"] is None


def test_lookup_caches_per_request(db, settings, monkeypatch):
    objs, _ = _world(db, settings)
    calls = []
    import app.sources as srcs
    real = srcs.describe_source
    monkeypatch.setattr(srcs, "describe_source", lambda d, p: calls.append(p) or real(d, p))
    look = SourceLookup(db)
    path = f"solutions/{objs[2].id}/card.md"
    assert look(path) is look(path) and calls == [path]
    assert look.labels([path]) == {path: "AI 에너지 절감"}


def test_outline_page_shows_names_with_links(user_client, db, settings):
    p = outlined_proposal(db, settings)
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert 'href="/solutions/1"' in page and "AI 에너지 절감" in page
    assert not re.search(r"<code[^>]*>solutions/1/card.md", page)
    assert "자료 이름은 저장 후 자동으로 표시됩니다" in page
    assert "solutions/1/card.md</textarea>" in page  # 편집칸은 경로 그대로


def test_research_page_shows_names(user_client, db, settings):
    run = finished_run(db, settings)
    page = user_client.get(f"/research/runs/{run.id}").text
    assert 'class="chip" href="/activities/1"' in page and ">activities/2026/1.md<" not in page


# ---------------------------------------------------------------- polish round

def test_direction_card_shows_source_chip_with_link(user_client, db, settings):
    p = outlined_proposal(db, settings)
    d = dict(p.directions[0], rationale=[{"text": "근거 문장", "sources": ["solutions/1/card.md"]}])
    p.directions = [d, p.directions[1]]
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert re.search(r'<a class="chip" href="/solutions/1">', page)


def test_check_requires_login(client, db, settings):
    p = outlined_proposal(db, settings)
    r = client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치", "done": "1"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]


def test_check_is_by_position_and_handles_newlines(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[2]["needs_check"] = ["첫째", "둘째\n줄바꿈"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    url = f"/proposals/{p.id}/outline/slides/2/check"
    assert user_client.post(url, data={"item": "0", "text": "둘째\n줄바꿈"}).status_code == 404  # 낡은 화면
    assert user_client.post(url, data={"item": "x", "text": "첫째"}).status_code == 404
    assert user_client.post(url, data={"item": "1", "text": "둘째\r\n줄바꿈"}, follow_redirects=False).status_code == 303
    db.refresh(p)
    assert p.outline["slides"][2]["checked"] == ["둘째\n줄바꿈"]


def test_duplicate_needs_check_merged_on_save(user_client, db, settings):
    p = outlined_proposal(db, settings)
    base = {"kind": "bullets", "title": "t", "bullets": "a"}
    user_client.post(f"/proposals/{p.id}/outline/slides/2", data={**base, "needs_check": "같은 것\n같은 것\n다른 것"})
    db.refresh(p)
    assert p.outline["slides"][2]["needs_check"] == ["같은 것", "다른 것"]


def test_ai_checked_is_ignored_and_not_in_ai_schema():
    from app.ai.proposal_tasks import Outline
    assert "checked" not in str(Outline.model_json_schema())
    raw = sample_outline(8)
    raw["slides"][2]["needs_check"] = ["x"]
    raw["slides"][2]["checked"] = ["x"]
    from app.proposals import clean_outline
    import pathlib
    out = clean_outline(Outline.model_validate(raw), pathlib.Path("/nonexistent"))
    assert all(s["checked"] == [] for s in out["slides"])


def test_checked_follows_slide_on_move_and_delete(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides/2/check", data={"item": "0", "text": "절감률 수치"})
    r = user_client.post(f"/proposals/{p.id}/outline/slides/2/move", data={"dir": "up"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    db.refresh(p)
    hit = [i for i, s in enumerate(p.outline["slides"]) if s["checked"]]
    assert hit == [1] and p.outline["slides"][1]["needs_check"] == ["절감률 수치"]
    user_client.post(f"/proposals/{p.id}/outline/slides/3/delete")
    db.refresh(p)
    assert [i for i, s in enumerate(p.outline["slides"]) if s["checked"]] == [1]


def test_mixed_slide_shown_consistently(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[2]["needs_check"] = ["값 정하기", "근거 자료 없음: y"]
    slides[2]["checked"] = ["근거 자료 없음: y"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    text = re.sub(r"<[^>]+>", "", user_client.get(f"/proposals/{p.id}/outline").text)
    assert "확인할 항목이 남은 장: 확인할 점 1장" in text and "근거 문제 0장 · 확인할 점 1장 · 확인 끝 0장" in text
    assert "check-decide" in re.search(r'<details class="card slide-card([^"]*)" id="slide-2"', user_client.get(f"/proposals/{p.id}/outline").text).group(1)
    assert section_summaries(p, [])["outline"] == "8장 · 확인할 점 1장"
    detail = re.sub(r"<[^>]+>", "", user_client.get(f"/proposals/{p.id}").text)
    assert "확인할 항목이 남은 장: 확인할 점 1장" in detail and "8장 · 확인할 점 1장" in detail


# ---------------------------------------------------------------- 4묶음: 방향 카드와 출처 번호

def test_number_sources():
    from app.proposals import number_sources
    d = {"rationale": [
        {"text": "가", "sources": ["a.md", "b.md"]},
        {"text": "나", "sources": []},
        {"text": "다", "sources": ["b.md", "c.md", "b.md"]},
    ]}
    assert number_sources(d) == {
        "rationale": [{"text": "가", "refs": [1, 2]}, {"text": "나", "refs": []}, {"text": "다", "refs": [2, 3]}],
        "sources": ["a.md", "b.md", "c.md"],
    }
    assert number_sources({}) == {"rationale": [], "sources": []}
    assert number_sources({"rationale": [{"text": "x"}, "junk", {"text": "y", "sources": [5, "z.md"]}]})["sources"] == ["z.md"]


def _two_source_direction(p):
    return dict(p.directions[0], summary="한 줄 요약", effects=["효과 하나"], needs_check=["근거 자료 없음: 값", "값 정하기"], rationale=[
        {"text": "첫 근거", "sources": ["solutions/1/card.md", "solutions/2/card.md"]},
        {"text": "둘째 근거", "sources": ["solutions/1/card.md"]},
        {"text": "셋째 근거", "sources": []},
    ])


def test_direction_card_numbers_sources_and_lists_them_once(user_client, db, settings):
    p = outlined_proposal(db, settings)
    p.directions = [_two_source_direction(p), p.directions[1]]
    p.selected_index, p.status = None, "directions"
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert page.count('class="chip"') == 2 and '<ol class="source-list">' in page
    assert '<sup><a href="#d0-src-1">1</a></sup>' in page and '<sup><a href="#d0-src-2">2</a></sup>' in page
    assert page.count('<sup><a href="#d0-src-1">1</a></sup>') == 2 and 'id="d0-src-1"' in page and 'id="d0-src-2"' in page
    assert re.search(r"셋째 근거(?!<sup)", page) and "출처:</span>" not in page


def test_directions_grid_before_selection_and_flat_after(user_client, db, settings):
    p = outlined_proposal(db, settings)
    p.directions = [_two_source_direction(p), p.directions[1]]
    p.selected_index, p.status = None, "directions"
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert page.count('class="dir-grid"') == 1 and page.count("이 방향으로 구체화") == 2
    assert page.count("근거와 확인할 점 보기") == 2 and "근거 5건" not in page
    assert "근거 3건 · 확인할 점 2건" in re.sub(r"<[^>]+>", "", page) and "근거 문제 1건" in page
    p.selected_index, p.status = 0, "refining"
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert 'class="dir-grid"' not in page and "다른 방향 1개 보기" in page
    assert re.search(r'<details class="dir-more" open>', page)


def test_direction_full_summary_and_roles_inside_details(user_client, db, settings):
    p = outlined_proposal(db, settings)
    long = "아주 긴 요약 " * 30
    role = "아주 긴 역할 설명 " * 20
    d = dict(p.directions[0], summary=long.strip(), solutions=[{"solution_id": 1, "name": "솔", "role": role.strip()}])
    p.directions = [d, p.directions[1]]
    p.selected_index, p.status = None, "directions"
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    body = re.search(r'<details class="dir-more">(.*?)</details>', page, re.S).group(1)
    assert long.strip() in body and role.strip() in body and "<dt>요약</dt>" in body and "<dt>조합 솔루션</dt>" in body
    assert f'title="{long.strip()}"' in page


def test_slide_summary_shows_both_counts(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["needs_check"] = ["근거 자료 없음: x", "값 a", "값 b"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    s = re.findall(r'<summary class="slide-summary">(.*?)</summary>', page, re.S)[1]
    assert "근거 문제 1" in s and "확인할 점 2" in s


def test_rebuild_button_class_only_with_directions(user_client, db, settings):
    p = outlined_proposal(db, settings)
    assert 'class="dir-rebuild"' in user_client.get(f"/proposals/{p.id}").text
    p.directions, p.selected_index = [], None
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    assert 'class="dir-rebuild"' not in page and "방향 만들기" in page
