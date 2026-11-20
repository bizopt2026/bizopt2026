"""음성 받아쓰기 수정 묶음: 오류 분류, 길이 확인·조각 나누기, 힌트, 모델 준비 확인, 설정 검사, 올리기 흐름.

실제 `claude` 도, 실제 음성 모델·faster-whisper 도 쓰지 않는다 (엔진 안쪽은 가짜 모델·가짜 음성 원본으로 시험한다)."""
import io
import logging
import os
import sqlite3
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from app import cli, intake as intake_lib, labels, transcribe
from app.ai import intake_tasks
from app.config import Settings, load_settings
from app.models import AIJob, Intake, IntakeFile
from tests.test_stt import (AUDIO, DOC, PLAN, FakeEngine, _cache, _config, _files, _fw_settings, _latest, _run,
                            _upload, stt)  # noqa: F401 - stt 는 고정 장치

SR = transcribe.SAMPLE_RATE


# ---- 가짜 음성 원본·가짜 모델 ----

class FakeAudio:
    """`_open_audio` 가 돌려주는 것의 모양: 컨텍스트, `.duration`, `.pieces()`(16 kHz 모노 int16 조각)."""

    def __init__(self, samples, duration="exact", piece=3000):
        self.samples = samples
        self.duration = len(samples) / SR if duration == "exact" else duration
        self.piece = piece
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def pieces(self):
        for i in range(0, len(self.samples), self.piece):
            yield self.samples[i:i + self.piece]


class ArrayModel:
    """`WhisperModel.transcribe` 의 모양. 조각(배열)을 받고 조각 안 시각의 조각글을 돌려준다."""

    def __init__(self, script=None, language="ko", fail=None):
        self.calls, self.script, self.language, self.fail = [], script, language, fail

    def transcribe(self, audio, **kwargs):
        i = len(self.calls)
        self.calls.append({"n": len(audio), "audio": audio, **kwargs})
        if self.fail:
            raise self.fail
        segs = self.script(i, audio) if self.script else [(0.0, 1.0, f"조각{i}")]
        gen = (SimpleNamespace(start=s, end=e, text=t) for s, e, t in segs)
        return gen, SimpleNamespace(duration=len(audio) / SR, language=self.language)


def _engine(tmp_path, monkeypatch, model, audio=None, **kw):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe, "_cached_model_dir", lambda name, root: root / "m")
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    t = transcribe.get(_fw_settings(tmp_path, **kw))
    monkeypatch.setattr(t, "_model", lambda: model)
    holder = {}
    if audio is not None:
        def opener(path):
            holder["source"] = audio
            return audio
        monkeypatch.setattr(transcribe, "_open_audio", opener)
    return t


def _wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    return p


def _speech(seconds, gaps=()):
    """시끄러운 소리 `seconds` 초. `gaps`(시작, 끝 초)는 무음."""
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(int(seconds * SR)) * 4000).astype(np.int16)
    for a, b in gaps:
        x[int(a * SR):int(b * SR)] = 0
    return x


# ---- C1 의존성·오류 분류 ----

def test_stt_extra_pins_pyav_below_19():
    meta = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["optional-dependencies"]["stt"] == ["faster-whisper>=1.1", "av<19"]
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "PyAV 19" in text  # 핀을 풀 조건을 주석으로 남긴다


def _ffmpeg_error(name="InvalidDataError", *bases):
    base = type("FFmpegError", (Exception,), {"__module__": "av.error"})
    return type(name, (base, *bases), {"__module__": "av.error"})


@pytest.mark.parametrize("exc,name", [
    (TypeError("av.open() got an unexpected keyword argument 'metadata_errors'"), "TypeError"),
    (ImportError("dll"), "ImportError"),
    (AttributeError("x"), "AttributeError"),
    (MemoryError(), "MemoryError"),
    (OSError("disk"), "OSError"),
    (RuntimeError("ctranslate2"), "RuntimeError"),
])
def test_library_errors_are_program_errors_not_damaged_file(tmp_path, monkeypatch, caplog, exc, name):
    model = ArrayModel(fail=exc)
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(2)))
    with caplog.at_level(logging.ERROR), pytest.raises(transcribe.TranscribeError) as e:
        t.transcribe(_wav(tmp_path))
    assert str(e.value) == f"받아쓰기 프로그램 오류입니다. 관리자에게 알려 주세요 ({name})"
    assert "손상" not in str(e.value)
    assert "Traceback" in caplog.text  # 자세한 내용은 기록에


