import re

from app.ai.proposal_tasks import Slide
from app.models import AIJob, Proposal
from app.proposals import slide_from_form, slide_to_form
from tests.conftest import make_proposal_world, outlined_proposal, sample_outline


def _slides(db, p):
    db.refresh(p)
    return p.outline["slides"]


def test_outline_page_shows_sources_and_needs_check(user_client, db, settings):
    p = outlined_proposal(db, settings)
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert "절감률 수치" in page and "solutions/1/card.md" in page and "확인할 점" in page
    assert f"/proposals/{p.id}" in page and 'id="slide-7"' in page


def test_outline_page_escapes_text(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["title"] = "<script>alert(1)</script>"
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)" in page


def test_edit_slide_text_fields(user_client, db, settings):
    p = outlined_proposal(db, settings)
    before = p.outline_updated_at
    r = user_client.post(f"/proposals/{p.id}/outline/slides/1", data={
        "kind": "table", "title": "비교", "key_message": "연 10% 절감",
        "columns": "항목 | 현재 | 제안", "rows": "전력 | 100 | 90\n인력 | 3",
        "needs_check": "수치 근거\n"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proposals/{p.id}/outline?open=1#slide-1"
    db.refresh(p)
    s = p.outline["slides"][1]
    assert (s["kind"], s["columns"]) == ("table", ["항목", "현재", "제안"])
    assert s["rows"] == [["전력", "100", "90"], ["인력", "3", ""]] and s["needs_check"] == ["수치 근거"]
    assert s["bullets"] == [] and set(s) == set(Slide(kind="x").model_dump())
    assert p.outline_updated_at > before


def test_edit_cover_and_kind_rules(user_client, db, settings):
    p = outlined_proposal(db, settings)
    url = f"/proposals/{p.id}/outline/slides"
    assert user_client.post(f"{url}/0", data={"kind": "cover", "title": "새 제목", "subtitle": "부제"}).status_code == 200
    assert _slides(db, p)[0]["subtitle"] == "부제"
    assert user_client.post(f"{url}/0", data={"kind": "bullets", "title": "x"}).status_code == 400
    assert user_client.post(f"{url}/1", data={"kind": "cover", "title": "x"}).status_code == 400
    assert user_client.post(f"{url}/1", data={"kind": "nope", "title": "x"}).status_code == 400
    assert _slides(db, p)[1]["kind"] == "bullets"


def test_steps_parsing():
    s = slide_from_form("steps", {"title": "일정", "steps": "1단계: 진단: 2주\n2단계\n\n"})
    assert s["steps"] == [{"name": "1단계", "detail": "진단: 2주"}, {"name": "2단계", "detail": ""}]


def test_form_clears_other_kind_fields():
    s = slide_from_form("bullets", {"title": "t", "bullets": "a\n\nb", "columns": "x | y", "subtitle": "z"})
    assert s["bullets"] == ["a", "b"] and s["columns"] == [] and s["subtitle"] == ""
    assert set(s) == set(Slide(kind="x").model_dump())


def test_form_round_trip():
    slides = sample_outline(8)["slides"]
    full = [Slide.model_validate(x).model_dump() for x in slides]
    full[1].update(key_message="핵심", sources=["solutions/1/card.md", "a/b.md"], needs_check=["확인1", "확인2"])
    full[2].update(left=["a", "b"], right=["c"])
    kinds = {s["kind"] for s in full}
    assert kinds == {"cover", "bullets", "two_column", "table", "steps", "closing"}
    for s in full:
        assert slide_from_form(s["kind"], slide_to_form(s)) == s
    assert slide_from_form("steps", {"steps": "A: x\nB"})["steps"] == [{"name": "A", "detail": "x"}, {"name": "B", "detail": ""}]
    # 값에 | 나 : 가 있어도 허용되는 자리(글머리, 단계 설명)는 그대로 돌아온다
    t = Slide(kind="bullets", title="a|b: c", bullets=["x | y: z"]).model_dump()
    assert slide_from_form("bullets", slide_to_form(t)) == t
    st = Slide(kind="steps", steps=[{"name": "착수", "detail": "a: b | c"}]).model_dump()
    assert slide_from_form("steps", slide_to_form(st)) == st


def test_delete_move_add_rules(user_client, db, settings):
    p = outlined_proposal(db, settings)
    base = f"/proposals/{p.id}/outline/slides"
    titles = [s["title"] for s in _slides(db, p)]
    assert user_client.post(f"{base}/0/delete").status_code == 400
    assert user_client.post(f"{base}/1/move", data={"dir": "up"}).status_code == 400
    assert user_client.post(f"{base}/2/move", data={"dir": "up"}).status_code == 200
    assert [s["title"] for s in _slides(db, p)][1:3] == [titles[2], titles[1]]
    assert user_client.post(f"{base}/7/move", data={"dir": "down"}).status_code == 400
    assert user_client.post(f"{base}/0/move", data={"dir": "down"}).status_code == 400
    assert user_client.post(f"{base}/2/move", data={"dir": "sideways"}).status_code == 400
    r = user_client.post(f"{base}", data={"after": "3"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?open=4&renumber=1#slide-4")
    slides = _slides(db, p)
    assert len(slides) == 9 and slides[4]["title"] == "새 슬라이드" and slides[4]["kind"] == "bullets"
    assert user_client.post(f"{base}/4/delete").status_code == 200
    assert len(_slides(db, p)) == 8
    while len(_slides(db, p)) < 14:
        assert user_client.post(f"{base}", data={"after": "1"}).status_code == 200
    r = user_client.post(f"{base}", data={"after": "1"})
    assert r.status_code == 400 and "슬라이드는 14장까지 만들 수 있습니다." in r.text


def test_updated_at_strictly_increases(user_client, db, settings):
    p = outlined_proposal(db, settings)
    seen = [p.outline_updated_at]
    for _ in range(3):
        user_client.post(f"/proposals/{p.id}/outline/slides/1", data={"kind": "bullets", "title": "같은 내용"})
        db.refresh(p)
        seen.append(p.outline_updated_at)
    assert all(a < b for a, b in zip(seen, seen[1:]))


def test_out_of_range_and_no_outline(user_client, db, settings):
    p = outlined_proposal(db, settings)
    base = f"/proposals/{p.id}/outline"
    assert user_client.post(f"{base}/slides/8", data={"kind": "bullets"}).status_code == 404
    assert user_client.post(f"{base}/slides/8/delete").status_code == 404
    assert user_client.post(f"{base}/slides/8/move", data={"dir": "up"}).status_code == 404
    assert user_client.post(f"{base}/slides", data={"after": "8"}).status_code == 404
    assert user_client.post(f"{base}/slides", data={"after": "x"}).status_code == 404
    q, *_ = make_proposal_world(db, settings)
    assert q.outline is None
    assert user_client.get(f"/proposals/{q.id}/outline").status_code == 404
    assert user_client.post(f"/proposals/{q.id}/outline/slides/0", data={"kind": "cover"}).status_code == 404
    assert user_client.get("/proposals/9999/outline").status_code == 404


def test_edit_blocked_while_job_running(user_client, db, settings):
    p = outlined_proposal(db, settings)
    job = AIJob(kind="proposal_outline", status="running", input={"proposal_id": p.id})
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.commit()
    base = f"/proposals/{p.id}/outline"
    before = [s["title"] for s in _slides(db, p)]
    for path, data in [("/slides/1", {"kind": "bullets", "title": "x"}), ("/slides/1/delete", {}),
                       ("/slides/2/move", {"dir": "up"}), ("/slides", {"after": "1"})]:
        r = user_client.post(base + path, data=data)
        assert r.status_code == 400 and "AI 작업이 끝난 뒤 수정하세요." in r.text
    assert [s["title"] for s in _slides(db, p)] == before
    assert user_client.get(base).status_code == 200


def test_added_blank_slide_needs_check(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides", data={"after": "1"})
    db.refresh(p)
    assert p.outline["slides"][2]["title"] == "새 슬라이드"
    assert p.outline["slides"][2]["needs_check"] == ["근거 자료 없음: 내용 확인 필요"]


def _card(page, i):
    return re.search(rf'<details class="card slide-card[^"]*" id="slide-{i}"([^>]*)>', page)


def test_outline_cards_collapsed_by_default_and_open_param(user_client, db, settings):
    p = outlined_proposal(db, settings)
    url = f"/proposals/{p.id}/outline"
    page = user_client.get(url).text
    assert page.count('<details class="card slide-card') == 8 and [i for i in range(8) if " open" in _card(page, i).group(1)] == [2]  # 확인할 점이 남은 첫 장만 열린다
    assert " open" in _card(user_client.get(url + "?open=3").text, 3).group(1)
    for bad in ("x", "8", "-1", "99999999", ""):  # 잘못된 값은 기본 규칙(첫 남은 장)으로
        page = user_client.get(url + f"?open={bad}").text
        assert [i for i in range(8) if " open" in _card(page, i).group(1)] == [2]


def test_outline_summary_row_has_no_buttons_and_shows_status(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["needs_check"] = ["근거 자료 없음: 값"]
    slides[2]["needs_check"] = ["a", "b"]
    slides[3]["needs_check"] = ["c"]; slides[3]["checked"] = ["c"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    summaries = re.findall(r"<summary class=\"slide-summary\">(.*?)</summary>", page, re.S)
    assert len(summaries) == 8 and not any("<button" in x or "<form" in x or "<a " in x for x in summaries)
    assert "근거 문제 1" in summaries[1] and "확인할 점 2" in summaries[2] and "확인 끝" in summaries[3]
    assert "badge" in summaries[4] and not re.search(r"확인할 점|확인 끝|근거 문제", summaries[4])


def test_redirects_open_the_touched_slide(user_client, db, settings):
    p = outlined_proposal(db, settings)
    base = f"/proposals/{p.id}/outline/slides"
    loc = lambda r: r.headers["location"]
    r = user_client.post(f"{base}/3/move", data={"dir": "up"}, follow_redirects=False)
    assert loc(r) == f"/proposals/{p.id}/outline?open=2&renumber=1#slide-2"
    r = user_client.post(f"{base}/3/delete", follow_redirects=False)
    assert loc(r) == f"/proposals/{p.id}/outline?open=3&renumber=1#slide-3"
    r = user_client.post(f"{base}/6/delete", follow_redirects=False)  # 마지막 장이면 이전 장
    assert loc(r) == f"/proposals/{p.id}/outline?open=5&renumber=1#slide-5"


def test_check_all_route(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[3]["needs_check"] = ["근거 자료 없음: x", "값 정하기", "다른 값"]
    slides[3]["checked"] = ["값 정하기"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    url = f"/proposals/{p.id}/outline/slides/3/check-all"
    r = user_client.post(url, data={"done": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proposals/{p.id}/outline?open=3#slide-3"
    assert sorted(_slides(db, p)[3]["checked"]) == sorted(slides[3]["needs_check"])
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert "확인 모두 취소" in page and "이 장의 항목 모두 확인함" not in page.split('id="slide-3"')[1].split("</details>")[0]
    user_client.post(url, data={"done": "0"})
    assert _slides(db, p)[3]["checked"] == []
    assert "이 장의 항목 모두 확인함" in user_client.get(f"/proposals/{p.id}/outline").text.split('id="slide-3"')[1]
    assert user_client.post(url, data={"done": "x"}).status_code == 400
    assert user_client.post(f"/proposals/{p.id}/outline/slides/99/check-all", data={"done": "1"}).status_code == 404


def test_check_all_button_only_with_two_unchecked(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["needs_check"] = ["하나"]
    slides[2]["needs_check"] = ["하나", "둘"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert page.count("이 장의 항목 모두 확인함") == 1 and "slides/2/check-all" in page


def test_check_all_blocked_while_busy(user_client, db, settings):
    p = outlined_proposal(db, settings)
    job = AIJob(kind="proposal_outline", status="running", input={"proposal_id": p.id})
    db.add(job); db.flush(); p.active_job_id = job.id; db.commit()
    r = user_client.post(f"/proposals/{p.id}/outline/slides/2/check-all", data={"done": "1"})
    assert r.status_code == 400 and "AI 작업이 끝난 뒤 수정하세요." in r.text
