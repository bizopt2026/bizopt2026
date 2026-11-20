"""음성 받아쓰기 연결 지점과 서버 안에서 도는 엔진(faster-whisper).

엔진은 `Transcriber` 를 구현하고 `TRANSCRIBERS` 에 이름을 더한다 (설정 `transcriber` 값).
faster-whisper 는 선택 설치(`pip install -e ".[stt]"`)라서 이 모듈을 불러올 때는 가져오지 않고, 쓸 때 처음 가져온다.
받아쓰기는 프로세스 전체에서 한 번에 하나만 돈다 (`run`)."""
from __future__ import annotations

import gc
import importlib.util
import inspect
import logging
import os
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)

MODEL_NAMES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
INSTALL_HINT = 'faster-whisper 미설치: pip install -e ".[stt]"'
SETUP_HINT = "python -m app.cli stt-setup"
MARKER_EVERY_S = 60
SAMPLE_RATE = 16000  # 받아쓰기 모델이 받는 표본 수
CUT_WINDOW_S = 10.0  # 조각을 자를 때 조각 끝 이만큼 안에서 가장 조용한 곳을 찾는다
_CUT_FRAME = 800  # 조용한 곳을 찾는 칸 (50 ms)
DAMAGED = "음성을 받아쓰지 못했습니다. 파일이 손상되었거나 지원하지 않는 형식일 수 있습니다."
PROGRAM_ERROR = "받아쓰기 프로그램 오류입니다. 관리자에게 알려 주세요"
BUILTIN_LANGUAGES = (
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu ha haw he hi hr ht hu hy id is it "
    "ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so "
    "sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh yue"
).split()  # faster-whisper 1.2.1 의 언어 코드. 라이브러리가 있으면 그쪽 목록을 쓴다 (`whisper_languages`)

_RUN_LOCK = threading.Lock()  # 받아쓰기는 프로세스 전체에서 한 번에 하나만


class TranscribeError(Exception):
    """받아쓰기에 실패했다. 메시지는 화면에 그대로 보여도 되는 한국어 안내.
    `cause` 는 관리자용 원인(예외 종류와 내용)이고 화면에 보이지 않는다 (명령줄과 로그에만)."""

    def __init__(self, message: str = "", cause: str = ""):
        super().__init__(message)
        self.cause = cause


class TranscribeCancelled(Exception):
    """진행 알림 쪽에서 "이 받아쓰기는 이제 필요 없다"고 알릴 때 던진다 (묶음을 버린 경우 등). 엔진은 그대로 위로 올린다."""


ProgressCallback = Callable[[float], None]


class Transcriber:
    last_audio_seconds: float | None = None

    @classmethod
    def from_settings(cls, settings: "Settings") -> "Transcriber":
        return cls()

    def available(self) -> bool:
        raise NotImplementedError

    def transcribe(self, path: Path, *, hints: str = "", on_progress: ProgressCallback | None = None) -> str:
        """음성 파일의 글. 실패하면 `TranscribeError`. 진행(0~1)은 `on_progress` 로 알린다."""
        raise NotImplementedError

    def model_name(self) -> str:
        """받아쓴 글을 다시 써도 되는지 가를 모델 식별 글 (모델이 바뀌면 저장해 둔 글을 쓰지 않는다)."""
        return ""

    def language_name(self) -> str:
        """받아쓴 글을 다시 써도 되는지 가를 언어 식별 글 (언어 설정이 바뀌면 저장해 둔 글을 쓰지 않는다)."""
        return ""

    def label(self) -> str:
        """점검 화면에 보일 상태 문구."""
        return "사용 중" if self.available() else "사용 안 함"

    def prepare_model(self, say: Callable[[str], None] = print) -> None:
        raise TranscribeError("이 엔진은 모델 준비가 필요 없습니다.")

    def self_test(self) -> str:
        raise TranscribeError("이 엔진은 자체 점검을 지원하지 않습니다.")