def test_program_error_during_decode_is_not_damaged_file(tmp_path, monkeypatch):
    class Broken(FakeAudio):
        def pieces(self):
            raise TypeError("av.open() got an unexpected keyword argument")
            yield  # pragma: no cover

    t = _engine(tmp_path, monkeypatch, ArrayModel(), Broken(_speech(2)))
    with pytest.raises(transcribe.TranscribeError, match=r"받아쓰기 프로그램 오류입니다.*\(TypeError\)"):
        t.transcribe(_wav(tmp_path))


def test_real_decode_errors_keep_damaged_file_message(tmp_path, monkeypatch):
    class Bad(FakeAudio):
        def pieces(self):
            raise _ffmpeg_error("InvalidDataError", ValueError)("Invalid data found when processing input")
            yield  # pragma: no cover

    t = _engine(tmp_path, monkeypatch, ArrayModel(), Bad(_speech(2)))
    with pytest.raises(transcribe.TranscribeError, match="손상되었거나 지원하지 않는 형식") as e:
        t.transcribe(_wav(tmp_path))
    assert e.value.cause.startswith("InvalidDataError")


def test_av_os_and_memory_errors_are_program_errors(tmp_path, monkeypatch):
    for exc in (_ffmpeg_error("OSError", OSError)("io"), _ffmpeg_error("MemoryError", MemoryError)()):
        class Bad(FakeAudio):
            def pieces(self, exc=exc):
                raise exc
                yield  # pragma: no cover

        t = _engine(tmp_path, monkeypatch, ArrayModel(), Bad(_speech(2)))
        with pytest.raises(transcribe.TranscribeError, match="받아쓰기 프로그램 오류입니다"):
            t.transcribe(_wav(tmp_path))


def test_open_error_is_classified_too(tmp_path, monkeypatch):
    t = _engine(tmp_path, monkeypatch, ArrayModel())

    def opener(path):
        raise _ffmpeg_error("InvalidDataError", ValueError)("bad")

    monkeypatch.setattr(transcribe, "_open_audio", opener)
    with pytest.raises(transcribe.TranscribeError, match="손상되었거나"):
        t.transcribe(_wav(tmp_path))


def test_self_test_surfaces_real_cause(tmp_path, monkeypatch):
    t = _engine(tmp_path, monkeypatch, ArrayModel(fail=TypeError("metadata_errors")))
    monkeypatch.setattr(transcribe, "_open_audio", lambda p: FakeAudio(_speech(1)))
    with pytest.raises(transcribe.TranscribeError) as e:
        t.self_test()
    assert "TypeError" in str(e.value) and "metadata_errors" in str(e.value)


def test_self_test_disables_vad_so_the_model_really_runs(tmp_path, monkeypatch):
    model = ArrayModel(script=lambda i, a: [])
    t = _engine(tmp_path, monkeypatch, model)
    monkeypatch.setattr(transcribe, "_open_audio", lambda p: FakeAudio(np.zeros(SR, np.int16)))
    assert "통과" in t.self_test()
    assert model.calls and model.calls[0]["vad_filter"] is False


def test_cli_prints_cause_for_setup_and_test(tmp_path, monkeypatch, capsys, stt):
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path, 'transcriber = "faster-whisper"\n')))
    err = transcribe.TranscribeError("받아쓰기 프로그램 오류입니다. 관리자에게 알려 주세요 (TypeError)", cause="TypeError: 원인글")
    monkeypatch.setattr(FakeEngine, "prepare_model", lambda self, say=print: None, raising=False)
    monkeypatch.setattr(FakeEngine, "self_test", lambda self: (_ for _ in ()).throw(err), raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-setup"])
    assert "TypeError: 원인글" in str(e.value) and "(TypeError)" in str(e.value)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    stt.error = err
    with pytest.raises(SystemExit) as e:
        cli.main(["stt-test", str(audio)])
    assert "TypeError: 원인글" in str(e.value)


# ---- I1 길이 확인(모델 부르기 전)·조각 나누기 ----

def test_too_long_is_rejected_before_the_model_is_called(tmp_path, monkeypatch):
    model = ArrayModel()
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(3), duration=121 * 60.0), transcribe_max_minutes=120)
    with pytest.raises(transcribe.TranscribeError, match="최대 120분"):
        t.transcribe(_wav(tmp_path))
    assert model.calls == []
    assert t.last_audio_seconds == 121 * 60.0


def test_unknown_duration_is_limited_while_decoding(tmp_path, monkeypatch):
    model = ArrayModel()
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(130), duration=None),
                transcribe_max_minutes=1, transcribe_chunk_minutes=0.5)
    with pytest.raises(transcribe.TranscribeError, match="너무 깁니다"):
        t.transcribe(_wav(tmp_path))


def test_limit_message_uses_minutes(tmp_path, monkeypatch):
    t = _engine(tmp_path, monkeypatch, ArrayModel(), FakeAudio(_speech(3), duration=61 * 60.0), transcribe_max_minutes=60)
    with pytest.raises(transcribe.TranscribeError, match=r"최대 60분, 이 파일은 약 61분"):
        t.transcribe(_wav(tmp_path))


