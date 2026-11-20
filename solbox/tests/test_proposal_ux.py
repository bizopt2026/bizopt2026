"""제안 상세 화면: 단계 표시줄, 구역 접기, 구역 요약."""
import re
from datetime import datetime

from conftest import make_proposal_world, outlined_proposal, refining_proposal
from app.models import AIJob, ProposalMessage
from app.proposals import candidate_solutions, open_section, section_summaries, step_states


def _states(p):
    return {s["key"]: s["state"] for s in step_states(p)}


def test_step_states_new_proposal(db, settings):
    p = make_proposal_world(db, settings)[0]
    assert [s["key"] for s in step_states(p)] == ["target", "directions", "chat", "outline", "deck"]
    assert [s["label"] for s in step_states(p)] == ["대상", "방향", "구체화", "구성안", "제안서"]
    assert _states(p) == {"target": "done", "directions": "current", "chat": "locked", "outline": "locked", "deck": "locked"}
    assert all(s["href"] for s in step_states(p) if s["state"] != "locked") and all(s["href"] is None for s in step_states(p) if s["state"] == "locked")
    assert step_states(p)[1]["href"] == "#directions"


def test_step_states_follow_status(db, settings):
    p = refining_proposal(db, settings)
    assert _states(p) == {"target": "done", "directions": "done", "chat": "current", "outline": "locked", "deck": "locked"}
    p = outlined_proposal(db, settings)
    assert _states(p) == {"target": "done", "directions": "done", "chat": "done", "outline": "current", "deck": "todo"}
    p.status = "deck"
    assert _states(p) == {"target": "done", "directions": "done", "chat": "done", "outline": "done", "deck": "current"}


def test_open_section_by_status_and_job(db, settings):
    p = make_proposal_world(db, settings)[0]
    assert open_section(p) == "directions"
    p.status = "directions"
    assert open_section(p) == "directions"
    for status, key in (("refining", "chat"), ("outlined", "outline"), ("deck", "deck")):
        p.status = status
        assert open_section(p) == key
    p.status = "refining"
    job = AIJob(kind="proposal_outline", input={}, status="running", priority=0)
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.flush()
    db.refresh(p)
    assert open_section(p) == "outline"
    job.status = "done"
    assert open_section(p) == "chat"


def test_section_summaries(db, settings):
    p = make_proposal_world(db, settings)[0]
    s = section_summaries(p, candidate_solutions(db, p))
    assert s["target"] == "한빛병원 · 설비 유지보수 — 조합 솔루션 AI 에너지 절감, 스마트 조명"
    assert s["directions"] == "아직 만들지 않음"
    assert s["chat"] == "방향을 선택하면 시작할 수 있습니다"
    assert s["outline"] == s["deck"] == "아직 만들지 않음"
    p = refining_proposal(db, settings)
    s = section_summaries(p, candidate_solutions(db, p))
    assert s["directions"] == "선택한 방향: 전력비 절감 패키지"
    assert s["chat"] == "대화 없음"
    p.messages.append(ProposalMessage(role="user", content="안녕"))
    p.messages.append(ProposalMessage(role="assistant", content="네", created_at=datetime(2026, 5, 3, 14, 5)))
    db.commit()
    assert section_summaries(p, [])["chat"] == "대화 2개 · 마지막 메시지 2026-05-03 14:05"
    p.selected_index = None
    assert section_summaries(p, [])["directions"] == "방향 2개"


def test_section_summaries_outline_and_deck(db, settings):
    p = outlined_proposal(db, settings)
    s = section_summaries(p, [])
    assert s["outline"] == "8장 · 확인할 점 1장"
    assert s["deck"] == "아직 만들지 않음"
    p.deck_name, p.deck_built_at = "a.pptx", datetime(2026, 5, 4, 9, 30)
    assert section_summaries(p, [])["deck"] == "마지막 생성 2026-05-04 09:30"


def _open_ids(html):
    return set(re.findall(r'<details class="card section" id="(\w+)" open', html))


