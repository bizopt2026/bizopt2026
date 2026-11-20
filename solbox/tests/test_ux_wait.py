"""AI 대기 안내: 종류별 예상 시간과 서버가 계산한 경과 시간."""
from datetime import datetime, timedelta

import pytest

from app import labels
from app.ai.queue import enqueue
from app.models import User


def _job(db, kind="solution_draft", **kw):
    kim = db.query(User).filter_by(username="kim").one()
    j = enqueue(db, kind, {}, kim.id)
    for k, v in kw.items():
        setattr(j, k, v)
    db.commit()
    return j


def test_expected_texts_cover_every_job_kind():
    assert labels.JOB_EXPECTED["activity_structure"] == "보통 20초 안에"
    assert labels.JOB_EXPECTED["solution_draft"] == "보통 30초 안에"
    assert labels.JOB_EXPECTED["research_area"] == "분야마다 1~2분"
    assert labels.JOB_EXPECTED["ai_ping"] == "보통 10초 안에"
    assert set(labels.JOB_EXPECTED) == set(labels.JOB_KINDS)


@pytest.mark.parametrize("seconds, text", [(0, "0초"), (35, "35초"), (59, "59초"), (60, "1분"), (95, "1분 35초"), (-5, "0초")])
def test_elapsed_text(seconds, text):
    now = datetime(2026, 11, 18, 12, 0, 0)
    assert labels.elapsed_text(now - timedelta(seconds=seconds), now) == text + " 경과"


def test_wait_detail_combines_expected_and_elapsed():
    now = datetime(2026, 11, 18, 12, 0, 0)
    class J:  # noqa: D401
        kind = "proposal_outline"; started_at = now - timedelta(seconds=35)
    assert labels.wait_detail(J, now) == "(보통 1분 안에 끝납니다 · 35초 경과)"
    J.kind = "research_area"
    assert labels.wait_detail(J, now) == "(분야마다 1~2분 · 35초 경과)"
    J.kind = "nope"
    assert labels.wait_detail(J, now) == "(35초 경과)"
    J.started_at = None
    assert labels.wait_detail(J, now) == ""


def test_job_status_fragment_shows_expected_and_elapsed(user_client, db, monkeypatch):
    now = datetime(2026, 11, 18, 12, 0, 0)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(labels, "datetime", FrozenDatetime)
    j = _job(db, "solution_draft", status="running", started_at=now - timedelta(seconds=35))
    html = user_client.get(f"/jobs/{j.id}/status").text
    assert "AI가 작성 중입니다… (보통 30초 안에 끝납니다 · 35초 경과)" in html


def test_pending_job_explains_the_queue(user_client, db):
    j = _job(db)
    assert "AI 대기 1번째 (앞 작업이 끝나면 시작합니다)" in user_client.get(f"/jobs/{j.id}/status").text


def test_research_status_keeps_going_note_and_queue_hint(admin_client, user_client, db):
    from app.models import ResearchRun
    from tests.conftest import make_topics

    make_topics(db)
    admin_client.post("/research/run")
    run = db.query(ResearchRun).one()
    html = user_client.get(f"/research/runs/{run.id}/status").text
    assert "다른 화면으로 가도 조사는 계속 진행됩니다." in html
    assert "AI 대기 1번째" in html and "앞 작업이 끝나면 시작합니다" in html
