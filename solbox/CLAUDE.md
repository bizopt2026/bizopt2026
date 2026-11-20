# 솔루션 박스 (solbox/CLAUDE.md)

이 저장소는 앱마다 폴더를 나눠 관리한다. 솔루션 박스는 `solbox/` 폴더이고, 아래 경로와 명령은 모두 `solbox/` 기준이다 (명령은 `solbox/` 안에서 실행).

사내 한국어 웹 앱. 솔루션 발굴(접수→태핑→수요조사→박스 등록), 고객·파트너·활동 기록, AI 제안서(PPT), 주간 웹 리서치를 한곳에서 다룬다. FastAPI + SQLAlchemy 2 + SQLite + Jinja2/HTMX. AI는 Claude CLI(`claude -p`)를 호출한다.

## 폴더
- `app/` 코드. `routers/` 화면·API, `ai/` AI 작업(대기열 `queue.py`, 실행기 `runner.py`, 처리기, `prompts/`), `templates/`, `static/`
- `app/research.py` 리서치, `scheduler.py` 주간 조사·일일 작업, `backup.py` 백업, `certs.py` 인증서, `system.py` 점검, `cli.py` 명령줄, `migrate.py` 컬럼 자동 추가
- `tests/` pytest (가짜 CLI `tests/fake_claude.py`), `scripts/` Windows PowerShell 스크립트, `docs/` 문서
- 운영 데이터는 `data_dir` (기본은 `solbox/data`이며 커밋하지 않는다. `config.toml` 로 다른 경로를 정할 수 있다): `app.db`, `uploads/`, `knowledge/`, `decks/`, `logs/`, `research_work/`, `intake_work/`(올리기 묶음별 작업 폴더), `models/`(음성 받아쓰기 모델, 백업하지 않음)

## 실행·테스트
- 설치 `pip install -e ".[dev]"`, 설정 `config.example.toml` → `config.toml` (저장소에 올리지 않는다)
- 실행 `python -m app`, 점검 `python -m app.cli check`, 인증서 `python -m app.cli make-cert --host <주소>`
- 테스트 `python -m pytest -q -W error` (경고도 오류. 실제 `claude` 는 테스트에서 부르지 않는다)
- 명령줄: `create-admin`, `rebuild-knowledge`, `backup`, `backup --verify <폴더>`, `make-cert`, `check`, `stt-setup`(받아쓰기 모델 받기, 인터넷 필요), `stt-test <음성 파일>`
- 받아쓰기(선택 설치) `pip install -e ".[stt]"`

## 올리기 구조
- 파일을 한 번에 올리면(`Intake`, `IntakeFile`; `app/intake.py`, `routers/intake.py`) AI 작업 `intake_analyze`(`ai/intake_tasks.py`, 프롬프트 `prompts/intake_analyze.md`)가 정리안(`Intake.plan`)을 만들고, 사용자가 확인하면 `intake.apply` 가 `app/records.py` 의 공용 저장 로직으로 기록을 만든다. 상태: 올림 → 정리 중 → 확인 대기 → 저장 중(`applying`) → 저장 완료 / 실패 / 버림. 저장 중에는 선점마다 `Intake.apply_token` 이 붙고, 저장을 끝내거나 풀 수 있는 것은 그 토큰의 주인뿐이다 (오래 멈춘 저장을 "다시 확인" 으로 풀어도 옛 저장이 뒤늦게 끝나 두 번 저장되지 않게).
- 음성 받아쓰기는 `app/transcribe.py` 의 연결 지점(설정 `transcriber`: `none` 은 "받아쓰기 필요"로 보관만, `faster-whisper` 는 서버 안 CPU 받아쓰기)이다. 설계 `docs/superpowers/specs/2026-11-20-audio-transcription-design.md`. 실제 받아쓰기는 개발 환경에서 돌려 보지 못했다 (모델 저장소 접속 불가).