def test_quietest_cut_picks_the_gap():
    x = _speech(10, gaps=[(6.0, 6.5)])
    cut = transcribe.quietest_cut(x)
    assert 6.0 * SR <= cut <= 6.5 * SR
    assert transcribe.quietest_cut(x[:100]) == 100  # 한 칸보다 짧으면 끝


def test_split_chunks_cuts_at_quiet_points_and_keeps_every_sample():
    x = _speech(100, gaps=[(27.0, 28.0), (54.0, 55.0), (80.0, 81.0)])
    src = FakeAudio(x)
    chunks = list(transcribe.split_chunks(src.pieces(), 30 * SR, 10 * SR))
    assert len(chunks) == 4
    assert np.array_equal(np.concatenate(chunks), x)
    ends = np.cumsum([len(c) for c in chunks]) / SR
    assert 27.0 <= ends[0] <= 28.0 and 54.0 <= ends[1] <= 55.0 and 80.0 <= ends[2] <= 81.0
    assert all(len(c) <= 30 * SR for c in chunks)


def test_split_chunks_short_input_is_one_chunk_and_empty_is_none():
    x = _speech(5)
    assert [len(c) for c in transcribe.split_chunks(FakeAudio(x).pieces(), 30 * SR, 10 * SR)] == [len(x)]
    assert list(transcribe.split_chunks(iter([]), 30 * SR, 10 * SR)) == []


def test_chunked_transcription_offsets_markers_progress_and_hints(tmp_path, monkeypatch):
    x = _speech(100, gaps=[(27.0, 28.0), (54.0, 55.0), (80.0, 81.0)])
    model = ArrayModel(script=lambda i, a: [(0.0, 5.0, f"앞{i}"), (10.0, 12.0, f"뒤{i}")])
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(x), transcribe_chunk_minutes=0.5,
                transcribe_language="ko", transcribe_max_minutes=120)
    seen = []
    text = t.transcribe(_wav(tmp_path), hints="한빛병원, 루미텍", on_progress=seen.append)
    assert len(model.calls) == 4
    for c in model.calls:  # 힌트·언어·옵션은 조각마다
        assert c["hotwords"] == "한빛병원, 루미텍" and c["language"] == "ko"
        assert c["vad_filter"] is True and c["condition_on_previous_text"] is False
        assert c.get("initial_prompt") is None
        assert c["audio"].dtype == np.float32 and c["audio"].ndim == 1
        assert float(np.abs(c["audio"]).max()) <= 1.0
    markers = [ln for ln in text.splitlines() if ln.startswith("[")]
    clocks = [int(m[1:3]) * 60 + int(m[4:6]) for m in markers]
    assert clocks == sorted(clocks) and clocks[0] == 0 and clocks[-1] >= 54
    assert "앞3" in text and "뒤3" in text
    assert seen == sorted(seen) and seen[-1] >= 0.97 and max(seen) <= 1.0  # 조각이 끝날 때마다 진행을 알린다
    assert t.last_audio_seconds == pytest.approx(100.0)


def test_chunk_starts_are_offsets_of_the_real_cut_points(tmp_path, monkeypatch):
    x = _speech(100, gaps=[(27.0, 28.0), (54.0, 55.0), (80.0, 81.0)])
    model = ArrayModel(script=lambda i, a: [(1.0, 2.0, f"c{i}")])
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(x), transcribe_chunk_minutes=0.5)
    text = t.transcribe(_wav(tmp_path))
    lines = text.splitlines()
    starts, acc = [], 0
    for c in model.calls:
        starts.append(acc)
        acc += c["n"]
    assert [round(s / SR + 1) for s in starts] == [1, round(starts[1] / SR + 1), round(starts[2] / SR + 1), round(starts[3] / SR + 1)]
    assert lines[0].startswith("[00:01]") and "c3" in lines[-1]


def test_silent_chunks_still_report_progress_and_allow_cancel(tmp_path, monkeypatch):
    model = ArrayModel(script=lambda i, a: [])
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(100, gaps=[(27, 28), (54, 55), (80, 81)])),
                transcribe_chunk_minutes=0.5)
    seen = []
    t.transcribe(_wav(tmp_path), on_progress=seen.append)
    assert len(seen) == 4 and seen[-1] >= 0.99

    stop_after = []

    def stop(p):
        stop_after.append(p)
        raise transcribe.TranscribeCancelled()

    model2 = ArrayModel(script=lambda i, a: [])
    t2 = _engine(tmp_path, monkeypatch, model2, FakeAudio(_speech(100)), transcribe_chunk_minutes=0.5)
    with pytest.raises(transcribe.TranscribeCancelled):
        t2.transcribe(_wav(tmp_path), on_progress=stop)
    assert len(model2.calls) == 1  # 다음 조각을 받아쓰지 않는다


