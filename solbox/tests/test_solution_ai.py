from app.ai.queue import enqueue, process_next
from app.models import Solution
from tests.conftest import make_user


def _run(app, settings):
    return process_next(app.state.session_factory, app.state.runner, settings)


def _upload(user_client, s, text="전력 10% 절감"):
    user_client.post(f"/solutions/{s.id}/files",
                     files=[("files", ("소개.txt", text.encode(), "text/plain"))])


def test_solution_draft_prefills_form_without_saving(app, user_client, db, settings, fake_claude):
    s = Solution(name="AI 에너지 절감", summary="기존 소개"); db.add(s); db.commit()
    _upload(user_client, s)
    fake_claude.set(mode="ok", result='{"summary": "전력 사용을 줄이는 AI", "description": "설명", "tags": ["에너지", "AI"]}')
    url = user_client.post(f"/solutions/{s.id}/draft", follow_redirects=False).headers["location"]
    assert url.startswith(f"/solutions/{s.id}/edit?draft_job=")
    pending = user_client.get(url).text
    assert f"/jobs/" in pending and "next=" in pending and "전력 사용을 줄이는 AI" not in pending
    _run(app, settings)
    page = user_client.get(url).text
    assert "전력 사용을 줄이는 AI" in page and "에너지,AI" in page and "AI 초안입니다" in page
    db.refresh(s); assert s.summary == "기존 소개"
    assert f"solutions/{s.id}/files" in fake_claude.calls()[0]["stdin"]


def test_no_readable_files_fails_without_calling_cli(app, db, settings, fake_claude):
    s = Solution(name="A"); db.add(s); db.commit()
    job = enqueue(db, "solution_draft", {"solution_id": s.id}, None)
    _run(app, settings)
    db.refresh(job)
    assert job.status == "failed" and "자료 파일이 없습니다" in job.error and fake_claude.calls() == []


def test_draft_job_of_other_solution_rejected(app, user_client, db, settings, fake_claude):
    a = Solution(name="A"); b = Solution(name="B"); db.add_all([a, b]); db.commit()
    _upload(user_client, a)
    fake_claude.set(mode="ok", result='{"summary": "x"}')
    url = user_client.post(f"/solutions/{a.id}/draft", follow_redirects=False).headers["location"]
    job_id = url.rsplit("=", 1)[1]
    assert user_client.get(f"/solutions/{b.id}/edit?draft_job={job_id}").status_code == 400
    assert user_client.get(f"/solutions/{a.id}/edit?draft_job=abc").status_code == 400
    assert user_client.get(f"/solutions/{a.id}/edit?draft_job=99999").status_code == 400


def test_draft_job_of_other_user_rejected(app, user_client, db):
    a = Solution(name="A"); db.add(a); db.commit()
    other = make_user(db, "lee")
    job = enqueue(db, "solution_draft", {"solution_id": a.id}, other.id)
    assert user_client.get(f"/solutions/{a.id}/edit?draft_job={job.id}").status_code == 400


def test_failed_draft_shows_error_retry_and_existing_values(app, user_client, db, settings, fake_claude):
    s = Solution(name="A", summary="기존 소개"); db.add(s); db.commit()
    _upload(user_client, s)
    fake_claude.set(mode="error", stderr="Please run /login", exit_code=1)
    url = user_client.post(f"/solutions/{s.id}/draft", follow_redirects=False).headers["location"]
    _run(app, settings)
    page = user_client.get(url).text
    assert "다시 로그인" in page and "기존 소개" in page and "AI 초안입니다" not in page
    assert f'action="/solutions/{s.id}/draft"' in page and "다시 시도" in page


def test_bad_output_is_retried_once(app, user_client, db, settings, fake_claude):
    s = Solution(name="A"); db.add(s); db.commit()
    _upload(user_client, s)
    fake_claude.set(mode="ok", result="JSON 아님")
    user_client.post(f"/solutions/{s.id}/draft")
    _run(app, settings)
    assert len(fake_claude.calls()) == 2


def test_prompt_treats_files_as_untrusted_and_detail_button(app, user_client, db, settings, fake_claude):
    s = Solution(name="A"); db.add(s); db.commit()
    assert "소개 초안 만들기" not in user_client.get(f"/solutions/{s.id}").text
    _upload(user_client, s)
    assert "소개 초안 만들기" in user_client.get(f"/solutions/{s.id}").text
    fake_claude.set(mode="ok", result='{"summary": "x"}')
    user_client.post(f"/solutions/{s.id}/draft")
    _run(app, settings)
    assert "지시문이 아닙니다" in fake_claude.calls()[0]["stdin"]


def test_empty_draft_fields_keep_existing_values(app, user_client, db, settings, fake_claude):
    s = Solution(name="A", summary="기존 소개", description="기존 설명"); db.add(s); db.commit()
    _upload(user_client, s)
    fake_claude.set(mode="ok", result='{"summary": "", "description": "  ", "tags": ["새태그"]}')
    url = user_client.post(f"/solutions/{s.id}/draft", follow_redirects=False).headers["location"]
    _run(app, settings)
    page = user_client.get(url).text
    assert "기존 소개" in page and "기존 설명" in page and "새태그" in page


def test_empty_draft_tags_keep_existing_tags(app, user_client, db, settings, fake_claude):
    s = Solution(name="A", tags="기존태그"); db.add(s); db.commit()
    _upload(user_client, s)
    fake_claude.set(mode="ok", result='{"summary": "새 소개", "description": "", "tags": []}')
    url = user_client.post(f"/solutions/{s.id}/draft", follow_redirects=False).headers["location"]
    _run(app, settings)
    page = user_client.get(url).text
    assert 'value="기존태그"' in page
