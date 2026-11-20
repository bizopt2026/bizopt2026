import dataclasses
from pathlib import Path

import pytest

from app import cli, transcribe
from app.config import load_settings
from app.intake import prepare_workdir, work_dir
from app.models import Intake, IntakeFile


class FakeTranscriber(transcribe.Transcriber):
    def __init__(self, text="안녕하세요 통화 내용입니다", error=None):
        self.text, self.error, self.paths = text, error, []

    def available(self) -> bool:
        return True

    def transcribe(self, path: Path) -> str:
        self.paths.append(path)
        if self.error:
            raise self.error
        return self.text


def _config(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "data"\nsecret_key = "k"\n{extra}', encoding="utf-8")
    return cfg


def test_transcriber_setting_defaults_to_none(tmp_path):
    assert load_settings(_config(tmp_path)).transcriber == "none"
    assert load_settings(_config(tmp_path, 'transcriber = "none"\n')).transcriber == "none"


def test_unknown_transcriber_exits_with_korean_hint(tmp_path):
    with pytest.raises(SystemExit) as e:
        load_settings(_config(tmp_path, 'transcriber = "whisper"\n'))
    assert "transcriber" in str(e.value) and "none" in str(e.value) and "whisper" in str(e.value)


def test_none_transcriber_is_unavailable(settings):
    t = transcribe.get(settings)
    assert isinstance(t, transcribe.NoneTranscriber) and t.available() is False
    with pytest.raises(transcribe.TranscribeError):
        t.transcribe(Path("x.m4a"))


def _bundle_with_audio(db, settings):
    it = Intake(note="", status="uploaded")
    db.add(it)
    db.flush()
    f = IntakeFile(intake_id=it.id, original_name="회의.m4a", stored_name="1.m4a", kind="audio", size=4, read_status="needs_transcript")
    db.add(f)
    db.commit()
    from app.intake import stored_path
    stored_path(settings, f).parent.mkdir(parents=True, exist_ok=True)
    stored_path(settings, f).write_bytes(b"\x00\x01fake")
    return it, f


def test_audio_without_transcriber_needs_transcript(db, settings):
    it, f = _bundle_with_audio(db, settings)
    prepare_workdir(settings, it)
    assert f.read_status == "needs_transcript"
    assert [p.name for p in work_dir(settings, it).glob("*.md")] == ["파일목록.md"]


def test_audio_with_transcriber_is_pending_not_transcribed_in_request(db, settings, monkeypatch):
    it, f = _bundle_with_audio(db, settings)
    fake = FakeTranscriber()
    monkeypatch.setattr(transcribe, "get", lambda s: fake)
    prepare_workdir(settings, it)
    assert f.read_status == "pending_transcript" and fake.paths == []  # 받아쓰기는 정리 작업 안에서 한다 (tests/test_stt.py)
    assert [p.name for p in work_dir(settings, it).glob("*.md")] == ["파일목록.md"]
    assert "받아쓰기 대기" in (work_dir(settings, it) / "파일목록.md").read_text(encoding="utf-8")


def test_system_page_and_check_show_transcriber(admin_client, tmp_path, monkeypatch, capsys):
    assert "음성 받아쓰기" in admin_client.get("/admin/system").text
    assert "사용 안 함" in admin_client.get("/admin/system").text
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path)))
    try:
        cli.main(["check"])
    except SystemExit:  # 이 PC 의 claude·포트 상태에 따라 다른 줄이 문제일 수 있다
        pass
    line = next(x for x in capsys.readouterr().out.splitlines() if "음성 받아쓰기" in x)
    assert "사용 안 함" in line and "문제" not in line