def test_source_is_closed_even_when_cancelled(tmp_path, monkeypatch):
    src = FakeAudio(_speech(40))
    t = _engine(tmp_path, monkeypatch, ArrayModel(), src, transcribe_chunk_minutes=0.5)

    def stop(p):
        raise transcribe.TranscribeCancelled()

    with pytest.raises(transcribe.TranscribeCancelled):
        t.transcribe(_wav(tmp_path), on_progress=stop)
    assert src.closed


def test_auto_language_is_detected_once_then_reused(tmp_path, monkeypatch):
    model = ArrayModel(language="en")
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(100)), transcribe_language="auto", transcribe_chunk_minutes=0.5)
    t.transcribe(_wav(tmp_path))
    assert model.calls[0]["language"] is None
    assert all(c["language"] == "en" for c in model.calls[1:])


def test_auto_language_is_not_locked_by_a_silent_first_chunk(tmp_path, monkeypatch):
    # 첫 조각에 말이 없으면(무음·음악) 감지된 언어가 제멋대로이므로 고정하지 않고, 말이 나온 조각에서 정한다.
    model = ArrayModel(script=lambda i, a: [] if i == 0 else [(0.0, 1.0, f"c{i}")], language="en")
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(100)), transcribe_language="auto", transcribe_chunk_minutes=0.5)
    t.transcribe(_wav(tmp_path))
    assert model.calls[0]["language"] is None and model.calls[1]["language"] is None
    assert all(c["language"] == "en" for c in model.calls[2:]) and len(model.calls) >= 3


def test_unknown_duration_still_calls_progress_so_cancel_works(tmp_path, monkeypatch):
    # 길이를 알 수 없는 파일(브라우저 녹음 webm 등)도 진행 콜백을 불러야 버림·다시 정리 확인이 된다.
    seen = []
    model = ArrayModel()
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(100), duration=None), transcribe_chunk_minutes=0.5)
    t.transcribe(_wav(tmp_path), on_progress=seen.append)
    assert seen and set(seen) == {0.0}

    def stop(p):
        raise transcribe.TranscribeCancelled()

    model2 = ArrayModel()
    t2 = _engine(tmp_path, monkeypatch, model2, FakeAudio(_speech(100), duration=None), transcribe_chunk_minutes=0.5)
    with pytest.raises(transcribe.TranscribeCancelled):
        t2.transcribe(_wav(tmp_path), on_progress=stop)
    assert len(model2.calls) == 1


def test_empty_audio_is_a_damaged_file(tmp_path, monkeypatch):
    t = _engine(tmp_path, monkeypatch, ArrayModel(), FakeAudio(np.zeros(0, np.int16)))
    with pytest.raises(transcribe.TranscribeError, match="손상되었거나"):
        t.transcribe(_wav(tmp_path))


def test_defaults_and_validation_of_new_settings(tmp_path):
    s = load_settings(_config(tmp_path))
    assert s.transcribe_max_minutes == 120 and s.transcribe_chunk_minutes == 10
    assert load_settings(_config(tmp_path, "transcribe_chunk_minutes = 5\n")).transcribe_chunk_minutes == 5
    for bad in ("0", "1.5", '"10"', "true", "-3"):
        with pytest.raises(SystemExit) as e:
            load_settings(_config(tmp_path, f"transcribe_chunk_minutes = {bad}\n"))
        assert "transcribe_chunk_minutes" in str(e.value)


def test_example_config_documents_chunk_minutes_and_new_default():
    root = Path(__file__).resolve().parent.parent
    example = (root / "config.example.toml").read_text(encoding="utf-8")
    assert "transcribe_chunk_minutes" in example and "transcribe_max_minutes = 120" in example


# ---- I2 힌트는 모든 창에 (hotwords) ----

def test_hints_go_to_hotwords_not_initial_prompt(tmp_path, monkeypatch):
    model = ArrayModel()
    t = _engine(tmp_path, monkeypatch, model, FakeAudio(_speech(2)))
    t.transcribe(_wav(tmp_path), hints="루미텍, 메모")
    assert model.calls[0]["hotwords"] == "루미텍, 메모" and model.calls[0].get("initial_prompt") is None
    model2 = ArrayModel()
    t2 = _engine(tmp_path, monkeypatch, model2, FakeAudio(_speech(2)))
    t2.transcribe(_wav(tmp_path))
    assert model2.calls[0]["hotwords"] is None


