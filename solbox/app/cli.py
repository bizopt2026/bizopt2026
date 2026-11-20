"""관리용 명령줄: python -m app.cli create-admin ..."""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from app import backup, certs, transcribe
from app.config import load_settings
from app.db import Base, make_engine, make_session_factory
from app.knowledge import KnowledgeMirror
from app.migrate import add_missing_columns
from app.models import User
from app.security import hash_password
from app.system import folder_size, human_size, run_checks


def create_admin(session_factory, username: str, name: str, password: str) -> None:
    with session_factory() as db:
        if db.query(User).filter_by(username=username).first():
            raise SystemExit(f"이미 있는 아이디입니다: {username}")
        db.add(User(username=username, name=name, password_hash=hash_password(password), role="admin"))
        db.commit()


def _backup_command(verify_folder: str | None) -> None:
    if verify_folder is not None:
        problems = backup.verify(Path(verify_folder))
        if problems:
            print("백업에 문제가 있습니다:")
            for p in problems:
                print(f"- {p}")
            raise SystemExit(1)
        print("이상 없음")
        return
    settings = load_settings()
    try:
        path = backup.run_backup(settings)
    except FileNotFoundError:
        raise SystemExit(f"데이터베이스 파일이 없습니다: {settings.db_path}") from None
    print(f"백업했습니다: {path} ({human_size(folder_size(path))})")


def _make_cert_command(hosts: list[str], out: str, overwrite: bool) -> None:
    try:
        crt, key = certs.make_self_signed(Path(out), hosts, overwrite=overwrite)
    except FileExistsError as e:
        raise SystemExit(f"이미 있는 파일입니다: {e}\n덮어쓰려면 --overwrite 를 붙이세요.") from None
    except ValueError as e:
        raise SystemExit(str(e)) from None
    print(f"인증서를 만들었습니다: {crt}")
    print("config.toml 에 아래 두 줄을 넣으세요.")
    print(f'ssl_certfile = "{crt.resolve().as_posix()}"')
    print(f'ssl_keyfile = "{key.resolve().as_posix()}"')


def _check_command() -> None:
    problems = 0
    for name, problem, detail in run_checks(load_settings):
        if problem is None:
            print(f"{name}: 정상" + (f" ({detail})" if detail else ""))
        else:
            problems += 1
            print(f"{name}: 문제: {problem}")
    if problems:
        print(f"문제가 {problems}개 있습니다.")
        raise SystemExit(1)
    print("모두 정상입니다.")


