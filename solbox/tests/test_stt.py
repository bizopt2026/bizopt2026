"""음성 받아쓰기(서버 안, faster-whisper): 설정·엔진 연결·분석 작업 안의 받아쓰기·진행 표시·복구.

실제 `claude` 도, 실제 음성 모델도 쓰지 않는다. 가짜 엔진을 `TRANSCRIBERS` 에 등록해 쓴다."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import cli, intake as intake_lib, transcribe
from app.ai import intake_tasks, queue
from app.ai.queue import fail_stale_running, process_next
from app.config import Settings, load_settings
from app.db import make_engine
from app.migrate import add_missing_columns
from app.models import AIJob, Customer, Intake, IntakeFile, Partner, Solution
from tests.conftest import FAKE_CLAUDE

AUDIO = ("회의.m4a", b"\x00\x01fake-audio", "audio/mp4")
AUDIO2 = ("통화.mp3", b"\x00\x02fake-audio", "audio/mpeg")
DOC = ("자료.txt", "문서 내용".encode(), "text/plain")
PLAN = json.dumps({"activities": [{"draft": {"kind": "meeting", "title": "회의", "discussion": ["내용"]},
                                   "raw_from": 1, "files": [1], "reason": "녹취"}]}, ensure_ascii=False)


class Crash(BaseException):
    """서버가 죽는 상황 흉내 (처리기의 `except Exception` 에 잡히지 않는다)."""


class FakeEngine(transcribe.Transcriber):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []  # (저장 파일 이름, hints)
        self.text = "안녕하세요 통화 내용입니다"
        self.errors: dict[str, BaseException] = {}  # 저장 파일 이름 → 던질 예외 (없으면 `error`)
        self.error: BaseException | None = None
        self.steps = (0.2, 0.6, 1.0)
        self.on_step = None  # 진행 알림 직전에 부르는 함수 (이름, 단계 번호)
        self.on_done = None  # 받아쓰기를 마치기 직전에 부르는 함수 (이름)
        self.model = "small"
        self.language = ""
        self.pause_s = 0.0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return True

    def model_name(self) -> str:
        return self.model

    def language_name(self) -> str:
        return self.language

    def transcribe(self, path, *, hints="", on_progress=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((Path(path).name, hints))
            err = self.errors.get(Path(path).name, self.error)
            if err is not None:
                raise err
            for i, step in enumerate(self.steps):
                if self.on_step:
                    self.on_step(Path(path).name, i)
                if on_progress:
                    on_progress(step)
            if self.pause_s:
                time.sleep(self.pause_s)
            if self.on_done:
                self.on_done(Path(path).name)
            return self.text
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def stt(settings, monkeypatch):
    fake = FakeEngine()

    class Registered(FakeEngine):
        @classmethod
        def from_settings(cls, s):
            return fake

    monkeypatch.setitem(transcribe.TRANSCRIBERS, "faster-whisper", Registered)
    settings.transcriber = "faster-whisper"
    monkeypatch.setattr(intake_tasks, "PROGRESS_INTERVAL_S", 0.0)
    return fake


def _upload(client, files, note=""):
    return client.post("/intake", data={"note": note}, files=[("files", f) for f in files], follow_redirects=False)


def _run(app, settings):
    return process_next(app.state.session_factory, app.state.runner, settings)


def _latest(db) -> Intake:
    db.expire_all()
    return db.query(Intake).order_by(Intake.id.desc()).first()


def _files(db, it):
    db.expire_all()
    return sorted(db.query(IntakeFile).filter_by(intake_id=it.id), key=lambda f: f.id)


def _cache(settings, f):
    return intake_lib.stored_path(settings, f).with_name(f.stored_name + ".transcript.txt")


# ---- 설정 ----

def _config(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "data"\nsecret_key = "k"\n{extra}', encoding="utf-8")
    return cfg


def test_settings_defaults(tmp_path):
    s = load_settings(_config(tmp_path))
    assert (s.transcriber, s.transcribe_model, s.transcribe_language, s.transcribe_threads, s.transcribe_max_minutes) == \
        ("none", "small", "ko", 0, 120)
    assert s.models_dir == s.data_dir / "models"


@pytest.mark.parametrize("name", ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"])
def test_settings_accept_model_names_and_engine(tmp_path, name):
    s = load_settings(_config(tmp_path, f'transcriber = "faster-whisper"\ntranscribe_model = "{name}"\n'))
    assert s.transcriber == "faster-whisper" and s.transcribe_model == name


def test_settings_local_model_folder_path(tmp_path):
    (tmp_path / "mymodel").mkdir()
    s = load_settings(_config(tmp_path, 'transcribe_model = "mymodel/"\n'))
    assert Path(s.transcribe_model) == (tmp_path / "mymodel")  # 상대 경로는 설정 파일 기준
    s = load_settings(_config(tmp_path, f"transcribe_model = '{tmp_path / 'abs'}'\n"))
    assert Path(s.transcribe_model) == tmp_path / "abs"


@pytest.mark.parametrize("extra,word", [
    ('transcribe_model = "smal"\n', "transcribe_model"),
    ('transcribe_model = ""\n', "transcribe_model"),
    ('transcribe_model = 3\n', "transcribe_model"),
    ('transcribe_language = ""\n', "transcribe_language"),
    ('transcribe_language = 5\n', "transcribe_language"),
    ('transcribe_threads = -1\n', "transcribe_threads"),
    ('transcribe_threads = "4"\n', "transcribe_threads"),
    ('transcribe_threads = true\n', "transcribe_threads"),
    ('transcribe_max_minutes = 0\n', "transcribe_max_minutes"),
    ('transcribe_max_minutes = 1.5\n', "transcribe_max_minutes"),
])
def test_settings_validation_messages(tmp_path, extra, word):
    with pytest.raises(SystemExit) as e:
        load_settings(_config(tmp_path, extra))
    assert word in str(e.value) and "설정 파일" in str(e.value)


def test_unknown_engine_lists_faster_whisper(tmp_path):
    with pytest.raises(SystemExit) as e:
        load_settings(_config(tmp_path, 'transcriber = "whisper"\n'))
    assert "faster-whisper" in str(e.value) and "none" in str(e.value)


def test_example_config_and_extra_documented():
    root = Path(__file__).resolve().parent.parent
    example = (root / "config.example.toml").read_text(encoding="utf-8")
    for key in ("transcriber", "transcribe_model", "transcribe_language", "transcribe_threads", "transcribe_max_minutes"):
        assert key in example
    import tomllib
    meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["optional-dependencies"]["stt"] == ["faster-whisper>=1.1", "av<19"]
    assert not any("whisper" in d for d in meta["project"]["dependencies"])


# ---- 엔진 (faster-whisper 연결 코드, 라이브러리·모델 없이) ----

def _fw_settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "data", secret_key="t", transcriber="faster-whisper", **kw)


def test_module_import_does_not_import_library():
    src = Path(transcribe.__file__).read_text(encoding="utf-8")
    top = [ln for ln in src.splitlines() if ln.startswith(("import faster_whisper", "from faster_whisper"))]
    assert top == []


def test_missing_package_is_unavailable_with_korean_label(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)
    t = transcribe.get(_fw_settings(tmp_path))
    assert isinstance(t, transcribe.FasterWhisperTranscriber) and t.available() is False
    assert t.label() == 'faster-whisper 미설치: pip install -e ".[stt]"'
    with pytest.raises(transcribe.TranscribeError, match="미설치"):
        t.transcribe(tmp_path / "x.wav")


def test_missing_local_model_folder_points_to_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    s = _fw_settings(tmp_path, transcribe_model=str(tmp_path / "없는모델"))
    t = transcribe.get(s)
    assert t.available() is False and "stt-setup" in t.label()
    (tmp_path / "없는모델").mkdir()
    assert t.available() is False  # model.bin 이 있어야 모델 폴더다
    (tmp_path / "없는모델" / "model.bin").write_bytes(b"x")
    (tmp_path / "없는모델" / "tokenizer.json").write_bytes(b"x")
    (tmp_path / "없는모델" / "vocabulary.json").write_bytes(b"x")
    assert t.available() is True and t.label() == "faster-whisper, 없는모델 폴더, CPU".replace("없는모델 폴더", t.model_label())


def test_named_model_not_downloaded_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    monkeypatch.setattr(transcribe, "_cached_model_dir", lambda name, root: None)
    t = transcribe.get(_fw_settings(tmp_path))
    assert not t.available() and "stt-setup" in t.label()
    monkeypatch.setattr(transcribe, "_cached_model_dir", lambda name, root: root / "m")
    assert t.available() and t.label() == "faster-whisper, small, CPU"





def test_format_segments_exact():
    segs = [(0.0, 5.0, "가"), (5.0, 30.0, "나"), (61.0, 65.0, "다"), (70.0, 80.0, "라"), (125.0, 130.0, "마")]
    assert transcribe.format_segments(segs) == "[00:00] 가\n나\n[01:01] 다\n라\n[02:05] 마"
    assert transcribe.format_segments([(0.0, 1.0, "  "), (1.0, 2.0, "글")]) == "[00:01] 글"


# ---- 힌트 ----

def test_build_hints_dedupes_caps_and_keeps_order():
    names = ["한빛병원", "루미텍", "한빛 병원", "A", " 루미텍 ", "AI 공조\n최적제어"]
    out = intake_lib.build_hints(names, "신규 후보 자료")
    assert out.index("한빛병원") < out.index("루미텍") < out.index("AI 공조 최적제어")
    assert out.count("한빛") == 1 and out.count("루미텍") == 1  # 공백만 다른 이름은 한 번
    assert "\n" not in out and out.endswith("신규 후보 자료")
    many = [f"솔루션이름{i:03d}번" for i in range(100)]
    capped = intake_lib.build_hints(many, "메모" * 100)
    assert len(capped) <= intake_lib.HINT_MAX_CHARS
    assert "솔루션이름000번" in capped
    assert intake_lib.build_hints([], "") == ""
    assert intake_lib.build_hints([], "메모만") == "메모만"


def test_build_hints_never_cuts_a_name_in_half():
    names = [f"가나다라마바사{i:02d}" for i in range(60)]
    out = intake_lib.build_hints(names, "")
    parts = [p for p in out.split(", ") if p]
    assert all(len(p) == 9 for p in parts) and len(out) <= intake_lib.HINT_MAX_CHARS


def test_registered_names_most_recent_first(db):
    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1)
    db.add_all([
        Solution(name="오래된솔루션", stage="boxed", created_at=t0),
        Customer(name="새고객", created_at=t0 + timedelta(days=5)),
        Partner(name="중간파트너", created_at=t0 + timedelta(days=2)),
    ])
    db.commit()
    assert intake_lib.registered_names(db) == ["새고객", "중간파트너", "오래된솔루션"]


# ---- 올리기: 요청 안에서는 받아쓰지 않는다 ----

def test_upload_marks_pending_without_transcribing(app, user_client, db, settings, stt):
    r = _upload(user_client, [AUDIO, DOC])
    assert r.status_code == 303
    it = _latest(db)
    assert stt.calls == []  # 업로드 요청은 오래 걸리면 안 된다
    audio, doc = _files(db, it)
    assert (audio.read_status, doc.read_status) == ("pending_transcript", "ok")
    assert it.status == "analyzing"
    assert "받아쓰기 대기" in user_client.get(f"/intake/{it.id}").text


def test_upload_without_engine_keeps_needs_transcript(app, user_client, db, settings):
    _upload(user_client, [AUDIO, DOC])
    it = _latest(db)
    audio, _ = _files(db, it)
    assert audio.read_status == "needs_transcript" and it.status == "analyzing"  # 문서가 있으니 시작은 된다


def test_audio_only_without_engine_still_fails_to_start(app, user_client, db, settings):
    _upload(user_client, [AUDIO])
    it = _latest(db)
    assert it.status == "failed" and it.error == intake_lib.NO_READABLE


def test_audio_only_with_engine_starts_analysis(app, user_client, db, settings, stt):
    _upload(user_client, [AUDIO])
    it = _latest(db)
    assert it.status == "analyzing" and it.job_id is not None


def test_labels_for_new_statuses():
    from app import labels
    assert labels.READ_STATUSES["pending_transcript"] == "받아쓰기 대기"


# ---- 분석 작업 안의 받아쓰기 ----

def test_audio_only_flow_transcribes_then_plans(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO], note="한빛병원 통화")
    it = _latest(db)
    assert _run(app, settings) is True
    it = _latest(db)
    assert it.status == "ready" and it.plan["items"][0]["kind"] == "activity"
    (f,) = _files(db, it)
    assert (f.read_status, f.read_error) == ("ok", None) and f.progress in (None, 100)
    copy = intake_lib.work_dir(settings, it) / "1-회의.m4a.md"
    assert copy.read_text(encoding="utf-8") == "# 회의.m4a\n\n안녕하세요 통화 내용입니다\n"
    cache = _cache(settings, f)
    assert cache.read_text(encoding="utf-8").splitlines()[0] == "#model: small"
    assert "안녕하세요 통화 내용입니다" in cache.read_text(encoding="utf-8")
    listing = (intake_lib.work_dir(settings, it) / "파일목록.md").read_text(encoding="utf-8")
    assert "음성(받아쓰기)" in listing and "1-회의.m4a.md" in listing and "읽음" in listing
    assert len(fake_claude.calls()) == 1
    # 활동 원문은 받아쓴 글 그대로다
    assert "안녕하세요 통화 내용입니다" in json.dumps(it.plan, ensure_ascii=False)


def test_hints_include_registered_names_and_note(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    db.add_all([Customer(name="한빛병원"), Partner(name="루미텍")])
    db.commit()
    _upload(user_client, [AUDIO], note="신규 솔루션 후보")
    _run(app, settings)
    (_, hints), = stt.calls
    assert "한빛병원" in hints and "루미텍" in hints and "신규 솔루션 후보" in hints
    assert len(hints) <= intake_lib.HINT_MAX_CHARS


def test_prompt_marks_transcribed_files(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    _run(app, settings)
    sent = fake_claude.calls()[0]["stdin"]
    assert "음성(받아쓰기)" in sent and "받아쓰기 확인 필요" in sent


def test_engine_failure_audio_only_fails_bundle_with_reason(app, user_client, db, settings, stt, fake_claude, caplog):
    stt.error = transcribe.TranscribeError("음성이 너무 깁니다 (최대 180분)")
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    (f,) = _files(db, it)
    assert it.status == "failed" and "읽을 수 있는 파일이 없습니다" in it.error and "회의.m4a" in it.error
    assert (f.read_status, f.read_error) == ("failed", "음성이 너무 깁니다 (최대 180분)")
    assert fake_claude.calls() == [] and "음성이 너무 깁니다" in caplog.text
    page = user_client.get(f"/intake/{it.id}").text
    assert "음성이 너무 깁니다" in page and "다시 정리" in page
    assert not _cache(settings, f).exists()


def test_engine_failure_mixed_bundle_continues_with_document(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=json.dumps({"activities": []}))
    stt.error = transcribe.TranscribeError("오류")
    _upload(user_client, [AUDIO, DOC])
    _run(app, settings)
    it = _latest(db)
    audio, doc = _files(db, it)
    assert it.status == "ready" and audio.read_status == "failed" and audio.read_error == "오류" and doc.read_status == "ok"
    listing = (intake_lib.work_dir(settings, it) / "파일목록.md").read_text(encoding="utf-8")
    assert "읽지 못함" in listing
    assert "오류" in user_client.get(f"/intake/{it.id}").text  # 실패 이유가 파일 표에 보인다


def test_unexpected_engine_exception_marks_file_failed(app, user_client, db, settings, stt, fake_claude, caplog):
    stt.error = RuntimeError("secret internals")
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    (f,) = _files(db, it)
    assert it.status == "failed" and f.read_status == "failed"
    assert "secret internals" not in (f.read_error or "") and f.read_error
    assert "secret internals" in caplog.text


def test_empty_transcript_is_failure(app, user_client, db, settings, stt):
    stt.text = "   "
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    (f,) = _files(db, it)
    assert f.read_status == "failed" and "글" in f.read_error and it.status == "failed"


def test_cache_reused_on_reanalyze_and_workdir_rebuild(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    assert len(stt.calls) == 1
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    assert len(stt.calls) == 1 and _latest(db).status == "ready"
    import shutil
    shutil.rmtree(intake_lib.work_dir(settings, it))  # 작업 폴더가 지워진 뒤에도
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    it = _latest(db)
    assert len(stt.calls) == 1 and it.status == "ready"
    assert (intake_lib.work_dir(settings, it) / "1-회의.m4a.md").is_file()
    (f,) = _files(db, it)
    assert f.read_status == "ok"


def test_cache_invalidated_when_model_changes(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    stt.model = "medium"
    stt.text = "다른 모델이 받아쓴 글"
    import shutil
    shutil.rmtree(intake_lib.work_dir(settings, it))
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    it = _latest(db)
    assert len(stt.calls) == 2
    (f,) = _files(db, it)
    assert "다른 모델이 받아쓴 글" in _cache(settings, f).read_text(encoding="utf-8")
    assert _cache(settings, f).read_text(encoding="utf-8").startswith("#model: medium")


def test_failed_audio_retried_on_reanalyze(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    stt.error = transcribe.TranscribeError("일시 오류")
    _upload(user_client, [AUDIO, DOC])
    _run(app, settings)
    it = _latest(db)
    assert _files(db, it)[0].read_status == "failed"
    stt.error = None
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    audio = _files(db, _latest(db))[0]
    assert audio.read_status == "ok" and audio.read_error is None and len(stt.calls) == 2


def test_progress_column_updates_throttled_and_independent(app, user_client, db, settings, stt, fake_claude, monkeypatch):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    seen = []
    other = app.state.session_factory()

    def peek(name, step):
        other.expire_all()
        f = other.query(IntakeFile).one()
        seen.append((step, f.progress))

    stt.on_step = peek
    _run(app, settings)
    # 알림 직전에 본 값: 처음엔 0, 그다음은 앞 알림의 값 (20, 60)
    assert seen == [(0, 0), (1, 20), (2, 60)]
    other.close()
    # 2초 간격 제한: 간격이 길면 첫 알림만 기록한다
    monkeypatch.setattr(intake_tasks, "PROGRESS_INTERVAL_S", 1000.0)
    seen.clear()
    _upload(user_client, [AUDIO2])
    other = app.state.session_factory()
    stt.on_step = lambda name, step: (other.expire_all(), seen.append((step, other.query(IntakeFile).filter_by(original_name="통화.mp3").one().progress)))
    _run(app, settings)
    other.close()
    assert seen == [(0, 0), (1, 20), (2, 20)]


def test_progress_write_does_not_block_other_writers(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    other = app.state.session_factory()

    def write_elsewhere(name, step):  # 받아쓰는 동안 다른 쓰기가 막히지 않는다
        other.execute(__import__("sqlalchemy").update(Intake).values(note="그 사이 고친 메모"))
        other.commit()

    stt.on_step = write_elsewhere
    _run(app, settings)
    other.close()
    assert _latest(db).note == "그 사이 고친 메모"


def test_status_fragment_shows_progress_and_hint(app, user_client, db, settings, stt):
    _upload(user_client, [AUDIO, DOC])
    it = _latest(db)
    audio = _files(db, it)[0]
    audio.progress = 37
    job = db.get(AIJob, it.job_id)
    job.status, job.started_at = "running", __import__("datetime").datetime.now()
    db.commit()
    text = user_client.get(f"/intake/{it.id}/status").text
    assert "음성 받아쓰는 중 · 회의.m4a · 37%" in text
    assert "보통 1분 안에 끝납니다" not in text
    assert "녹음 길이에 따라 오래 걸릴 수 있어요. 이 화면을 떠나도 됩니다." in text
    assert 'hx-trigger="every 2s"' in text
    page = user_client.get(f"/intake/{it.id}").text
    assert "받아쓰는 중 37%" in page
    # 받아쓰기가 끝나면 AI 정리 문구
    audio = _files(db, it)[0]
    audio.read_status, audio.progress = "ok", 100
    db.commit()
    text = user_client.get(f"/intake/{it.id}/status").text
    assert "AI가 파일을 정리하고 있어요" in text and "음성 받아쓰는 중" not in text


def test_status_fragment_unchanged_for_non_audio(app, user_client, db, settings):
    _upload(user_client, [DOC])
    it = _latest(db)
    job = db.get(AIJob, it.job_id)
    job.status, job.started_at = "running", __import__("datetime").datetime.now()
    db.commit()
    text = user_client.get(f"/intake/{it.id}/status").text
    assert "정리 중…" in text and "보통 1분 안에 끝납니다" in text


def test_pending_audio_waiting_shows_pending_not_percent(app, user_client, db, settings, stt):
    _upload(user_client, [AUDIO])
    it = _latest(db)
    page = user_client.get(f"/intake/{it.id}").text
    assert "받아쓰기 대기" in page and "받아쓰는 중" not in page
    text = user_client.get(f"/intake/{it.id}/status").text  # 아직 대기열
    assert "AI 대기" in text and "음성 받아쓰는 중" not in text


def test_discard_during_transcription_stops_and_cleans_up(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    it = _latest(db)
    other = app.state.session_factory()

    def discard(name, step):
        if step == 1:
            intake_lib.discard(other, settings, other.get(Intake, it.id))

    stt.on_step = discard
    _run(app, settings)
    other.close()
    it = _latest(db)
    job = db.get(AIJob, it.job_id)
    db.refresh(job)
    assert it.status == "discarded" and job.result == "superseded"
    assert fake_claude.calls() == []
    assert not intake_lib.work_dir(settings, it).exists()
    (f,) = _files(db, it)
    assert f.read_status == "pending_transcript" and not _cache(settings, f).exists()


def test_discard_between_files_skips_the_rest(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO, AUDIO2])
    it = _latest(db)
    other = app.state.session_factory()
    stt.on_done = lambda name: intake_lib.discard(other, settings, other.get(Intake, it.id))
    _run(app, settings)
    other.close()
    assert len(stt.calls) == 1 and _latest(db).status == "discarded"
    assert not intake_lib.work_dir(settings, it).exists() and fake_claude.calls() == []


def test_restart_mid_transcription_then_resume_uses_cache(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO, AUDIO2])
    it = _latest(db)
    f1, f2 = _files(db, it)
    stt.errors[f2.stored_name] = Crash()
    with pytest.raises(Crash):
        _run(app, settings)
    db.expire_all()
    assert db.get(AIJob, it.job_id).status == "running"
    assert _cache(settings, f1).is_file() and not _cache(settings, f2).exists()
    assert fail_stale_running(db) == 1 and intake_lib.recover(db) == 1
    it = _latest(db)
    assert it.status == "failed"
    stt.errors.clear()
    stt.calls.clear()
    r = user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert _run(app, settings) is True
    it = _latest(db)
    assert it.status == "ready"
    assert [c[0] for c in stt.calls] == [f2.stored_name]  # 이미 끝난 파일은 다시 받아쓰지 않는다
    assert [f.read_status for f in _files(db, it)] == ["ok", "ok"]


def test_restart_recovery_via_app_start(app, user_client, db, settings, stt):
    _upload(user_client, [AUDIO])
    it = _latest(db)
    db.get(AIJob, it.job_id).status = "running"
    db.commit()
    with TestClient(app):
        pass
    assert _latest(db).status == "failed"


def test_only_one_transcription_at_a_time(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    _upload(user_client, [AUDIO2])
    stt.pause_s = 0.3
    results = []
    barrier = threading.Barrier(2)

    def work():
        barrier.wait()
        results.append(_run(app, settings))

    threads = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert results == [True, True] and len(stt.calls) == 2
    assert stt.max_active == 1


def test_claim_skips_analysis_that_needs_busy_engine(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])  # 받아쓰기가 필요한 정리 작업
    audio_job = _latest(db).job_id
    other = AIJob(kind="activity_structure", input={"raw_text": "x"}, priority=0, status="pending")
    db.add(other)
    db.commit()
    with transcribe._RUN_LOCK:  # 다른 워커가 받아쓰는 중
        claimed = queue._claim_next(app.state.session_factory)
        assert claimed is not None and claimed[0] == other.id
        assert queue._claim_next(app.state.session_factory) is None  # 받아쓰기 정리는 남겨 둔다
    claimed = queue._claim_next(app.state.session_factory)
    assert claimed is not None and claimed[0] == audio_job  # 풀리면 집는다


def test_claim_does_not_skip_analysis_without_audio(app, user_client, db, settings, stt):
    _upload(user_client, [DOC])
    job = _latest(db).job_id
    with transcribe._RUN_LOCK:
        claimed = queue._claim_next(app.state.session_factory)
    assert claimed is not None and claimed[0] == job


def test_run_lock_serialises_plain_callers(stt, tmp_path):
    seen = []

    def go():
        transcribe.run(stt, tmp_path / "a.m4a")
        seen.append(stt.max_active)

    stt.pause_s = 0.1
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert stt.max_active == 1


def test_run_supports_old_style_fake_without_keywords(tmp_path):
    class Old(transcribe.Transcriber):
        def available(self):
            return True

        def transcribe(self, path):
            return "옛 방식"

    assert transcribe.run(Old(), tmp_path / "a.m4a", hints="h", on_progress=lambda p: None) == "옛 방식"


# ---- 프롬프트·DB·점검·명령줄 ----

def test_prompt_rule_text():
    p = (Path(__file__).resolve().parent.parent / "app" / "ai" / "prompts" / "intake_analyze.md").read_text(encoding="utf-8")
    assert "음성(받아쓰기)" in p and "잘못 들은" in p and "고유명사" in p
    assert "등록된 이름" in p and "`reason`" in p and "(받아쓰기 확인 필요)" in p
    assert "숫자" in p and "날짜" in p and "조용히" in p
    assert "받아쓴 글 그대로" in p


def test_migrate_adds_read_error_and_progress(tmp_path):
    db_path = tmp_path / "app.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE intakes (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE intake_files (id INTEGER PRIMARY KEY, intake_id INTEGER, original_name VARCHAR, "
                "stored_name VARCHAR, kind VARCHAR, read_status VARCHAR, size INTEGER, target_type VARCHAR, "
                "target_id INTEGER, created_at DATETIME)")
    con.execute("INSERT INTO intake_files (id, intake_id, original_name, read_status) VALUES (1, 1, '옛.m4a', 'needs_transcript')")
    con.commit()
    con.close()
    engine = make_engine(db_path)
    added = add_missing_columns(engine, db_path)
    engine.dispose()
    assert "intake_files.read_error" in added and "intake_files.progress" in added
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT original_name, read_status, read_error, progress FROM intake_files").fetchone()
    con.close()
    assert row == ("옛.m4a", "needs_transcript", None, None)


def test_check_line_variants(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path, 'transcriber = "faster-whisper"\n')))
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)

    def line():
        try:
            cli.main(["check"])
        except SystemExit:
            pass
        return next(x for x in capsys.readouterr().out.splitlines() if "음성 받아쓰기" in x)

    out = line()
    assert "문제" in out and "faster-whisper 미설치" in out
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    monkeypatch.setattr(transcribe, "_cached_model_dir", lambda name, root: root / "m")
    assert line() == "음성 받아쓰기: 정상 (faster-whisper, small, CPU)"
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path)))
    assert "사용 안 함" in line()


def test_system_page_shows_problem_label(admin_client, settings, monkeypatch):
    settings.transcriber = "faster-whisper"
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)
    assert "faster-whisper 미설치" in admin_client.get("/admin/system").text


def test_dropzone_note_depends_on_engine(user_client, settings, stt):
    for url in ("/intake/new", "/"):
        page = user_client.get(url).text
        assert "음성 파일은 받아써서 정리합니다 (길면 오래 걸려요)" in page and "보관만 됩니다" not in page
    settings.transcriber = "none"
    for url in ("/intake/new", "/"):
        assert "음성 파일은 보관만 됩니다" in user_client.get(url).text


def test_failed_audio_offers_reanalyze(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=json.dumps({"activities": []}))
    stt.error = transcribe.TranscribeError("일시 오류")
    _upload(user_client, [AUDIO, DOC])
    _run(app, settings)
    it = _latest(db)
    page = user_client.get(f"/intake/{it.id}").text
    assert "일시 오류" in page and "다시 정리를 누르면 다시 받아씁니다" in page and ">다시 정리<" in page


# ---- 명령줄 ----

def _cli_env(tmp_path, monkeypatch, extra=""):
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path, extra)))


def test_stt_test_guards(tmp_path, monkeypatch, capsys):
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-test", str(tmp_path / "없음.m4a")])
    assert "파일을 찾을 수 없습니다" in str(e.value)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-test", str(audio)])
    assert "faster-whisper 미설치" in str(e.value)
    with pytest.raises(SystemExit):
        cli.main(["stt-test"])  # 인자 없음


def test_stt_test_prints_text_time_and_speed(tmp_path, monkeypatch, capsys, stt):
    _cli_env(tmp_path, monkeypatch)
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"x")

    def fake_transcribe(self, path, *, hints="", on_progress=None):
        self.last_audio_seconds = 120.0
        if on_progress:
            on_progress(0.5)
            on_progress(1.0)
        return "받아쓴 글"

    monkeypatch.setattr(FakeEngine, "transcribe", fake_transcribe)
    clock = iter([100.0, 160.0, 160.0, 160.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 160.0))
    cli.main(["stt-test", str(audio)])
    out = capsys.readouterr().out
    assert "받아쓴 글" in out and "걸린 시간" in out and "1분" in out
    assert "2.0배" in out  # 음성 2분을 실제 1분에


def test_stt_setup_guards_and_success(tmp_path, monkeypatch, capsys, stt):
    _cli_env(tmp_path, monkeypatch, 'transcriber = "faster-whisper"\n')
    monkeypatch.setattr(FakeEngine, "prepare_model", lambda self, say=print: say("모델 준비 완료"), raising=False)
    monkeypatch.setattr(FakeEngine, "self_test", lambda self: "자체 점검 통과", raising=False)
    cli.main(["stt-setup"])
    out = capsys.readouterr().out
    assert "모델 준비 완료" in out and "자체 점검 통과" in out and "stt-test" in out
    # 엔진을 쓰도록 설정하지 않았으면 안내한다
    _cli_env(tmp_path, monkeypatch)
    cli.main(["stt-setup"])
    assert 'transcriber = "faster-whisper"' in capsys.readouterr().out


def test_stt_setup_reports_missing_package(tmp_path, monkeypatch):
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-setup"])
    assert 'pip install -e ".[stt]"' in str(e.value)


def test_stt_setup_reports_download_failure(tmp_path, monkeypatch):
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)

    def boom(self, say=print):
        raise transcribe.TranscribeError("모델을 내려받지 못했습니다. 인터넷 연결을 확인하세요.")

    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "prepare_model", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-setup"])
    assert "인터넷" in str(e.value)


def test_models_folder_not_in_backup(app, settings):
    from datetime import datetime
    from app.backup import run_backup
    (settings.models_dir / "models--x").mkdir(parents=True)
    (settings.models_dir / "models--x" / "model.bin").write_bytes(b"x")
    path = run_backup(settings, now=datetime(2026, 11, 20, 3, 0))
    assert not any("model.bin" in str(p) for p in Path(path).rglob("*"))