def test_hotwords_is_a_real_parameter_of_the_installed_signature():
    """설치돼 있으면 실제 시그니처에서 확인한다 (없으면 건너뜀)."""
    fw = pytest.importorskip("faster_whisper")
    import inspect
    assert "hotwords" in inspect.signature(fw.WhisperModel.transcribe).parameters


# ---- 모델 준비 확인 (부분 내려받기·ImportError) ----

def _model_dir(path: Path, files=("model.bin", "tokenizer.json", "vocabulary.json", "config.json")):
    path.mkdir(parents=True, exist_ok=True)
    for f in files:
        (path / f).write_bytes(b"x")
    return path


def test_local_model_needs_the_files_the_loader_needs(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    m = tmp_path / "mm"
    t = transcribe.get(_fw_settings(tmp_path, transcribe_model=str(m)))
    _model_dir(m, files=("model.bin",))
    assert not t.available() and "stt-setup" in t.label()  # model.bin 만 있다 = 부분 내려받기
    _model_dir(m, files=("model.bin", "tokenizer.json"))
    assert not t.available()  # 어휘 파일이 없다
    (m / "vocabulary.txt").write_bytes(b"x")  # vocabulary.* 아무거나
    assert t.available()
    (m / "model.bin").write_bytes(b"")
    assert not t.available()  # 빈 model.bin


def test_ready_result_is_rechecked_and_never_cached_when_false(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    transcribe._READY.clear()
    m = _model_dir(tmp_path / "mm", files=("model.bin",))
    t = transcribe.get(_fw_settings(tmp_path, transcribe_model=str(m)))
    assert not t.available() and not transcribe._READY
    _model_dir(m)
    assert t.available() and transcribe._READY
    (m / "model.bin").unlink()  # 나중에 지워지면 알아챈다
    assert not t.available()


def test_named_model_snapshot_without_weights_is_not_ready(tmp_path, monkeypatch):
    """`download_model(local_files_only=True)` 는 내려받다 만 폴더도 돌려준다 → 파일을 확인한다."""
    snap = tmp_path / "data" / "models" / "snap"
    snap.mkdir(parents=True)
    (snap / "config.json").write_bytes(b"{}")
    fake_utils = SimpleNamespace(download_model=lambda name, **kw: str(snap))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(utils=fake_utils))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", fake_utils)
    assert transcribe._cached_model_dir("small", tmp_path / "data" / "models") is None
    _model_dir(snap)
    assert transcribe._cached_model_dir("small", tmp_path / "data" / "models") == snap


def test_import_error_in_readiness_has_its_own_message(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)

    def boom(self):
        raise ImportError("DLL load failed while importing ctranslate2")

    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", boom)
    t = transcribe.get(_fw_settings(tmp_path))
    msg = t.problem()
    assert "불러오지 못했습니다" in msg and "ImportError" in msg and "아직 없습니다" not in msg
    assert 'pip install -e ".[stt]"' in msg and "Visual C++" in msg
    assert not t.available()


def test_import_error_inside_cached_model_dir_is_not_swallowed(tmp_path, monkeypatch):
    class Broken:
        def __getattr__(self, name):
            raise ImportError("broken wheel")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(utils=Broken()))
    monkeypatch.delitem(sys.modules, "faster_whisper.utils", raising=False)
    with pytest.raises(ImportError):
        transcribe._cached_model_dir("small", tmp_path / "models")


def test_stt_setup_invalidates_ready_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    s = _fw_settings(tmp_path)
    t = transcribe.get(s)
    key = (s.transcribe_model, str(s.models_dir))
    transcribe._READY[key] = Path("stale")
    fake_utils = SimpleNamespace(download_model=lambda name, **kw: str(_model_dir(tmp_path / "snap")))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", fake_utils)
    t.prepare_model(lambda m: None)
    assert key not in transcribe._READY


def test_model_load_failure_forgets_ready_state(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_import", lambda self: SimpleNamespace())
    m = _model_dir(tmp_path / "mm")
    s = _fw_settings(tmp_path, transcribe_model=str(m))
    t = transcribe.get(s)
    assert t.available()

    def boom(self):
        raise RuntimeError("bad model")

    monkeypatch.setattr(transcribe.FasterWhisperTranscriber, "_load_model", boom)
    transcribe._MODELS.clear()
    with pytest.raises(transcribe.TranscribeError):
        t._model()
    assert (s.transcribe_model, str(s.models_dir)) not in transcribe._READY


# ---- HF_HOME ----

def test_hf_home_helper_sets_models_dir_and_runs_before_library_use(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    s = _fw_settings(tmp_path)
    transcribe.get(s)  # 어느 경로든 엔진을 만들면 (서버 시작·check·stt-setup·stt-test) 라이브러리를 가져오기 전에 정해진다
    assert Path(os.environ["HF_HOME"]) == s.models_dir / ".hf_home"


def test_hf_home_overrides_an_existing_value_and_is_one_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "프로필"))
    transcribe.configure_hf_home(tmp_path / "models")
    assert Path(os.environ["HF_HOME"]) == tmp_path / "models" / ".hf_home"
    src = Path(transcribe.__file__).read_text(encoding="utf-8")
    assert src.count('"HF_HOME"') == 1  # 환경 변수는 이 한 곳에서만 만진다


