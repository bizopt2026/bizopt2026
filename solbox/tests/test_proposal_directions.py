import json
import threading

from app.ai.queue import enqueue, process_next
from app.labels import ERROR_KINDS
from app.models import AIJob, Proposal, ProposalMessage
from app.proposals import check_sources, start_job
from tests.conftest import make_proposal_world, make_user


def _run(app, settings):
    return process_next(app.state.session_factory, app.state.runner, settings)


def good_directions(s1, s2):
    return {"directions": [
        {"title": "전력비 절감 패키지", "summary": "AI 제어와 조명 교체",
         "solutions": [{"solution_id": s1.id, "role": "공조 제어"}, {"solution_id": s2.id, "role": "조명"}],
         "rationale": [{"text": "전력비 부담 언급", "sources": [f"solutions/{s1.id}/card.md"]}],
         "effects": ["전력비 절감"], "needs_check": ["절감률 수치"]},
        {"title": "조명 단독 교체", "solutions": [{"solution_id": s2.id}],
         "rationale": [{"text": "노후 조명", "sources": ["customers/999.md"]}]}]}


def _set(fake_claude, data):
    fake_claude.set(mode="ok", result=json.dumps(data, ensure_ascii=False))


def test_directions_flow(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    assert "AI 대기 1번째" in user_client.get(f"/proposals/{p.id}").text
    _run(app, settings)
    db.refresh(p)
    assert p.status == "directions" and len(p.directions) == 2 and p.active_job_id is None
    d1, d2 = p.directions
    assert d1["solutions"][0] == {"solution_id": s1.id, "name": "AI 에너지 절감", "role": "공조 제어"}
    assert d2["rationale"][0]["sources"] == []
    assert "출처 확인 불가: customers/999.md" in d2["needs_check"]
    assert "근거 자료 없음: 노후 조명" in d2["needs_check"]
    prompt = fake_claude.calls()[0]["stdin"]
    assert "AI 에너지 절감" in prompt and "안전 관제" not in prompt
    assert f"customers/{c.id}.md" in prompt and "에너지 절감 쪽으로" in prompt
    page = user_client.get(f"/proposals/{p.id}").text
    assert "전력비 절감 패키지" in page and "이 방향으로 구체화" in page
    assert f"solutions/{s1.id}/card.md" not in page  # 경로 대신 자료 이름(C2)


def test_extra_solution_becomes_candidate(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    p.extra_solution_ids = [s3.id]
    db.commit()
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    assert "안전 관제" in fake_claude.calls()[0]["stdin"]


def test_unknown_solution_id_is_dropped(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    data = good_directions(s1, s2)
    data["directions"][0]["solutions"].append({"solution_id": 9999})
    data["directions"][0]["solutions"].append({"solution_id": s3.id})  # 후보 아님
    user_client.post(f"/proposals/{p.id}/directions")
    _set(fake_claude, data)
    _run(app, settings)
    db.refresh(p)
    d1 = p.directions[0]
    assert [s["solution_id"] for s in d1["solutions"]] == [s1.id, s2.id]
    assert d1["needs_check"].count("후보에 없는 솔루션이 언급됨") == 1
    # 후보 없는 방향은 버려지고, 남은 방향이 1개뿐이면 실패
    data = good_directions(s1, s2)
    data["directions"][1]["solutions"] = [{"solution_id": 9999}]
    _set(fake_claude, data)
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert p.active_job.status == "failed" and p.active_job.error == "제안 방향을 충분히 만들지 못했습니다."
    assert len(p.directions) == 2  # 이전 결과는 그대로
    assert "제안 방향을 충분히 만들지 못했습니다." in user_client.get(f"/proposals/{p.id}").text


def test_more_than_three_directions_trimmed(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    d = {"title": "방향", "solutions": [{"solution_id": s1.id}]}
    _set(fake_claude, {"directions": [{**d, "title": f"방향{i}"} for i in range(5)]})
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert [x["title"] for x in p.directions] == ["방향0", "방향1", "방향2"]


def test_no_boxed_solutions_fails_without_cli(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    for s in (s1, s2):
        s.stage = "hold"
    db.commit()
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert p.active_job.status == "failed" and p.active_job.error == "박스에 등록된 솔루션이 없습니다."
    assert fake_claude.calls() == []


def test_memo_is_wrapped_as_untrusted(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    prompt = fake_claude.calls()[0]["stdin"]
    assert "<<<MEMO\n에너지 절감 쪽으로\nMEMO>>>" in prompt and "신뢰할 수 없는 글" in prompt


def test_check_sources_rejects_traversal(tmp_path):
    (tmp_path / "customers").mkdir()
    (tmp_path / "customers" / "1.md").write_text("x", encoding="utf-8")
    ok, bad = check_sources(tmp_path, ["customers/1.md", "customers\\1.md", "../secret.md", "/etc/passwd", "customers/2.md"])
    assert ok == ["customers/1.md", "customers/1.md"] and bad == ["../secret.md", "/etc/passwd", "customers/2.md"]


def test_check_sources_rejects_other_oddities(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.md").write_text("x", encoding="utf-8")
    ok, bad = check_sources(tmp_path, ["a", "", "C:/x.md", "a/../a/x.md", "a/x.md", "./a/x.md", "a//x.md", "a/b:c.md"])
    assert ok == ["a/x.md", "a/x.md", "a/x.md"] and bad == ["a", "", "C:/x.md", "a/../a/x.md", "a/b:c.md"]


def test_second_request_while_running_is_ignored(user_client, db, settings):
    p, *_ = make_proposal_world(db, settings)
    user_client.post(f"/proposals/{p.id}/directions")
    r = user_client.post(f"/proposals/{p.id}/directions", follow_redirects=False)
    assert "notice=busy" in r.headers["location"]
    assert db.query(AIJob).filter_by(kind="proposal_directions").count() == 1
    assert "이미 AI 작업이 진행 중입니다." in user_client.get(r.headers["location"]).text


def test_create_starts_directions_job(user_client, db, settings):
    p, c, b, *_ = make_proposal_world(db, settings)
    user_client.post("/proposals", data={"customer_id": c.id, "business_id": b.id})
    new = db.query(Proposal).order_by(Proposal.id.desc()).first()
    assert new.id != p.id and new.status == "new"
    assert new.active_job_id is not None and new.active_job.kind == "proposal_directions"
    assert new.active_job.input["proposal_id"] == new.id


def test_start_job_is_atomic_under_threads(app, db, settings):
    p, *_ = make_proposal_world(db, settings)
    user = make_user(db, "t1")
    barrier = threading.Barrier(8)
    results = []

    def go():
        with app.state.session_factory() as s:
            pp = s.get(Proposal, p.id)
            u = s.get(type(user), user.id)
            barrier.wait()
            results.append(start_job(s, pp, "proposal_directions", u))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is not None for r in results) == 1
    assert db.query(AIJob).filter_by(kind="proposal_directions").count() == 1


def test_failed_job_shows_error_and_retry(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    fake_claude.set(mode="error", stderr="Please run /login", exit_code=1)
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert p.active_job.status == "failed" and p.active_job.error_kind == "auth"
    page = user_client.get(f"/proposals/{p.id}").text
    assert ERROR_KINDS["auth"] in page and "다시 시도" in page
    _set(fake_claude, good_directions(s1, s2))
    r = user_client.post(f"/proposals/{p.id}/directions", follow_redirects=False)
    assert "notice" not in r.headers["location"]
    _run(app, settings)
    db.refresh(p)
    assert p.status == "directions" and db.query(AIJob).count() == 2


def test_status_fragment_visible_to_other_user_and_redirects_when_done(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    make_user(db, "lee")
    from fastapi.testclient import TestClient
    other = TestClient(app)
    other.post("/login", data={"username": "lee", "password": "pw1234"})
    r = other.get(f"/proposals/{p.id}/status")
    assert r.status_code == 200 and "AI 대기 1번째" in r.text and "hx-trigger" in r.text
    assert "HX-Redirect" not in r.headers
    _run(app, settings)
    r = other.get(f"/proposals/{p.id}/status")
    assert r.headers["HX-Redirect"] == f"/proposals/{p.id}"
    gone = other.get("/proposals/9999/status")  # 지워진 제안 건: 404 를 되풀이하지 않고 목록으로 보낸다
    assert gone.status_code == 200 and gone.headers["HX-Redirect"] == "/proposals"


def _with_directions(db, settings):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    p.directions = [
        {"title": "전력비 절감 패키지", "summary": "", "effects": [], "needs_check": [], "rationale": [],
         "solutions": [{"solution_id": s1.id, "name": s1.name, "role": ""}, {"solution_id": s2.id, "name": s2.name, "role": ""}]},
        {"title": "조명 단독 교체", "summary": "", "effects": [], "needs_check": [], "rationale": [],
         "solutions": [{"solution_id": s2.id, "name": s2.name, "role": ""}]},
    ]
    p.status = "directions"
    db.commit()
    return p, s1, s2


def test_select_direction(user_client, db, settings):
    p, s1, s2 = _with_directions(db, settings)
    r = user_client.post(f"/proposals/{p.id}/select", data={"index": "0"}, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(p)
    assert p.status == "refining" and p.selected_index == 0
    assert sorted(s.id for s in p.solutions) == sorted([s1.id, s2.id]) and p.title == "전력비 절감 패키지"
    assert "선택됨" in user_client.get(f"/proposals/{p.id}").text
    user_client.post(f"/proposals/{p.id}/select", data={"index": "1"})
    db.refresh(p)
    assert [s.id for s in p.solutions] == [s2.id] and p.title == "전력비 절감 패키지"  # 제목은 이미 있으면 유지
    for bad in ("5", "-1", "x", ""):
        assert user_client.post(f"/proposals/{p.id}/select", data={"index": bad}).status_code == 400


def test_regenerate_resets_selection(app, user_client, db, settings, fake_claude):
    p, s1, s2 = _with_directions(db, settings)
    user_client.post(f"/proposals/{p.id}/select", data={"index": "0"})
    db.add(ProposalMessage(proposal_id=p.id, role="user", content="안녕"))
    db.commit()
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert p.selected_index is None and p.status == "directions" and len(p.messages) == 1


def test_proposal_deleted_while_job_runs(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    pid = p.id
    db.query(Proposal).filter_by(id=pid).update({"active_job_id": None})
    db.query(Proposal).filter_by(id=pid).delete()
    db.commit()
    _run(app, settings)
    job = db.query(AIJob).one()
    assert job.status == "done" and job.result == "deleted"


def test_loser_of_race_leaves_no_job(app, db, settings):
    p, *_ = make_proposal_world(db, settings)
    user = make_user(db, "t2")
    with app.state.session_factory() as s1, app.state.session_factory() as s2:
        p1, p2 = s1.get(Proposal, p.id), s2.get(Proposal, p.id)
        u1, u2 = s1.get(type(user), user.id), s2.get(type(user), user.id)
        assert p1.active_job_id is None and p2.active_job_id is None  # 둘 다 낡은 값을 들고 있다
        assert start_job(s1, p1, "proposal_directions", u1) is not None
        s2.refresh = lambda *_a, **_k: None  # 낡은 상태 그대로 경쟁하게 한다
        assert start_job(s2, p2, "proposal_directions", u2) is None
    assert db.query(AIJob).count() == 1


def test_superseded_job_does_not_write(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    other = enqueue(db, "proposal_directions", {"proposal_id": p.id}, None)
    db.refresh(p)
    p.active_job_id = other.id
    db.commit()
    _run(app, settings)  # 첫 작업은 더 이상 현재 작업이 아니다
    db.refresh(p)
    assert p.directions == [] and p.status == "new" and p.active_job_id == other.id
    assert db.query(AIJob).filter_by(result="superseded").count() == 1


def test_select_refused_while_job_active(user_client, db, settings):
    p, s1, s2 = _with_directions(db, settings)
    user_client.post(f"/proposals/{p.id}/directions")
    r = user_client.post(f"/proposals/{p.id}/select", data={"index": "0"}, follow_redirects=False)
    assert r.status_code == 303 and "notice=busy" in r.headers["location"]
    db.refresh(p)
    assert p.selected_index is None and p.status == "directions"


def test_select_with_vanished_solutions(user_client, db, settings):
    p, s1, s2 = _with_directions(db, settings)
    p.directions = [{**p.directions[0], "solutions": [{"solution_id": 9999, "name": "x", "role": ""}]}] + p.directions[1:]
    db.commit()
    r = user_client.post(f"/proposals/{p.id}/select", data={"index": "0"})
    assert r.status_code == 400 and "솔루션이 더 이상 없습니다" in r.text
    db.refresh(p)
    assert p.selected_index is None


def test_regeneration_clears_solutions(app, user_client, db, settings, fake_claude):
    p, s1, s2 = _with_directions(db, settings)
    user_client.post(f"/proposals/{p.id}/select", data={"index": "0"})
    db.refresh(p)
    assert len(p.solutions) == 2
    _set(fake_claude, good_directions(s1, s2))
    user_client.post(f"/proposals/{p.id}/directions")
    _run(app, settings)
    db.refresh(p)
    assert p.solutions == []


def test_delete_proposal_with_failed_job(app, admin_client, db, settings, fake_claude):
    p, *_ = make_proposal_world(db, settings)
    pid = p.id
    fake_claude.set(mode="error", stderr="Please run /login", exit_code=1)
    admin_client.post(f"/proposals/{pid}/directions")
    _run(app, settings)
    r = admin_client.post(f"/proposals/{pid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Proposal, pid) is None


def test_direction_without_rationale_needs_check(app, user_client, db, settings, fake_claude):
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    data = good_directions(s1, s2)
    data["directions"][1]["rationale"] = []
    user_client.post(f"/proposals/{p.id}/directions")
    _set(fake_claude, data)
    _run(app, settings)
    db.refresh(p)
    assert "근거 자료 없음" in p.directions[1]["needs_check"]
    assert "근거 자료 없음" not in p.directions[0]["needs_check"]