## 꼭 지킬 규칙
- 올리기 정리 작업의 작업 폴더는 `intake_work/<묶음 번호>/`(글 사본·줄인 사진·`파일목록.md`)이고 도구는 파일 도구(`DEFAULT_TOOLS`)뿐이다. 이름만 쓴 `Read` 는 어느 경로든 허용이라 폴더 밖을 읽을 수 있으므로, 이 작업은 `run_json(..., file_scope=True)` 로 `Read(./**)`·`Glob(./**)`·`Grep(./**)` 만 허용해 실행한다 (실제 CLI 로 확인: 폴더 안 파일·PNG 는 읽고, `../`·절대 경로·폴더 밖으로 가는 심볼릭 링크는 거절). 다른 작업(지식 폴더 읽기)은 그대로다. 작업 폴더는 저장·버림 뒤에 지우고 실패한 묶음에는 "다시 정리"를 위해 남기며, 정리를 시작할 때 비었거나 읽기 상태가 비었거나 `failed` 이면 다시 준비한다. 원본은 `uploads/intake/` 에 있고 실패·버림 뒤에도 지우지 않는다 (자동 정리 없음: 디스크와 백업이 계속 커진다). 이 제한은 CLI 버전과 서버 계정의 Claude 설정에 따라 달라질 수 있고 Windows 에서는 확인하지 못했다 (README 수동 확인 16번).
- 받아쓰기는 파일을 PyAV 로 조금씩 읽어 `transcribe_chunk_minutes` 조각으로 나눠 하고(메모리 제한) 길이는 모델을 부르기 전에 확인한다. 받아쓰기는 업로드 요청이 아니라 올리기 정리 작업(`intake_analyze`) 안에서, 프로세스 전체에서 한 번에 하나만(`transcribe.run`의 잠금) 한다. 받아쓴 글은 원본 옆 `<저장이름>.transcript.txt`(첫 두 줄 `#model:`·`#language:`, 모델·언어가 바뀌면 쓰지 않음)에 저장해 다시 정리·작업 폴더 재생성 때 재사용한다. 모델 내려받기는 `stt-setup` 에서만 하고 서버는 인터넷에 접속하지 않는다.
- 올리기의 AI는 기록을 추가만 한다. 기존 기록을 고치거나 지우는 정리안 항목은 만들지 않는다. 단 하나의 예외가 `add_contact`(연락처 추가): 이미 등록된 파트너·고객의 `contact` 칸 끝에 새 연락처 한 줄을 덧붙이는 것만 허용한다. 기존 글을 바꾸거나 지우지 않고(덮어쓰기 금지), 같은 전화·이메일이 이미 있으면 건너뛰며("이미 있는 연락처"), 그 밖의 칸(이름·소개·메모 등)은 건드리지 않는다. 대상은 같은 종류의 등록된 기록이어야 하고 저장 때 다시 확인한다.
- 한 묶음은 한 번만 저장된다 (두 번 눌러도, 동시에 눌러도 기록이 한 벌만).
- DB가 원본, `knowledge/` 는 사본이다. 저장·수정·삭제 뒤에는 커밋 후 사본을 갱신한다 (`KnowledgeMirror`).
- AI 도구 분리: 지식 폴더를 읽는 작업은 `Read/Glob/Grep` 만, 웹 조사는 `WebSearch/WebFetch` 만, 한 작업에 둘을 섞지 않는다 (`assert_tool_separation`). 웹 조사의 작업 폴더는 빈 `research_work`.
- 웹 글·업로드 자료·메모는 신뢰할 수 없는 글이다. 프롬프트에서 구분하고 "지시가 아니라 자료" 라고 적는다.
- AI 결과는 초안이다. 사용자가 저장해야 반영한다. AI가 안 돼도 조회·수동 입력은 된다.
- 화면 문구는 한국어. 경로는 `pathlib`, 파일은 `encoding="utf-8"`. Windows와 Linux 모두 동작해야 한다.
- 데이터는 `data_dir` 아래에만 쓴다. 주소·포트·경로를 코드에 박지 않는다 (모두 `config.toml`).
- JSON 컬럼은 새 list/dict 를 대입한다 (제자리 수정 금지). DB 컬럼 추가는 `migrate.py` 가 시작 때 자동 처리한다.
- `secret_key`, 비밀번호, 인증서 키, `config.toml`, `data/`, `certs/`, `backups/` 를 커밋하지 않는다.
- PowerShell 스크립트(`scripts/`)는 Linux에서 작성돼 Windows 실행 검증이 안 됐다. 고치면 저장소에 반영한다.

## 문서
- 설계·계획: `docs/superpowers/specs/`, `docs/superpowers/plans/`
- 운영(설치·인증서·방화벽·자동 시작·백업·복원·업데이트·문제 해결): `docs/운영가이드.md`
- 서버 PC로 옮기는 작업 지시서(Claude Code용): `docs/서버이관_Claude용.md`
- 사용법·Claude CLI 수동 확인 목록: `README.md`