def test_every_cli_path_configures_hf_home_before_importing(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path, 'transcriber = "faster-whisper"\n')))
    monkeypatch.setattr(transcribe, "_package_installed", lambda: False)
    audio = tmp_path / "x.wav"
    audio.write_bytes(b"x")
    for argv in (["stt-setup"], ["stt-test", str(audio)], ["check"]):
        monkeypatch.delenv("HF_HOME", raising=False)
        try:
            cli.main(argv)
        except SystemExit:
            pass
        assert Path(os.environ["HF_HOME"]).parts[-2:] == ("models", ".hf_home"), argv


# ---- 언어 코드 ----

@pytest.mark.parametrize("code", ["ko", "KO", "en", "ja", "zh", "yue", "auto", "AUTO"])
def test_language_accepts_whisper_codes_and_auto(tmp_path, code):
    assert load_settings(_config(tmp_path, f'transcribe_language = "{code}"\n')).transcribe_language == code.lower()


@pytest.mark.parametrize("code", ["korean", "kr", "xx", "k", "ko-KR"])
def test_language_rejects_unknown_codes_in_korean(tmp_path, code):
    with pytest.raises(SystemExit) as e:
        load_settings(_config(tmp_path, f'transcribe_language = "{code}"\n'))
    text = str(e.value)
    assert "transcribe_language" in text and "언어 코드" in text and "auto" in text


def test_language_list_comes_from_library_when_importable(tmp_path, monkeypatch):
    fake = SimpleNamespace(_LANGUAGE_CODES=("ko", "zz"))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "faster_whisper.tokenizer", fake)
    monkeypatch.setattr(transcribe, "_package_installed", lambda: True)
    s = load_settings(_config(tmp_path, 'transcriber = "faster-whisper"\ntranscribe_language = "zz"\n'))
    assert s.transcribe_language == "zz"
    with pytest.raises(SystemExit):  # 라이브러리 목록에 없으면 (내장 목록에 있어도) 이 라이브러리로는 못 쓴다
        load_settings(_config(tmp_path, 'transcriber = "faster-whisper"\ntranscribe_language = "ja"\n'))


def test_builtin_language_list_matches_installed_library_when_present():
    tok = pytest.importorskip("faster_whisper.tokenizer")
    assert set(transcribe.BUILTIN_LANGUAGES) == set(tok._LANGUAGE_CODES)


# ---- 받아쓴 글 저장본에 언어 ----

def test_cache_header_records_language_and_other_language_invalidates(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    stt.language = "ko"
    _upload(user_client, [AUDIO])
    _run(app, settings)
    it = _latest(db)
    (f,) = _files(db, it)
    lines = _cache(settings, f).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#model: small" and lines[1] == "#language: ko"
    assert intake_lib.read_transcript_cache(settings, f, "small", "ko") is not None
    assert intake_lib.read_transcript_cache(settings, f, "small", "en") is None
    assert intake_lib.read_transcript_cache(settings, f, "small", "auto") is None
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    assert len(stt.calls) == 1  # 같은 언어는 다시 받아쓰지 않는다
    stt.language = "en"
    import shutil
    shutil.rmtree(intake_lib.work_dir(settings, it))  # 작업 폴더를 다시 만들 때 저장본을 쓸지 가른다 (모델이 바뀐 경우와 같다)
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    _run(app, settings)
    assert len(stt.calls) == 2  # 다른 언어면 다시 받아쓴다
    assert _cache(settings, f).read_text(encoding="utf-8").splitlines()[1] == "#language: en"


# ---- I3 엔진을 나중에 켠 뒤 ----

def _enable_engine(settings, monkeypatch, request):
    return request.getfixturevalue("stt")


NOTE = "이제 음성을 받아쓸 수 있어요. 다시 정리를 눌러 주세요."


def test_audio_only_bundle_uploaded_with_engine_off_is_picked_up_after_enabling(app, user_client, db, settings, fake_claude, request):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    it = _latest(db)
    (f,) = _files(db, it)
    assert (it.status, f.read_status) == ("failed", "needs_transcript")
    assert NOTE not in user_client.get(f"/intake/{it.id}").text  # 엔진이 꺼져 있으면 옛 안내
    engine = request.getfixturevalue("stt")
    page = user_client.get(f"/intake/{it.id}").text
    assert NOTE in page and "글자로 풀어 올려 주세요" not in page
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    it = _latest(db)
    (f,) = _files(db, it)
    assert it.status == "analyzing" and f.read_status == "pending_transcript"
    _run(app, settings)
    it = _latest(db)
    (f,) = _files(db, it)
    assert it.status == "ready" and f.read_status == "ok" and len(engine.calls) == 1


def test_mixed_bundle_audio_is_picked_up_on_reanalyze(app, user_client, db, settings, fake_claude, request):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO, DOC])
    _run(app, settings)
    it = _latest(db)
    audio, doc = _files(db, it)
    assert it.status == "ready" and audio.read_status == "needs_transcript" and doc.read_status == "ok"
    engine = request.getfixturevalue("stt")
    assert NOTE in user_client.get(f"/intake/{it.id}").text
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    assert _files(db, _latest(db))[0].read_status == "pending_transcript"
    _run(app, settings)
    audio, doc = _files(db, _latest(db))
    assert _latest(db).status == "ready" and audio.read_status == "ok" and doc.read_status == "ok"
    assert len(engine.calls) == 1
    assert NOTE not in user_client.get(f"/intake/{it.id}").text


