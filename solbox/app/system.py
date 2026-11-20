"""시스템 점검: 관리자 화면과 `python -m app.cli check` 가 함께 쓰는 도우미."""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from app import transcribe
from app.config import Settings

MIN_PYTHON = (3, 11)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"  # pragma: no cover


def folder_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def app_version() -> str:
    try:
        return metadata.version("solbox")
    except metadata.PackageNotFoundError:
        return "알 수 없음"


def sqlite_target(path: Path) -> str:
    """SQLite 에 넘길 경로. file: URI 로 바꾸지 않는다 (UNC 경로는 URI 로 만들면 SQLite 가 거부한다)."""
    return str(path)


def open_readonly(path: Path, timeout: float = 5) -> sqlite3.Connection:
    """쓰지 못하는 연결로 연다. 파일이 있는지는 부른 쪽이 먼저 확인해야 한다."""
    conn = sqlite3.connect(sqlite_target(path), timeout=timeout)
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


Check = tuple[str, str | None, str]  # (항목, 문제 내용 또는 None, 덧붙일 설명)


def _check_python() -> Check:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return ("Python 버전", f"{text} 입니다. 3.11 이상이 필요합니다.", "")
    return ("Python 버전", None, text)


def _check_data_dir(settings: Settings) -> Check:
    d = settings.data_dir
    if not d.exists():
        parent = d.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if os.access(parent, os.W_OK):
            return ("데이터 폴더", None, f"{d} (아직 없음. 처음 실행할 때 만들어집니다)")
        return ("데이터 폴더", f"{d} 를 만들 수 없습니다 (쓰기 권한이 없습니다).", "")
    try:
        with tempfile.TemporaryFile(dir=d):
            pass
    except OSError as e:
        return ("데이터 폴더", f"{d} 에 쓸 수 없습니다 ({e}).", "")
    return ("데이터 폴더", None, f"{d} (쓰기 가능)")


def _check_db(settings: Settings) -> Check:
    path = settings.db_path
    if not path.is_file():
        return ("데이터베이스", None, "아직 없음. 처음 실행할 때 만들어집니다")
    try:
        conn = open_readonly(path)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return ("데이터베이스", f"{path} 를 열 수 없습니다 ({e}).", "")
    return ("데이터베이스", None, str(path))


def _check_knowledge(settings: Settings) -> Check:
    if settings.knowledge_dir.is_dir():
        return ("지식 폴더", None, str(settings.knowledge_dir))
    if not settings.data_dir.exists() or not settings.db_path.exists():  # 처음 설치: 처음 실행할 때 만들어진다
        return ("지식 폴더", None, "아직 없음. 처음 실행할 때 만들어집니다")
    return ("지식 폴더", f"{settings.knowledge_dir} 가 없습니다. python -m app.cli rebuild-knowledge 로 만드세요.", "")


def _check_claude(settings: Settings) -> Check:
    name = settings.claude_cmd[0] if settings.claude_cmd else ""
    exe = shutil.which(name) if name else None
    if exe is None:
        return ("Claude CLI", f"'{name}' 을(를) 찾을 수 없습니다. 설치하고 PATH 를 확인하세요.", "")
    try:
        done = subprocess.run(
            [exe, *settings.claude_cmd[1:], "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return ("Claude CLI", f"--version 을 실행하지 못했습니다 ({e}).", "")
    if done.returncode != 0:
        return ("Claude CLI", f"--version 이 실패했습니다 (종료 코드 {done.returncode}).", "")
    return ("Claude CLI", None, (done.stdout or "").strip().splitlines()[0] if (done.stdout or "").strip() else exe)


def _check_transcriber(settings: Settings) -> Check:
    t = transcribe.get(settings)
    if settings.transcriber == "none":
        return ("음성 받아쓰기", None, "사용 안 함")
    if not t.available():
        return ("음성 받아쓰기", t.label(), "")
    return ("음성 받아쓰기", None, t.label())


def _check_cert(settings: Settings) -> Check | None:
    crt, key = settings.ssl_certfile, settings.ssl_keyfile
    if not crt and not key:
        return None
    if not crt or not key:
        return ("인증서", "ssl_certfile 과 ssl_keyfile 을 둘 다 적어야 합니다.", "")
    for p in (crt, key):
        if not Path(p).is_file():
            return ("인증서", f"파일이 없습니다: {p}", "")
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(crt, key)
    except (ssl.SSLError, OSError) as e:
        return ("인증서", f"인증서와 키를 읽을 수 없습니다 ({e}).", "")
    return ("인증서", None, str(crt))


def _check_port(settings: Settings) -> Check:
    s = socket.socket(socket.AF_INET6 if ":" in settings.host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":  # Windows 에서는 이 옵션이 이중 사용을 허용해 버린다
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((settings.host, settings.port))
    except PermissionError:
        return ("포트", f"포트 {settings.port}을 열 수 없습니다 (다른 프로그램이 쓰고 있거나 권한이 없습니다).", "")
    except OSError as e:
        if isinstance(e, socket.gaierror):
            return ("포트", f"주소 {settings.host} 를 쓸 수 없습니다 ({e}).", "")
        return ("포트", f"포트 {settings.port}을 다른 프로그램이 쓰고 있습니다 (서버가 이미 실행 중이면 정상입니다).", "")
    finally:
        s.close()
    return ("포트", None, f"{settings.host}:{settings.port} 사용 가능")


def run_checks(load: Callable[[], Settings]) -> list[Check]:
    """점검 결과 목록. 설정을 읽지 못하면 그 한 줄만 돌려준다. 서버를 띄우거나 데이터를 바꾸지 않는다."""
    try:
        settings = load()
    except SystemExit as e:
        return [("설정 파일 읽기", str(e.code).strip() or "읽을 수 없습니다.", "")]
    results: list[Check] = [("설정 파일 읽기", None, "")]
    results.append(_check_python())
    results.append(_check_data_dir(settings))
    results.append(_check_db(settings))
    results.append(_check_knowledge(settings))
    results.append(_check_claude(settings))
    results.append(_check_transcriber(settings))
    cert = _check_cert(settings)
    if cert is not None:
        results.append(cert)
    results.append(_check_port(settings))
    return results