def test_detail_page_opens_only_current_section(user_client, db, settings):
    p = refining_proposal(db, settings)
    html = user_client.get(f"/proposals/{p.id}").text
    assert _open_ids(html) == {"chat"}
    assert 'aria-current="step"' in html and 'class="stepbar"' in html
    for key in ("target", "directions", "chat", "outline", "deck"):
        assert f'id="{key}"' in html
    assert "선택한 방향: 전력비 절감 패키지" in html
    assert "다른 방향 1개 보기" in html


def test_detail_page_directions_all_visible_before_selection(user_client, db, settings):
    p = refining_proposal(db, settings)
    p.selected_index, p.status = None, "directions"
    db.commit()
    html = user_client.get(f"/proposals/{p.id}").text
    assert _open_ids(html) == {"directions"}
    assert "다른 방향" not in html and "전력비 절감 패키지" in html and "조명 단독 교체" in html


def test_detail_chat_collapses_old_messages(user_client, db, settings):
    p = refining_proposal(db, settings)
    for i in range(8):
        p.messages.append(ProposalMessage(role="user" if i % 2 == 0 else "assistant", content=f"메시지{i}"))
    db.commit()
    html = user_client.get(f"/proposals/{p.id}").text
    assert "이전 대화 2개 보기" in html
    assert "메시지0" in html and "메시지7" in html
    assert 'name="message"' in html


def test_detail_chat_six_messages_not_collapsed(user_client, db, settings):
    p = refining_proposal(db, settings)
    for i in range(6):
        p.messages.append(ProposalMessage(role="user", content=f"m{i}"))
    db.commit()
    assert "이전 대화" not in user_client.get(f"/proposals/{p.id}").text


def test_chat_validation_error_keeps_chat_open(user_client, db, settings):
    p = outlined_proposal(db, settings)
    r = user_client.post(f"/proposals/{p.id}/chat", data={"message": " "})
    assert r.status_code == 400
    assert "chat" in _open_ids(r.text)


def test_running_job_opens_its_section(user_client, db, settings):
    p = refining_proposal(db, settings)
    job = AIJob(kind="proposal_outline", input={"proposal_id": p.id}, status="running", priority=0)
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.commit()
    html = user_client.get(f"/proposals/{p.id}").text
    assert _open_ids(html) == {"outline"}
    assert 'id="ai-status"' in html


def test_changing_direction_still_asks_for_confirmation(user_client, db, settings):
    p = outlined_proposal(db, settings)
    html = user_client.get(f"/proposals/{p.id}").text
    assert html.count("방향을 바꾸면 만들어 둔 구성안과 제안서가 지워집니다") >= 2  # 다른 방향 선택 + 방향 다시 만들기


def test_step_bar_marks_running_later_step(db, settings):
    p = refining_proposal(db, settings)
    job = AIJob(kind="proposal_outline", input={"proposal_id": p.id}, status="running", priority=0)
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.flush()
    db.refresh(p)
    steps = {s["key"]: s for s in step_states(p)}
    assert steps["outline"]["busy"] and steps["outline"]["href"] == "#outline" and steps["chat"]["state"] == "current"
    assert not steps["chat"]["busy"] and not steps["deck"]["busy"]


def test_step_bar_shows_running_label(user_client, db, settings):
    p = refining_proposal(db, settings)
    job = AIJob(kind="proposal_outline", input={"proposal_id": p.id}, status="running", priority=0)
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.commit()
    html = user_client.get(f"/proposals/{p.id}").text
    nav = re.search(r'<nav class="stepbar".*?</nav>', html, re.S).group(0)
    assert nav.count("진행 중") == 1 and 'href="#outline"' in nav


def test_detail_page_uses_shared_script_only(user_client, db, settings):
    html = user_client.get(f"/proposals/{refining_proposal(db, settings).id}").text
    assert '/static/app.js' in html and "decodeURIComponent" not in html