def test_needs_prepare_reacts_to_needs_transcript_only_when_engine_is_available(app, user_client, db, settings, request):
    _upload(user_client, [AUDIO, DOC])
    it = _latest(db)
    assert intake_lib.needs_prepare(settings, it) is False  # 엔진이 꺼져 있다: 할 일 없음
    request.getfixturevalue("stt")
    assert intake_lib.needs_prepare(settings, it) is True
    intake_lib.ensure_workdir(settings, it)
    assert _files(db, it)[0].read_status == "pending_transcript"
    assert intake_lib.needs_prepare(settings, it) is False


def test_applied_and_discarded_bundles_are_untouched(app, user_client, db, settings, request):
    _upload(user_client, [AUDIO, DOC])
    it = _latest(db)
    request.getfixturevalue("stt")
    for status in ("applied", "discarded"):
        db.execute(update(Intake).where(Intake.id == it.id).values(status=status))
        db.commit()
        db.expire_all()
        assert intake_lib.needs_prepare(settings, db.get(Intake, it.id)) is False
        r = user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
        assert r.status_code == 400
        assert _files(db, it)[0].read_status == "needs_transcript"
        assert NOTE not in user_client.get(f"/intake/{it.id}").text


def test_note_is_not_shown_while_analyzing(app, user_client, db, settings, fake_claude, request):
    _upload(user_client, [AUDIO, DOC])
    it = _latest(db)
    request.getfixturevalue("stt")
    db.execute(update(Intake).where(Intake.id == it.id).values(status="analyzing"))
    db.commit()
    assert NOTE not in user_client.get(f"/intake/{it.id}").text


def test_uploaded_state_start_button_also_prepares(app, user_client, db, settings, fake_claude, request):
    """상태가 `uploaded`("정리 시작" 버튼)인 묶음도 같은 길을 탄다."""
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    it = _latest(db)
    db.execute(update(Intake).where(Intake.id == it.id).values(status="uploaded", error=""))
    db.commit()
    request.getfixturevalue("stt")
    r = user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert _latest(db).status == "analyzing"


# ---- 작은 것들: 묵은 퍼센트, 풀린 안내 ----