class NoneTranscriber(Transcriber):
    def available(self) -> bool:
        return False

    def transcribe(self, path: Path, *, hints: str = "", on_progress: ProgressCallback | None = None) -> str:
        raise TranscribeError("음성 받아쓰기를 사용하지 않도록 설정되어 있습니다.")


# ---- 글 만들기 ----

def _clock(seconds: float) -> str:
    s = int(seconds)
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def format_segments(segments: Iterable[tuple[float, float, str]]) -> str:
    """(시작, 끝, 글) 목록을 한 조각에 한 줄로, 약 1분마다 줄 앞에 `[분:초]` 표시를 붙인 글로. 화자 표시는 없다."""
    lines: list[str] = []
    next_marker = 0.0
    for start, _end, text in segments:
        text = " ".join(str(text).split())
        if not text:
            continue
        if start >= next_marker:
            lines.append(f"[{_clock(start)}] {text}")
            next_marker = (int(start) // MARKER_EVERY_S + 1) * MARKER_EVERY_S
        else:
            lines.append(text)
    return "\n".join(lines)


# ---- faster-whisper ----

def configure_hf_home(models_dir: Path) -> None:
    """모델 저장소 라이브러리(`huggingface_hub`)의 `HF_HOME` 을 `data_dir/models/.hf_home` 으로 정한다.
    이 값은 라이브러리를 처음 가져올 때 한 번 읽히므로 faster_whisper·huggingface_hub 를 가져오기 전에 불러야 하고,
    그래서 엔진을 만들 때와 라이브러리를 가져오는 모든 곳에서 이 함수 하나만 쓴다. 모델 파일은 따로
    `cache_dir`/`download_root` 로 `models_dir` 에 받으므로, 이 값은 그 밖의 파일(토큰·임시 캐시)이 사용자 프로필에
    생기지 않게 하는 용도다. 운영자가 정해 둔 `HF_HOME` 도 덮어쓴다. 이미 가져온 뒤에는 바꿀 수 없다."""
    os.environ["HF_HOME"] = str(Path(models_dir) / ".hf_home")


def whisper_languages(models_dir: Path | None = None) -> list[str]:
    """쓸 수 있는 받아쓰기 언어 코드. `models_dir` 가 있고 faster-whisper 를 가져올 수 있으면 그 라이브러리의 목록, 아니면 내장 목록."""
    if models_dir is not None and _package_installed():
        configure_hf_home(models_dir)
        try:
            from faster_whisper.tokenizer import _LANGUAGE_CODES
            return list(_LANGUAGE_CODES)
        except Exception:  # noqa: BLE001 - 가져오지 못하면 내장 목록
            log.warning("faster-whisper 의 언어 목록을 읽지 못해 내장 목록을 씁니다", exc_info=True)
    return list(BUILTIN_LANGUAGES)


def _package_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def model_files_ok(folder: Path) -> bool:
    """`WhisperModel` 이 폴더에서 모델을 읽는 데 필요한 파일이 다 있는가: model.bin(비어 있지 않음), tokenizer.json,
    vocabulary.*. (config.json·preprocessor_config.json 은 없어도 읽힌다. 라이브러리 1.2.1 에서 확인.)"""
    try:
        weights = folder / "model.bin"
        if not weights.is_file() or weights.stat().st_size == 0 or not (folder / "tokenizer.json").is_file():
            return False
        return any(p.is_file() for p in folder.glob("vocabulary.*"))
    except OSError:
        return False


def _cached_model_dir(name: str, root: Path) -> Path | None:
    """이름으로 고른 모델이 `root` 에 이미 받아져 있으면 그 폴더, 아니면 None. 인터넷을 쓰지 않는다.
    내려받다 만 폴더(model.bin 이 없음 등)는 없는 것으로 본다. 라이브러리를 가져오지 못하는 `ImportError` 는 그대로 올린다."""
    configure_hf_home(root)
    from faster_whisper.utils import download_model
    try:
        folder = Path(download_model(name, local_files_only=True, cache_dir=str(root)))
    except Exception:  # noqa: BLE001 - 없으면 어떤 오류든 "받아져 있지 않음"
        return None
    return folder if model_files_ok(folder) else None


_READY: dict[tuple[str, str], Path] = {}  # 준비됐다고 확인한 모델 폴더 (부를 때마다 파일이 그대로인지 다시 본다)
_MODELS: dict[tuple, object] = {}
_MODELS_LOCK = threading.Lock()
_LOGGED: set[str] = set()


def _is_named(model: str) -> bool:
    return model in MODEL_NAMES


def _cause(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def _is_decode_error(e: BaseException) -> bool:
    """음성 파일 자체가 나쁠 때 PyAV 가 내는 오류(`av.error.FFmpegError` 계열)인가. 메모리·입출력 오류와
    프로그램 오류(TypeError·ImportError·AttributeError)는 파일 탓이 아니므로 제외한다."""
    if isinstance(e, (MemoryError, OSError, ImportError, AttributeError, TypeError)):
        return False
    return any(c.__name__ == "FFmpegError" and c.__module__.startswith("av") for c in type(e).__mro__)


# ---- 음성 읽기 (PyAV 로 16 kHz 모노로 조금씩) 와 조각 나누기 ----

def quietest_cut(window) -> int:
    """`window`(int16 배열) 안에서 가장 조용한 칸(50 ms)의 가운데 위치. 한 칸보다 짧으면 끝."""
    import numpy as np
    n = len(window) // _CUT_FRAME
    if n < 1:
        return len(window)
    energy = (window[:n * _CUT_FRAME].astype(np.float32).reshape(n, _CUT_FRAME) ** 2).mean(axis=1)
    return int(energy.argmin()) * _CUT_FRAME + _CUT_FRAME // 2


def split_chunks(pieces: Iterable, chunk_samples: int, window_samples: int):
    """int16 조각들을 모아 `chunk_samples` 가 넘기 전, 끝쪽 `window_samples` 안의 가장 조용한 곳에서 잘라 내보낸다.
    한 번에 한 조각 분량만 메모리에 둔다. 마지막 남은 조각도 내보낸다."""
    import numpy as np
    parts: list = []
    held = 0
    for piece in pieces:
        if not len(piece):
            continue
        parts.append(piece)
        held += len(piece)
        if held < chunk_samples:
            continue
        buf = np.concatenate(parts)
        while len(buf) >= chunk_samples:
            lo = max(0, chunk_samples - window_samples)
            cut = lo + quietest_cut(buf[lo:chunk_samples])
            yield buf[:cut]
            buf = buf[cut:]
        parts, held = ([buf] if len(buf) else []), len(buf)
    if held:
        yield np.concatenate(parts)


class _AvAudio:
    """PyAV 로 연 음성 파일: `.duration`(알면 초), `.pieces()`(16 kHz 모노 int16 조각을 차례로)."""

    def __init__(self, path: Path):
        self.path = path
        self.container = None
        self.stream = None
        self.duration: float | None = None

    def __enter__(self):
        import av
        self.av = av
        self.container = av.open(str(self.path))  # faster-whisper 의 decode_audio 와 달리 PyAV 버전에 걸리는 인자를 쓰지 않는다
        try:
            streams = self.container.streams.audio
            if not streams:
                raise TranscribeError(DAMAGED)
            self.stream = streams[0]
            if self.container.duration:
                self.duration = self.container.duration / av.time_base
            elif self.stream.duration and self.stream.time_base:
                self.duration = float(self.stream.duration * self.stream.time_base)
        except BaseException:
            self.container.close()
            raise
        return self

    def __exit__(self, *exc):
        if self.container is not None:
            self.container.close()

    def pieces(self):
        av = self.av
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            for packet in self.container.demux(self.stream):
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:  # 깨진 조각 하나는 건너뛴다 (faster-whisper 와 같은 방식)
                    continue
                for frame in frames:
                    frame.pts = None
                    for out in resampler.resample(frame):
                        yield out.to_ndarray().reshape(-1)
            for out in resampler.resample(None):
                yield out.to_ndarray().reshape(-1)
        finally:
            del resampler
            gc.collect()  # PyAV 리샘플러 관련 객체가 바로 풀리지 않는 문제 (faster-whisper 이슈 390)


def _open_audio(path: Path):
    return _AvAudio(Path(path))


class FasterWhisperTranscriber(Transcriber):
    """CPU(int8)로 서버 안에서 받아쓴다. 음성은 서버 밖으로 나가지 않는다."""

    def __init__(self, settings: "Settings"):
        self.settings = settings
        self.last_audio_seconds = None
        configure_hf_home(self._models_dir())  # 이 엔진을 쓰는 어느 길(서버·check·stt-setup·stt-test)이든 라이브러리보다 먼저

    @classmethod
    def from_settings(cls, settings: "Settings") -> "FasterWhisperTranscriber":
        return cls(settings)

    # 상태
    def model_name(self) -> str:
        return self.settings.transcribe_model

    def language_name(self) -> str:
        return self.settings.transcribe_language

    def model_label(self) -> str:
        m = self.settings.transcribe_model
        return m if _is_named(m) else Path(m).name

    def _models_dir(self) -> Path:
        return self.settings.models_dir

    def _broken(self, e: BaseException) -> str:
        key = _cause(e)
        if key not in _LOGGED:
            _LOGGED.add(key)
            log.warning("faster-whisper 를 가져오지 못했습니다", exc_info=e)
        return (f"받아쓰기 프로그램(faster-whisper)을 불러오지 못했습니다 ({type(e).__name__}). 설치가 깨졌거나 "
                f"Windows 에서는 Visual C++ 재배포 가능 패키지가 없을 수 있습니다. pip install -e \".[stt]\" 를 다시 실행해 보고, "
                "계속 안 되면 관리자에게 알려 주세요.")

    def problem(self) -> str:
        if not _package_installed():
            return INSTALL_HINT
        m = self.settings.transcribe_model
        key = (m, str(self._models_dir()))
        folder = _READY.get(key)
        if folder is not None and model_files_ok(folder):
            return ""
        _READY.pop(key, None)
        try:
            self._import()
            folder = _cached_model_dir(m, self._models_dir()) if _is_named(m) else (Path(m) if model_files_ok(Path(m)) else None)
        except (ImportError, OSError) as e:
            return self._broken(e)
        if folder is None:
            return f"모델 '{self.model_label()}' 이(가) 아직 없습니다. 서버 PC에서 {SETUP_HINT} 를 실행해 받으세요."
        _READY[key] = folder
        return ""

    def available(self) -> bool:
        return not self.problem()

    def label(self) -> str:
        return self.problem() or f"faster-whisper, {self.model_label()}, CPU"

    # 모델
    def _import(self):
        configure_hf_home(self._models_dir())
        import faster_whisper
        return faster_whisper

    def _load_model(self):
        fw = self._import()
        m = self.settings.transcribe_model
        return fw.WhisperModel(
            m, device="cpu", compute_type="int8", cpu_threads=self.settings.transcribe_threads,
            download_root=str(self._models_dir()), local_files_only=True,
        )

    def _model(self):
        key = (self.settings.transcribe_model, str(self._models_dir()), self.settings.transcribe_threads)
        with _MODELS_LOCK:
            if key not in _MODELS:
                try:
                    _MODELS[key] = self._load_model()
                except Exception as e:  # noqa: BLE001
                    log.exception("받아쓰기 모델을 불러오지 못했습니다")
                    _READY.pop(key[:2], None)  # 준비됐다는 판단이 틀렸을 수 있으니 다음에 다시 확인한다
                    raise TranscribeError(
                        f"받아쓰기 모델을 불러오지 못했습니다. {SETUP_HINT} 를 다시 실행해 보거나 관리자에게 알려 주세요.",
                        cause=_cause(e),
                    ) from None
            return _MODELS[key]

    def transcribe(self, path: Path, *, hints: str = "", on_progress: ProgressCallback | None = None) -> str:
        return self._transcribe(path, hints, on_progress, vad=True)

    def _too_long(self, duration: float | None) -> TranscribeError:
        limit = self.settings.transcribe_max_minutes
        how = f"이 파일은 약 {int(duration // 60)}분" if duration else "이 파일은 그보다 깁니다"
        return TranscribeError(f"음성이 너무 깁니다 (최대 {limit}분, {how}). 나눠서 올려 주세요.")

    def _transcribe(self, path: Path, hints: str, on_progress: ProgressCallback | None, vad: bool) -> str:
        """파일을 PyAV 로 조금씩 읽어 `transcribe_chunk_minutes` 길이의 조각으로 나눠 받아쓴다. 한 번에 한 조각만
        메모리에 있다 (모델의 `transcribe()` 에 파일 경로를 주면 전체를 한꺼번에 읽는다). 길이는 모델을 부르기 전에 확인한다."""
        problem = self.problem()
        if problem:
            raise TranscribeError(problem)
        model = self._model()
        import numpy as np
        s = self.settings
        detect = s.transcribe_language == "auto"
        lang = None if detect else s.transcribe_language
        limit_s = s.transcribe_max_minutes * 60
        chunk_s = s.transcribe_chunk_minutes * 60
        window_s = min(CUT_WINDOW_S, chunk_s / 2)
        try:
            with _open_audio(path) as src:
                duration = src.duration
                self.last_audio_seconds = duration or None
                if duration and duration > limit_s:
                    raise self._too_long(duration)
                done: list[tuple[float, float, str]] = []
                start = 0  # 지금 조각이 시작하는 표본 위치
                last = 0.0

                def report(seconds: float) -> None:
                    nonlocal last
                    if not on_progress:
                        return
                    if duration:
                        last = max(last, min(1.0, seconds / duration))
                    on_progress(last)  # 길이를 모르는 파일도 부른다: 진행률은 0 이지만 버림·다시 정리 확인이 이 호출에서 된다

                for chunk in split_chunks(src.pieces(), int(chunk_s * SAMPLE_RATE), int(window_s * SAMPLE_RATE)):
                    end = start + len(chunk)
                    if end / SAMPLE_RATE > limit_s:  # 길이를 알 수 없던 파일은 읽으면서 확인한다
                        raise self._too_long(duration)
                    audio = chunk.astype(np.float32) / 32768.0
                    segments, info = model.transcribe(
                        audio, language=lang, vad_filter=vad, condition_on_previous_text=False, hotwords=hints or None,
                    )
                    offset = start / SAMPLE_RATE
                    spoke = False
                    for seg in segments:
                        spoke = True
                        done.append((offset + float(seg.start), offset + float(seg.end), seg.text))
                        report(offset + float(seg.end))
                    if detect and lang is None and spoke:
                        # 자동 감지는 말이 나온 첫 조각의 언어로 정하고 나머지는 같은 언어로 한다.
                        # 말이 없는 조각(무음·음악)은 감지 결과가 제멋대로라 그 언어로 고정하지 않는다.
                        lang = getattr(info, "language", None) or None
                    start = end
                    report(start / SAMPLE_RATE)
                if start == 0:
                    raise TranscribeError(DAMAGED, cause="읽은 음성이 없습니다")
                if not duration:
                    self.last_audio_seconds = start / SAMPLE_RATE
            return format_segments(done)
        except (TranscribeError, TranscribeCancelled):
            raise
        except Exception as e:  # noqa: BLE001 - 라이브러리 오류의 자세한 내용은 기록에만 남긴다
            log.exception("음성 받아쓰기 오류 (%s)", Path(path).name)
            if _is_decode_error(e):
                raise TranscribeError(DAMAGED, cause=_cause(e)) from None
            raise TranscribeError(f"{PROGRAM_ERROR} ({type(e).__name__})", cause=_cause(e)) from None

    # 운영 명령
    def prepare_model(self, say: Callable[[str], None] = print) -> None:
        """설정한 모델을 `data_dir/models/` 에 받는다. 서버가 인터넷에 접속하는 유일한 곳이다."""
        if not _package_installed():
            raise TranscribeError(INSTALL_HINT)
        m = self.settings.transcribe_model
        key = (m, str(self._models_dir()))
        _READY.pop(key, None)
        if not _is_named(m):
            if not model_files_ok(Path(m)):
                raise TranscribeError(f"모델 폴더에 model.bin·tokenizer.json·vocabulary.* 가 모두 있어야 합니다: {m}")
            say(f"로컬 모델 폴더를 쓰므로 받을 것이 없습니다: {m}")
            return
        self._models_dir().mkdir(parents=True, exist_ok=True)
        try:
            self._import()
            from faster_whisper.utils import download_model
        except (ImportError, OSError) as e:
            raise TranscribeError(self._broken(e), cause=_cause(e)) from None
        say(f"모델 '{m}' 을(를) {self._models_dir()} 에 받습니다 (처음 한 번만, 크기에 따라 수 분 걸립니다)…")
        try:
            folder = Path(download_model(m, cache_dir=str(self._models_dir())))
        except Exception as e:  # noqa: BLE001
            log.exception("모델 내려받기 실패")
            raise TranscribeError(
                "모델을 내려받지 못했습니다. 인터넷 연결(모델 저장소 접속)과 디스크 여유 공간을 확인하세요. "
                f"({type(e).__name__})", cause=_cause(e),
            ) from None
        if not model_files_ok(folder):
            raise TranscribeError(f"내려받은 모델 폴더에 필요한 파일(model.bin, tokenizer.json, vocabulary.*)이 빠져 있습니다: {folder}")
        _READY.pop(key, None)  # 다음 확인에서 새 폴더를 읽는다

    def self_test(self) -> str:
        """무음 1초를 받아써 본다. 음성 구간 검출(VAD)을 끄므로 인코더·디코더가 실제로 돈다. 모델 파일이 읽히고, 라이브러리와
        오디오 읽기가 동작하는 것까지만 증명한다 (무음이라 글은 없는 것이 정상이고, 한국어 정확도·속도는 알 수 없다)."""
        import tempfile
        self._models_dir().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._models_dir()) as tmp:
            wav = Path(tmp) / "silence.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(b"\x00\x00" * SAMPLE_RATE)
            try:
                text = self._transcribe(wav, "", None, vad=False)
            except TranscribeError as e:
                if e.cause and e.cause not in str(e):
                    raise TranscribeError(f"{e} 원인: {e.cause}", cause=e.cause) from None
                raise
        return "자체 점검 통과 (무음 1초, 음성 구간 검출 끔: " + ("모델이 돌았고 글이 없는 것이 정상입니다)" if not text else f"받아쓴 글 {text!r})")


TRANSCRIBERS: dict[str, type[Transcriber]] = {"none": NoneTranscriber, "faster-whisper": FasterWhisperTranscriber}


def get(settings: "Settings") -> Transcriber:
    return TRANSCRIBERS[settings.transcriber].from_settings(settings)


def label(settings: "Settings") -> str:
    return get(settings).label()


def run(transcriber: Transcriber, path: Path, *, hints: str = "", on_progress: ProgressCallback | None = None) -> str:
    """받아쓰기를 프로세스 전체에서 한 번에 하나만 돌린다 (워커가 여럿이어도). 옛 방식(`transcribe(path)`)의 엔진도 받는다."""
    params = inspect.signature(transcriber.transcribe).parameters
    takes_kw = any(p.kind is p.VAR_KEYWORD for p in params.values())
    kwargs = {k: v for k, v in (("hints", hints), ("on_progress", on_progress)) if takes_kw or k in params}
    with _RUN_LOCK:
        return transcriber.transcribe(path, **kwargs)
