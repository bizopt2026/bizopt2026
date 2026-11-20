"""올리기 화면 개선 E2: 음성 안내, 큰 드롭존, 메뉴 배지, 좁은 화면 표, AI 안 될 때 안내."""
import io
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import event

from app import intake as intake_lib
from app import transcribe
from app.models import AIJob, Intake, User
from tests.conftest import make_user, ready_bundle

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
AUDIO_NOTE = "받아쓰기 필요 · 글자로 풀어 올려 주세요"
DROP_NOTE = "음성 파일은 보관만 됩니다 (받아쓰기 준비 중)"


def _upload(client, files):
    return client.post("/intake", data={"note": ""}, files=[("files", f) for f in files], follow_redirects=False)


def _ok_transcriber(monkeypatch):
    class Ok(transcribe.Transcriber):
        def available(self):
            return True

        def transcribe(self, path):
            return "받아쓴 글"

    monkeypatch.setitem(transcribe.TRANSCRIBERS, "none", Ok)


def _bundle(db, user_name="kim", status="ready", note=""):
    user = db.query(User).filter_by(username=user_name).one()
    it = Intake(note=note, status=status, created_by_id=user.id)
    db.add(it)
    db.commit()
    return it


# ---- U5 음성 안내 ----

def test_dropzone_audio_note_only_when_transcriber_missing(user_client, monkeypatch):
    for url in ("/intake/new", "/"):
        assert DROP_NOTE in user_client.get(url).text
    _ok_transcriber(monkeypatch)
    for url in ("/intake/new", "/"):
        assert DROP_NOTE not in user_client.get(url).text


def test_bundle_page_has_no_top_banner_but_note_on_audio_row(user_client, db):
    r = _upload(user_client, [("회의.m4a", b"\x00\x01", "audio/mp4"), ("메모.txt", "내용".encode(), "text/plain")])
    page = user_client.get(r.headers["location"]).text
    assert 'class="notice"' not in page and "아직 받아쓰기를 지원하지 않습니다" not in page
    rows = re.findall(r"<tr>.*?</tr>", page, re.S)
    audio = next(x for x in rows if "회의.m4a" in x)
    text_row = next(x for x in rows if "메모.txt" in x)
    assert AUDIO_NOTE in audio and AUDIO_NOTE not in text_row


def test_audio_note_gone_after_apply_and_with_transcriber(user_client, db, settings, monkeypatch):
    r = _upload(user_client, [("회의.m4a", b"\x00\x01", "audio/mp4")])
    it = db.query(Intake).one()
    it.status = "applied"
    db.commit()
    assert AUDIO_NOTE not in user_client.get(f"/intake/{it.id}").text


# ---- U6 드롭존 ----

def test_dropzone_whole_area_is_label_with_native_input():
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "intake" / "_dropzone.html").read_text(encoding="utf-8")
    m = re.search(r'<label class="dropzone-area"[^>]*>(.*?)</label>', html, re.S)
    assert m and 'type="file"' in m.group(1) and "파일을 여기에 끌어다 놓거나 눌러서 선택" in m.group(1)
    assert "hidden" not in re.search(r'<input[^>]*type="file"[^>]*>', html).group(0)  # 스크립트 없이도 쓸 수 있다


def test_dropzone_pages_and_styles(user_client):
    for url in ("/intake/new", "/"):
        page = user_client.get(url).text
        assert "파일을 여기에 끌어다 놓거나 눌러서 선택" in page and "pptx" in page and "dropzone-types" in page
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".dropzone.over .dropzone-area" in css and ".dropzone-area:focus-within" in css
    assert "min-height" in re.search(r"\.dropzone-area \{[^}]*\}", css).group(0)
    assert ".dropzone.enhanced" in css
    assert "enhanced" in (STATIC / "app.js").read_text(encoding="utf-8")


# ---- U8 메뉴 배지 ----

def _nav_badge(page):
    m = re.search(r'<span class="nav-badge"[^>]*aria-label="([^"]*)"[^>]*>(\d+)</span>', page)
    return (m.group(1), m.group(2)) if m else None


def test_nav_badge_counts_own_ready_bundles(user_client, admin_client, db):
    kim = db.query(User).filter_by(username="kim").one()
    assert _nav_badge(user_client.get("/solutions").text) is None  # 0건이면 숨긴다
    for status in ("ready", "ready", "analyzing", "applied"):
        _bundle(db, "kim", status)
    _bundle(db, "admin", "ready")
    assert _nav_badge(user_client.get("/solutions").text) == ("확인 대기 2건", "2")
    assert _nav_badge(admin_client.get("/").text) == ("확인 대기 1건", "1")  # 관리자도 내 것만
    page = user_client.get("/solutions").text
    assert re.search(r'<a href="/intake"[^>]*>올리기\s*<span class="nav-badge"', page)


def test_nav_badge_is_one_cheap_count_and_login_page_is_safe(app, user_client, db):
    _bundle(db, "kim", "ready")
    statements = []

    @event.listens_for(app.state.engine, "before_cursor_execute")
    def grab(conn, cursor, statement, params, context, executemany):
        if "intakes" in statement:
            statements.append(statement)

    try:
        user_client.get("/solutions")
    finally:
        event.remove(app.state.engine, "before_cursor_execute", grab)
    assert len(statements) == 1 and "count(" in statements[0].lower()
    from fastapi.testclient import TestClient
    anon = TestClient(app)
    assert anon.get("/login").status_code == 200 and "nav-badge" not in anon.get("/login").text
    assert "nav-badge" in (STATIC / "app.css").read_text(encoding="utf-8")


# ---- U7 좁은 화면 표 ----

def test_file_table_has_narrow_layout_hooks(user_client, db):
    r = _upload(user_client, [("회의.m4a", b"\x00\x01", "audio/mp4")])
    page = user_client.get(r.headers["location"]).text
    assert page.count('class="col-num"') == 2 and page.count('class="col-size"') == 2  # 머리 칸과 본문 칸
    assert 'class="name-status"' in page
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    block = re.search(r"@media \(max-width: 480px\) \{(.*?)\n\}", css, re.S).group(1)
    assert ".col-num" in block and ".col-size" in block and ".name-status" in block and "display:none" in block.replace(" ", "")


# ---- U9 AI 안 될 때 안내 ----

AI_DOWN = "지금은 AI 정리를 쓸 수 없어요. 파일은 올려 둘 수 있고, 급하면 직접 입력을 써 주세요."


def _failed_job(db, kind="auth"):
    db.add(AIJob(kind="intake_analyze", input={}, status="failed", error_kind=kind, finished_at=datetime.now()))
    db.commit()


def test_ai_down_notice_on_new_and_home(user_client, db):
    for url in ("/intake/new", "/"):
        assert AI_DOWN not in user_client.get(url).text
    _failed_job(db)
    for url in ("/intake/new", "/"):
        page = user_client.get(url).text
        assert AI_DOWN in page
        assert 'href="/solutions/new"' in page and 'href="/activities/new"' in page
    # 오류가 아닌 실패(기타)는 AI 가 안 되는 것으로 보지 않는다
    db.query(AIJob).delete()
    db.commit()
    _failed_job(db, "unknown")
    assert AI_DOWN not in user_client.get("/intake/new").text


def test_ai_down_notice_on_new_page_after_error(user_client, db):
    _failed_job(db)
    r = user_client.post("/intake", data={"note": ""}, files=[])
    assert r.status_code == 400 and AI_DOWN in r.text
