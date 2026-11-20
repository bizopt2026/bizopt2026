"""설정 파일(TOML) 읽기."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from app.transcribe import MODEL_NAMES, TRANSCRIBERS, whisper_languages


@dataclass
class Settings:
    data_dir: Path
    secret_key: str
    host: str = "127.0.0.1"
    port: int = 8443
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    claude_cmd: list[str] = field(default_factory=lambda: ["claude"])
    ai_concurrency: int = 1
    ai_timeout_s: int = 600
    research_timeout_s: int = 1800
    ai_worker_enabled: bool = True
    scheduler_enabled: bool = True
    max_upload_mb: int = 100
    backup_dir: Path | None = None
    backup_keep_days: int = 14
    transcriber: str = "none"
    transcribe_model: str = "small"
    transcribe_language: str = "ko"
    transcribe_threads: int = 0
    transcribe_max_minutes: int = 120
    transcribe_chunk_minutes: int = 10

    def __post_init__(self) -> None:
        if self.backup_dir is None:
            self.backup_dir = self.data_dir.parent / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"

    @property
    def research_work_dir(self) -> Path:
        return self.data_dir / "research_work"

    @property
    def intake_work_dir(self) -> Path:
        return self.data_dir / "intake_work"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def decks_dir(self) -> Path:
        return self.data_dir / "decks"


def _bad(key: str, why: str) -> SystemExit:
    return SystemExit(f"설정 파일의 '{key}' 값이 올바르지 않습니다: {why}")


def _check_transcribe_settings(raw: dict, config_path: Path, models_dir: Path) -> None:
    """음성 받아쓰기 설정을 검사한다. 모델이 폴더 경로면 설정 파일 기준 절대 경로로 바꿔 둔다."""
    if "transcribe_model" in raw:
        model = raw["transcribe_model"]
        if not isinstance(model, str) or not model.strip():
            raise _bad("transcribe_model", f"{model!r} (모델 이름이나 모델 폴더 경로를 글로 적으세요)")
        model = model.strip()
        if model not in MODEL_NAMES:
            if not any(c in model for c in "/\\") and not model.startswith((".", "~")):
                raise _bad("transcribe_model", f"{model!r}\n쓸 수 있는 이름: {', '.join(MODEL_NAMES)} 또는 모델 폴더 경로")
            folder = Path(model).expanduser()
            if not folder.is_absolute():
                folder = config_path.resolve().parent / folder
            model = str(folder)
        raw["transcribe_model"] = model
    if "transcribe_language" in raw:
        lang = raw["transcribe_language"]
        if not isinstance(lang, str) or not lang.strip():
            raise _bad("transcribe_language", f"{lang!r} (예: \"ko\", 자동 감지는 \"auto\")")
        lang = lang.strip().lower()
        # 받아쓰기를 켠 경우에만 설치된 라이브러리의 언어 목록을 본다 (아니면 내장 목록)
        known = whisper_languages(models_dir if raw.get("transcriber") == "faster-whisper" else None)
        if lang != "auto" and lang not in known:
            raise _bad("transcribe_language", f"{lang!r} (Whisper 언어 코드가 아닙니다. 예: \"ko\", 자동 감지는 \"auto\")")
        raw["transcribe_language"] = lang
    for key, minimum in (("transcribe_threads", 0), ("transcribe_max_minutes", 1), ("transcribe_chunk_minutes", 1)):
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
                raise _bad(key, f"{v!r} ({minimum} 이상의 정수)")


def load_settings(path: Path | None = None) -> Settings:
    if path is None:
        env = os.environ.get("SOLBOX_CONFIG")
        path = Path(env) if env else Path("config.toml")
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"설정 파일을 찾을 수 없습니다: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(
            f"설정 파일을 읽을 수 없습니다: {path} ({e})\n"
            "Windows 경로는 C:/solbox/data 처럼 / 로 쓰거나 'C:\\solbox\\data' 처럼 작은따옴표로 감싸세요."
        ) from None
    known = {f.name for f in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise SystemExit(f"알 수 없는 설정 키: {', '.join(sorted(unknown))}")
    for key in ("data_dir", "secret_key"):
        if key not in raw:
            raise SystemExit(f"설정 파일에 '{key}' 값이 없습니다: {path}")
    if str(raw["secret_key"]).strip() in ("", "change-me"):
        raise SystemExit(
            f"설정 파일의 'secret_key' 를 길고 무작위한 값으로 바꾸세요: {path}\n"
            'python -c "import secrets; print(secrets.token_urlsafe(32))" 로 만들 수 있습니다.'
        )
    transcriber = raw.get("transcriber", "none")
    if transcriber not in TRANSCRIBERS:
        raise SystemExit(
            f"설정 파일의 'transcriber' 값이 올바르지 않습니다: {transcriber!r}\n"
            f"쓸 수 있는 값: {', '.join(sorted(TRANSCRIBERS))}"
        )
    data_dir = Path(raw["data_dir"])
    if not data_dir.is_absolute():
        data_dir = path.resolve().parent / data_dir
    raw["data_dir"] = data_dir
    _check_transcribe_settings(raw, path, data_dir / "models")
    if "backup_dir" in raw:
        backup_dir = Path(raw["backup_dir"])
        if not backup_dir.is_absolute():
            backup_dir = path.resolve().parent / backup_dir
        raw["backup_dir"] = backup_dir
        resolved = backup_dir.resolve()
        for sub in ("uploads", "decks"):
            if resolved.is_relative_to((data_dir / sub).resolve()):
                raise SystemExit(
                    f"'backup_dir' 는 data_dir 안의 {sub} 폴더 안에 둘 수 없습니다 (백업이 자기 자신을 복사하게 됩니다): {backup_dir}"
                )
    return Settings(**raw)