def _configure_console() -> None:
    """한글을 못 쓰는 콘솔(Windows cp949 등)이나 파일로 돌릴 때 출력이 글자 오류로 끊기지 않게, 못 쓰는 글자는 ?로 바꿔 쓴다.
    표준 라이브러리의 `reconfigure` 만 쓴다 (코드 페이지는 바꾸지 않는다). 없는 스트림은 그대로 둔다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _error_text(e: transcribe.TranscribeError) -> str:
    """사용자용 안내 뒤에 관리자용 원인을 붙인다 (명령줄은 서버 PC 관리자가 본다)."""
    cause = getattr(e, "cause", "")
    return f"{e}\n원인: {cause}" if cause and cause not in str(e) else str(e)


def _stt_engine(settings) -> transcribe.Transcriber:
    """받아쓰기 명령은 설정의 `transcriber` 값과 상관없이 faster-whisper 로 점검한다 (켜기 전에 먼저 확인할 수 있게)."""
    return transcribe.TRANSCRIBERS["faster-whisper"].from_settings(settings)


def _stt_setup_command() -> None:
    settings = load_settings()
    engine = _stt_engine(settings)
    try:
        engine.prepare_model(print)
        print(engine.self_test())
    except transcribe.TranscribeError as e:
        raise SystemExit(_error_text(e)) from None
    print(f"준비가 끝났습니다. 모델은 {settings.models_dir} 에 있습니다.")
    if settings.transcriber != "faster-whisper":
        print('받아쓰기를 쓰려면 config.toml 에 transcriber = "faster-whisper" 를 적고 서버를 다시 시작하세요.')
    print("실제 녹음으로 속도를 보려면: python -m app.cli stt-test <음성 파일>")


def _stt_test_command(audio: str) -> None:
    path = Path(audio)
    if not path.is_file():
        raise SystemExit(f"음성 파일을 찾을 수 없습니다: {path}")
    settings = load_settings()
    engine = _stt_engine(settings)
    if not engine.available():
        raise SystemExit(engine.label())
    steps = [10]

    def show(fraction: float) -> None:
        pct = int(fraction * 100)
        if pct >= steps[0]:
            print(f"진행 {pct}%", flush=True)
            steps[0] = (pct // 10 + 1) * 10

    started = time.monotonic()
    try:
        text = transcribe.run(engine, path, on_progress=show)
    except transcribe.TranscribeError as e:
        raise SystemExit(_error_text(e)) from None
    elapsed = time.monotonic() - started
    print(text or "(받아쓴 글이 없습니다)")
    print(f"\n모델: {engine.label()}")
    minutes, seconds = divmod(int(elapsed), 60)
    print("걸린 시간: " + (f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"))
    audio_s = engine.last_audio_seconds
    if audio_s and elapsed > 0:
        print(f"음성 길이: 약 {audio_s / 60:.1f}분, 속도: {audio_s / elapsed:.1f}배 (음성 {audio_s / elapsed:.1f}분을 실제 1분에)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin", help="관리자 계정 만들기")
    p.add_argument("--username", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--password", help="생략하면 입력창에서 두 번 입력받는다")
    sub.add_parser("rebuild-knowledge", help="knowledge 폴더를 DB 기준으로 다시 만들기")
    p = sub.add_parser("backup", help="app.db, uploads, decks 를 백업 폴더에 복사하기")
    p.add_argument("--verify", metavar="폴더", help="백업을 만들지 않고 이 백업 폴더를 점검한다")
    p = sub.add_parser("make-cert", help="자체 서명 HTTPS 인증서 만들기")
    p.add_argument("--host", action="append", required=True, help="접속 주소(IP 또는 이름). 여러 번 쓸 수 있고 첫 항목이 대표 이름이 된다")
    p.add_argument("--out", default="certs", help="저장할 폴더 (기본 certs)")
    p.add_argument("--overwrite", action="store_true", help="이미 있는 인증서를 덮어쓴다")
    sub.add_parser("stt-setup", help="음성 받아쓰기 모델을 data_dir/models 에 받고 점검하기 (인터넷 필요, 처음 한 번)")
    p = sub.add_parser("stt-test", help="음성 파일 하나를 받아써서 글·걸린 시간·속도 보기")
    p.add_argument("audio", metavar="음성파일")
    sub.add_parser("check", help="설치 상태 점검 (서버를 띄우거나 데이터를 바꾸지 않는다)")
    _configure_console()
    args = parser.parse_args(argv)
    if args.cmd == "make-cert":
        _make_cert_command(args.host, args.out, args.overwrite)
        return
    if args.cmd == "stt-setup":
        _stt_setup_command()
        return
    if args.cmd == "stt-test":
        _stt_test_command(args.audio)
        return
    if args.cmd == "check":
        _check_command()
        return
    if args.cmd == "backup":
        _backup_command(args.verify)
        return

    password = None
    if args.cmd == "create-admin":
        password = args.password
        if password is None:
            password = getpass.getpass("비밀번호: ")
            if password != getpass.getpass("비밀번호 확인: "):
                raise SystemExit("비밀번호가 일치하지 않습니다.")

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_path)
    Base.metadata.create_all(engine)
    add_missing_columns(engine, settings.db_path)
    session_factory = make_session_factory(engine)
    try:
        if args.cmd == "rebuild-knowledge":
            with session_factory() as db:
                KnowledgeMirror(settings.knowledge_dir).rebuild_all(db, settings.uploads_dir)
            print(f"knowledge 폴더를 다시 만들었습니다: {settings.knowledge_dir}")
        else:
            create_admin(session_factory, args.username, args.name, password)
            print(f"관리자 계정을 만들었습니다: {args.username}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
