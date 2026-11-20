import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture(autouse=True)
def _no_daily_jobs(monkeypatch):
    """일일 작업 목록은 테스트마다 비운 채로 시작한다 (백업 등은 scheduler.start 가 등록한다)."""
    from app import scheduler

    monkeypatch.setattr(scheduler, "DAILY_JOBS", [])


@pytest.fixture(autouse=True)
def _keep_hf_home(monkeypatch):
    """받아쓰기 엔진을 만들면 `HF_HOME` 이 정해진다. 시험이 끝나면 원래대로 돌린다."""
    import os

    monkeypatch.setenv("HF_HOME", os.environ.get("HF_HOME", ""))
    if not os.environ["HF_HOME"]:
        monkeypatch.delenv("HF_HOME")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        secret_key="test",
        ai_worker_enabled=False,
        scheduler_enabled=False,
        claude_cmd=[sys.executable, str(FAKE_CLAUDE)],
    )


@pytest.fixture
def app(settings):
    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def db(app):
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(app):
    return TestClient(app)


def make_user(db, username, role="user", password="pw1234", name=None):
    from app.models import User
    from app.security import hash_password

    user = User(
        username=username,
        name=name or username,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def _logged_in_client(app, db, username, role):
    make_user(db, username, role=role)
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": "pw1234"}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def user_client(app, db):
    return _logged_in_client(app, db, "kim", "user")


@pytest.fixture
def admin_client(app, db):
    return _logged_in_client(app, db, "admin", "admin")


class FakeClaude:
    """tests/fake_claude.py 를 조종하는 제어 파일 도우미."""

    def __init__(self, control: Path):
        self.control = control
        self.set()

    def set(self, **keys):
        self.control.write_text(json.dumps(keys), encoding="utf-8")

    def calls(self) -> list[dict]:
        log = Path(str(self.control) + ".calls.jsonl")
        if not log.exists():
            return []
        lines = log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    control = tmp_path / "fake_claude_control.json"
    monkeypatch.setenv("FAKE_CLAUDE_CONTROL", str(control))
    return FakeClaude(control)


def make_proposal_world(db, settings):
    """제안 건 시험용 세계: 고객·사업, 박스 등록 솔루션 둘과 수요조사 솔루션 하나, 지식 사본, 제안 건."""
    from app.knowledge import KnowledgeMirror
    from app.models import Business, Customer, Proposal, Solution

    c = Customer(name="한빛병원")
    db.add(c)
    db.flush()
    b = Business(name="설비 유지보수", customer_id=c.id)
    s1 = Solution(name="AI 에너지 절감", stage="boxed", summary="전력 절감")
    s2 = Solution(name="스마트 조명", stage="boxed", summary="조명")
    s3 = Solution(name="안전 관제", stage="survey", summary="안전")
    db.add_all([b, s1, s2, s3])
    db.flush()
    p = Proposal(customer_id=c.id, business_id=b.id, request_memo="에너지 절감 쪽으로")
    db.add(p)
    db.commit()
    mirror = KnowledgeMirror(settings.knowledge_dir)
    for obj in (c, b, s1, s2, s3):
        mirror.sync(obj)
    return p, c, b, s1, s2, s3


def refining_proposal(db, settings):
    """방향 둘을 저장하고 첫 번째(전력비 절감 패키지)를 선택해 구체화 단계가 된 제안 건."""
    p, c, b, s1, s2, s3 = make_proposal_world(db, settings)
    p.directions = [
        {"title": "전력비 절감 패키지", "summary": "", "effects": [], "needs_check": [], "rationale": [],
         "solutions": [{"solution_id": s1.id, "name": s1.name, "role": ""}, {"solution_id": s2.id, "name": s2.name, "role": ""}]},
        {"title": "조명 단독 교체", "summary": "", "effects": [], "needs_check": [], "rationale": [],
         "solutions": [{"solution_id": s2.id, "name": s2.name, "role": ""}]},
    ]
    p.selected_index = 0
    p.status = "refining"
    p.solutions = [s1, s2]
    db.commit()
    return p


def outlined_proposal(db, settings):
    """구성안(8장)이 저장된 제안 건. 2번 장에 확인 필요 항목과 출처가 있다."""
    from datetime import datetime

    from app.ai.proposal_tasks import Outline
    from app.proposals import clean_outline

    p = refining_proposal(db, settings)
    outline = clean_outline(Outline.model_validate(sample_outline(8)), settings.knowledge_dir)
    for slide in outline["slides"][1:-1]:  # 내용 장은 모두 출처가 있고, 확인 필요는 2번 장에만 있다
        slide["needs_check"] = []
        slide["sources"] = ["solutions/1/card.md"]
    outline["slides"][2]["needs_check"] = ["절감률 수치"]
    p.outline = outline
    p.status = "outlined"
    p.outline_updated_at = datetime.now()
    db.commit()
    return p


def sample_outline(n=8):
    """AI 가 돌려주는 모양의 구성안 dict (Outline 으로 검증된다). 첫 장 cover, 마지막 장 closing, n >= 6 이면 여섯 종류를 모두 쓴다."""
    middle = [
        {"kind": "bullets", "title": "고객 현황과 과제", "key_message": "전력비가 부담입니다.", "bullets": ["전력비 증가", "노후 설비"]},
        {"kind": "two_column", "title": "제안 개요", "left_title": "현재", "left": ["수동 운영"], "right_title": "제안", "right": ["자동 제어"]},
        {"kind": "table", "title": "솔루션 구성", "columns": ["솔루션", "역할"], "rows": [["에너지 관리", "절감"], ["조명 제어", "교체"]]},
        {"kind": "steps", "title": "추진 일정", "steps": [{"name": "착수", "detail": "1개월"}, {"name": "구축", "detail": "3개월"}]},
    ]
    k = 0
    while len(middle) < n - 2:
        k += 1
        middle.append({"kind": "bullets", "title": f"상세 {k}", "key_message": "핵심입니다.", "bullets": [f"내용 {k}"]})
    slides = [{"kind": "cover", "title": "전력비 절감 제안", "subtitle": "고객사 귀중"}] + middle[: max(n - 2, 0)] + [{"kind": "closing", "title": "감사합니다"}]
    return {"title": "전력비 절감 제안", "slides": slides[:n]}


def make_topics(db):
    """리서치 시험용 주제: 기술 둘, 정책 하나 (시장 없음)."""
    from app.models import ResearchTopic

    db.add_all([
        ResearchTopic(name="건물 에너지 관리", keywords="BEMS, AI 공조", area="tech"),
        ResearchTopic(name="설비 예지보전", keywords="", area="tech"),
        ResearchTopic(name="전기요금 정책", keywords="", area="policy"),
    ])
    db.commit()


def make_research_world(db, settings):
    """제안 작업 시험용 세계: 주제, boxed 솔루션 하나, 고객과 최근 활동(수요 단서), 그 지식 사본."""
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.knowledge import KnowledgeMirror
    from app.models import Activity, Customer, Solution

    make_topics(db)
    s1 = Solution(name="AI 공조 최적제어", stage="boxed", summary="공조 전력 절감")
    customer = Customer(name="한빛병원")
    db.add_all([s1, customer])
    db.flush()
    activity = Activity(kind="meeting", date=date.today() - timedelta(days=10), title="한빛병원 미팅", raw_text="전기요금 부담 논의",
                        demand_signals=[{"customer": "한빛병원", "content": "전기요금 절감 요청"}])
    activity.solutions = [s1]
    activity.customers = [customer]
    db.add(activity)
    db.commit()
    mirror = KnowledgeMirror(settings.knowledge_dir)
    for obj in (s1, customer, activity):
        mirror.sync(obj)
    return SimpleNamespace(s1=s1, customer=customer, activity=activity)


def finished_run(db, settings):
    """끝난(partial) 조사 실행: 기술 분야 보고서(done), 정책 분야 보고서(failed), 제안 보고서(done)와 지식 사본."""
    from datetime import datetime

    from app.knowledge import KnowledgeMirror
    from app.models import ResearchReport, ResearchRun

    world = make_research_world(db, settings)
    run = ResearchRun(trigger="manual", status="partial", finished_at=datetime.now())
    db.add(run)
    db.flush()
    tech = ResearchReport(
        run_id=run.id, kind="area", area="tech", status="done", title="기술 동향 보고",
        trends=[
            {"headline": "AI 공조 제어 확산", "summary": "병원 적용 증가",
             "sources": [{"title": "기사", "url": "https://example.com/a"}], "needs_check": []},
            {"headline": "출처 없는 동향", "summary": "", "sources": [], "needs_check": ["출처 링크 없음, 확인 필요"]},
        ],
        insights=["설비 운영 고객에 제안 기회"],
    )
    policy = ResearchReport(run_id=run.id, kind="area", area="policy", status="failed", error="조사 결과가 비어 있습니다.")
    suggest = ResearchReport(
        run_id=run.id, kind="suggest", status="done", title="발굴 방향 제안",
        suggestions=[
            {"kind": "new", "title": "수요반응(DR) 솔루션 발굴", "detail": "DR 사업자 탐색", "reason": "전기요금 정책 변화",
             "solution_ids": [], "sources": ["activities/2026/1.md"]},
            {"kind": "push", "title": "AI 공조를 병원 고객에 우선 제안", "detail": "병원 중심", "reason": "병원 수요",
             "solution_ids": [world.s1.id], "sources": []},
        ],
    )
    db.add_all([tech, policy, suggest])
    db.commit()
    mirror = KnowledgeMirror(settings.knowledge_dir)
    for r in (tech, suggest):
        mirror.sync(r)
    return run


# ---- 올리기 묶음 시험용 도우미 ----

class _PageForm(HTMLParser):
    """화면의 저장 폼이 브라우저에서 그대로 제출될 때의 값 (이름 -> 값 목록)."""

    def __init__(self, action: str):
        super().__init__(convert_charrefs=True)
        self.action = action
        self.inside = False
        self.fields: dict[str, list[str]] = {}
        self._area = None
        self._select = None

    def _add(self, name, value):
        self.fields.setdefault(name, []).append(value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("action") == self.action
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = (a.get("type") or "text").lower()
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self._add(name, a.get("value") or "on")
            elif kind not in ("submit", "button", "file", "reset"):
                self._add(name, a.get("value") or "")
        elif tag == "textarea" and name:
            self._area = (name, [])
        elif tag == "select" and name:
            self._select = [name, None, None]
        elif tag == "option" and self._select:
            value = a.get("value", "")
            if self._select[2] is None:
                self._select[2] = value
            if "selected" in a:
                self._select[1] = value

    def handle_data(self, data):
        if self._area:
            self._area[1].append(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "textarea" and self._area:
            self._add(self._area[0], "".join(self._area[1]))
            self._area = None
        elif tag == "select" and self._select:
            name, picked, first = self._select
            self._add(name, picked if picked is not None else (first or ""))
            self._select = None


def form_from_page(client, intake) -> dict[str, list[str]]:
    """정리안 확인 화면(`GET /intake/{id}`)의 저장 폼을 화면의 기본값 그대로 읽는다."""
    r = client.get(f"/intake/{intake.id}")
    assert r.status_code == 200
    parser = _PageForm(f"/intake/{intake.id}/apply")
    parser.feed(r.text)
    assert parser.fields, "저장 폼을 찾지 못했습니다"
    return parser.fields


def ready_bundle(db, settings, files, owner="kim", **plan):
    """파일을 올리고 `plan`(AI 정리안 모양) 을 검증해 저장해 둔 확인 대기(ready) 묶음."""
    import io
    from datetime import date

    from fastapi import UploadFile

    from app import intake as intake_lib
    from app.ai.intake_tasks import IntakePlanOut
    from app.models import User

    user = db.query(User).filter_by(username=owner).one()
    uploads = [UploadFile(io.BytesIO(data), filename=name) for name, data in files]
    it, _ = intake_lib.create_intake(db, settings, user, "", uploads)
    intake_lib.prepare_workdir(settings, it)
    it.plan = intake_lib.clean_plan(db, it, IntakePlanOut(**plan), date(2026, 11, 19), settings)
    it.status = "ready"
    db.commit()
    return it


def ready_intake(db, settings, owner="kim"):
    """txt 3개가 든 확인 대기 묶음과 기존 솔루션 s1, 고객 c1. 정리안: 새 솔루션(파트너 루미텍) 1번 파일,
    s1 에 자료 추가 2번 파일, 한빛병원 통화 활동 기록 3번 파일."""
    from app.models import Customer, Solution

    s1 = Solution(name="AI 공조 최적제어", stage="boxed", summary="공조")
    c1 = Customer(name="한빛병원")
    db.add_all([s1, c1])
    db.commit()
    files = [("소개서.txt", "스마트 조명 소개".encode()), ("스펙.txt", "공조 스펙".encode()), ("녹취.txt", "한빛병원 통화 내용".encode())]
    plan = {
        "solutions": [{"name": "스마트 조명 제어", "summary": "재실 감지 조명", "tags": ["조명"], "partner": "루미텍",
                       "files": [1], "reason": "제품 소개서"}],
        "additions": [{"solution_id": s1.id, "files": [2], "reason": "기존 솔루션 스펙"}],
        "activities": [{"draft": {"kind": "tapping", "date": "2026-11-17", "title": "한빛병원 통화", "discussion": ["전력비 문의"],
                                  "mentions": {"customers": ["한빛 병원"], "solutions": ["AI 공조 최적제어", "스마트 조명 제어"]}},
                        "raw_from": 3, "files": [3], "reason": "통화 녹취록"}],
    }
    return ready_bundle(db, settings, files, owner, **plan), s1, c1