def test_new_analysis_resets_progress_and_read_error(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    stt.error = transcribe.TranscribeError("일시 오류")
    _upload(user_client, [AUDIO, DOC])
    _run(app, settings)
    it = _latest(db)
    (audio, _doc) = _files(db, it)
    assert audio.read_status == "failed" and audio.read_error
    db.execute(update(IntakeFile).where(IntakeFile.id == audio.id).values(progress=77))
    db.commit()
    user_client.post(f"/intake/{it.id}/analyze", data={}, follow_redirects=False)
    audio = _files(db, it)[0]
    assert _latest(db).status == "analyzing" and audio.progress is None and audio.read_error is None


def test_percent_shown_only_while_job_is_running(app, user_client, db, settings, stt, fake_claude):
    _upload(user_client, [AUDIO])
    it = _latest(db)
    (audio,) = _files(db, it)
    db.execute(update(IntakeFile).where(IntakeFile.id == audio.id).values(progress=40))
    db.commit()
    assert it.status == "analyzing"
    job = db.get(AIJob, it.job_id)
    assert job.status == "pending"
    assert "받아쓰는 중 40%" not in user_client.get(f"/intake/{it.id}").text
    db.execute(update(AIJob).where(AIJob.id == job.id).values(status="running"))
    db.commit()
    assert "받아쓰는 중 40%" in user_client.get(f"/intake/{it.id}").text


# ---- _write_file 재시도, 읽기 연결 ----

def test_write_file_retries_on_locked_database(app, db, settings, monkeypatch):
    it = Intake(note="", status="analyzing")
    db.add(it)
    db.flush()
    f = IntakeFile(intake_id=it.id, original_name="a.m4a", stored_name="1.m4a", kind="audio", size=1, read_status="pending_transcript")
    db.add(f)
    db.commit()
    monkeypatch.setattr(intake_tasks.time, "sleep", lambda s: None)
    real = intake_tasks.Session
    state = {"fail": 2}

    class Flaky(real):
        def commit(self):
            if state["fail"]:
                state["fail"] -= 1
                self.rollback()
                raise OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
            super().commit()

    monkeypatch.setattr(intake_tasks, "Session", Flaky)
    intake_tasks._write_file(app.state.engine, f.id, read_status="ok", progress=100)
    db.expire_all()
    assert db.get(IntakeFile, f.id).read_status == "ok" and state["fail"] == 0

    state["fail"] = 99
    with pytest.raises(OperationalError):  # 끝내 안 되면 위로 알린다
        intake_tasks._write_file(app.state.engine, f.id, read_status="failed")


def test_handler_does_not_hold_a_connection_while_transcribing(app, user_client, db, settings, stt, fake_claude):
    fake_claude.set(mode="ok", result=PLAN)
    _upload(user_client, [AUDIO])
    engine = app.state.engine
    held = []
    stt.on_step = lambda name, i: held.append(engine.pool.checkedout())
    _run(app, settings)
    assert held and set(held) == {0}


# ---- 후속 조치 빼기 ----

def test_fu_drop_skipped_when_task_text_no_longer_matches():
    item = {"draft": {"follow_ups": [{"task": "견적 보내기"}, {"task": "일정 잡기"}]},
            "dup_follow_ups": [{"row": 1, "task": "일정 잡기"}]}
    v = {"fu_drop": {1}, "fu_task": ["견적 보내기", "일정 잡기"]}
    assert intake_lib.kept_follow_up_positions(item, v) == [0]
    v = {"fu_drop": {1}, "fu_task": ["견적 보내기", "전혀 다른 일을 적었어요"]}
    assert intake_lib.kept_follow_up_positions(item, v) == [0, 1]  # 고친 글이면 빼지 않는다
    v = {"fu_drop": {1}, "fu_task": ["견적 보내기", "  일정   잡기 "]}
    assert intake_lib.kept_follow_up_positions(item, v) == [0]  # 공백 차이는 같은 글
    v = {"fu_drop": {1}, "fu_task": ["견적 보내기"]}
    assert intake_lib.kept_follow_up_positions(item, v) == [0]  # 줄이 사라졌으면 빼기는 무시


# ---- 명령줄 글자 깨짐 ----

def test_cli_console_survives_cp949_and_redirects(monkeypatch):
    raw = io.BytesIO()
    fake = io.TextIOWrapper(raw, encoding="cp949", errors="strict")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp949", errors="strict")
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", err)
    cli._configure_console()
    print("받아쓴 글 😀 ♪")
    print("오류 😀", file=sys.stderr)
    fake.flush()
    assert "받아쓴 글".encode("cp949") in raw.getvalue() and b"?" in raw.getvalue()


def test_cli_main_configures_console_first(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_configure_console", lambda: called.append(1))
    monkeypatch.setenv("SOLBOX_CONFIG", str(_config(tmp_path)))
    with pytest.raises(SystemExit):
        cli.main(["stt-test", str(tmp_path / "없음")])
    assert called == [1]


def test_cli_console_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(write=lambda s: None))
    cli._configure_console()  # 오류 없이 지나간다


# ---- 문서 ----

def test_docs_cover_ram_wheels_unverified_and_troubleshooting():
    root = Path(__file__).resolve().parent.parent / "docs"
    guide = (root / "운영가이드.md").read_text(encoding="utf-8")
    sec = guide[guide.index("## 12. 음성 받아쓰기"):guide.index("## 보안 참고")]
    for word in ("av<19", "TypeError", "메모리", "개발 중 합성 음성으로 측정", "계속 올라가 있", "pip install -e",
                 "onnxruntime", "ctranslate2", "wma", "large-v3-turbo", "Windows", "한국어", "transcribe_chunk_minutes",
                 "받아쓰기 프로그램 오류", "vad_filter", "자체 점검", "다시 정리"):
        assert word in sec, word
    spec = (root / "superpowers" / "specs" / "2026-11-20-audio-transcription-design.md").read_text(encoding="utf-8")
    for word in ("개발 중 합성 음성으로 측정", "hotwords", "transcribe_chunk_minutes", "120", "계속 올라가 있"):
        assert word in spec, word
