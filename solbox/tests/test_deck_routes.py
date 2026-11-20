import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from pptx import Presentation

from app.config import Settings
from app.knowledge import safe_name
from app.main import create_app
from app.models import AIJob, Proposal
from app.proposals import deck_is_stale, display_title
from tests.conftest import _logged_in_client, outlined_proposal

STALE = "구성안이 바뀌었습니다. 다시 생성하세요."
FAILED = "제안서 파일을 만들지 못했습니다."


def _files(settings, p):
    folder = settings.decks_dir / str(p.id)
    return sorted(folder.iterdir()) if folder.exists() else []


def test_build_and_download(user_client, db, settings):
    p = outlined_proposal(db, settings)
    r = user_client.post(f"/proposals/{p.id}/deck", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/proposals/{p.id}#deck"
    db.refresh(p)
    assert p.status == "deck" and (settings.decks_dir / str(p.id) / p.deck_name).exists()
    assert p.deck_built_at is not None
    d = user_client.get(f"/proposals/{p.id}/deck")
    assert d.content[:2] == b"PK"
    assert d.headers["content-type"] == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    assert d.headers["content-disposition"] == (
        f'attachment; filename="proposal-{p.id}.pptx"; filename*=UTF-8\'\'' + quote(safe_name(display_title(p)) + ".pptx"))


def test_build_back_to_outline(user_client, db, settings):
    p = outlined_proposal(db, settings)
    r = user_client.post(f"/proposals/{p.id}/deck", data={"back": "outline"}, follow_redirects=False)
    assert r.headers["location"] == f"/proposals/{p.id}/outline#deck"


def test_rebuild_replaces_old_file(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/deck")
    db.refresh(p)
    first = p.deck_name
    user_client.post(f"/proposals/{p.id}/deck")  # 같은 초 안에 다시 만들어도 이름이 겹치지 않는다
    db.refresh(p)
    assert p.deck_name != first
    files = _files(settings, p)
    assert [f.name for f in files] == [p.deck_name]


def test_stale_after_outline_edit(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/deck")
    fresh = user_client.get(f"/proposals/{p.id}").text
    assert "PPT 내려받기" in fresh and STALE not in fresh
    user_client.post(f"/proposals/{p.id}/outline/slides/1", data={"kind": "bullets", "title": "바뀜", "bullets": "x"})
    page = user_client.get(f"/proposals/{p.id}").text
    assert STALE in page and "PPT 내려받기" not in page
    db.refresh(p)
    assert deck_is_stale(p)
    assert STALE in user_client.get(f"/proposals/{p.id}/outline").text
    assert user_client.get(f"/proposals/{p.id}/deck").content[:2] == b"PK"  # 직접 요청하면 낡은 파일도 내려간다
    user_client.post(f"/proposals/{p.id}/deck")
    again = user_client.get(f"/proposals/{p.id}").text
    assert "PPT 내려받기" in again and STALE not in again
    db.refresh(p)
    assert not deck_is_stale(p)


def test_build_failure_keeps_previous(user_client, db, settings, monkeypatch):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/deck")
    db.refresh(p)
    name, built = p.deck_name, p.deck_built_at

    def boom(*args, **kwargs):
        Path(args[1]).write_bytes(b"half")
        raise RuntimeError("boom")

    monkeypatch.setattr("app.routers.proposals.build_deck", boom)
    r = user_client.post(f"/proposals/{p.id}/deck")
    assert r.status_code == 400 and FAILED in r.text
    db.refresh(p)
    assert p.deck_name == name and p.deck_built_at == built and p.status == "deck"
    assert [f.name for f in _files(settings, p)] == [name]


def test_no_outline_rejected_and_missing_file_404(user_client, db, settings):
    p = outlined_proposal(db, settings)
    p.outline = None
    p.status = "refining"
    db.commit()
    assert user_client.post(f"/proposals/{p.id}/deck").status_code == 400
    assert user_client.get(f"/proposals/{p.id}/deck").status_code == 404
    assert "PPT 만들기" not in user_client.get(f"/proposals/{p.id}").text
    assert user_client.post("/proposals/999/deck").status_code == 404


def test_deck_refused_while_ai_busy(user_client, db, settings):
    p = outlined_proposal(db, settings)
    job = AIJob(kind="proposal_chat", input={}, status="running")
    db.add(job)
    db.flush()
    p.active_job_id = job.id
    db.commit()
    r = user_client.post(f"/proposals/{p.id}/deck")
    assert r.status_code == 400 and "AI 작업이 끝난 뒤 생성하세요." in r.text
    assert _files(settings, p) == []


def test_needs_check_notice(user_client, db, settings):
    p = outlined_proposal(db, settings)
    assert "확인할 항목이 남은 장: 확인할 점 1장" in user_client.get(f"/proposals/{p.id}").text
    assert "확인할 항목이 남은 장: 확인할 점 1장" in user_client.get(f"/proposals/{p.id}/outline").text
    slides = [dict(s, needs_check=[]) for s in p.outline["slides"]]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    assert "확인할 항목이 남은 장" not in user_client.get(f"/proposals/{p.id}").text.split('id="deck"')[1]


def test_works_without_ai(tmp_path, db, settings):
    p = outlined_proposal(db, settings)
    off = Settings(
        data_dir=settings.data_dir, secret_key="test", ai_worker_enabled=False,
        claude_cmd=[str(tmp_path / "no-such-claude")],
    )
    app2 = create_app(off)
    try:
        s2 = app2.state.session_factory()
        try:
            c = _logged_in_client(app2, s2, "lee", "user")
        finally:
            s2.close()
        assert c.post(f"/proposals/{p.id}/deck", follow_redirects=False).status_code == 303
        assert c.get(f"/proposals/{p.id}/deck").content[:2] == b"PK"
    finally:
        app2.state.engine.dispose()


def test_built_deck_contains_edit(user_client, db, settings):
    p = outlined_proposal(db, settings)
    user_client.post(f"/proposals/{p.id}/outline/slides/1", data={"kind": "bullets", "title": "고친 제목", "bullets": "고친 줄"})
    user_client.post(f"/proposals/{p.id}/deck")
    db.refresh(p)
    prs = Presentation(settings.decks_dir / str(p.id) / p.deck_name)
    text = " ".join(sh.text_frame.text for sl in prs.slides for sh in sl.shapes if sh.has_text_frame)
    assert "고친 제목" in text and "한빛병원" in text


def test_delete_proposal_removes_deck_folder(admin_client, db, settings):
    p = outlined_proposal(db, settings)
    pid = p.id
    admin_client.post(f"/proposals/{pid}/deck")
    assert _files(settings, p)
    admin_client.post(f"/proposals/{pid}/delete")
    assert not (settings.decks_dir / str(pid)).exists()


def test_home_lists_open_proposals(user_client, db, settings):
    p = outlined_proposal(db, settings)
    done = Proposal(customer_id=p.customer_id, title="끝난 제안 건", status="deck")
    db.add(done)
    db.commit()
    page = user_client.get("/").text
    assert "진행 중인 제안 건" in page
    assert f'href="/proposals/{p.id}"' in page and "구성안" in page and "한빛병원" in page
    assert f'href="/proposals/{done.id}"' not in page


def test_home_open_proposals_limited_to_five(user_client, db, settings):
    p = outlined_proposal(db, settings)
    for i in range(6):
        db.add(Proposal(customer_id=p.customer_id, title=f"추가 {i}", status="new", updated_at=datetime.now() + timedelta(minutes=i + 1)))
    db.commit()
    page = user_client.get("/").text
    assert len(re.findall(r'href="/proposals/\d+"', page)) == 5 and "추가 5" in page and f'href="/proposals/{p.id}"' not in page
