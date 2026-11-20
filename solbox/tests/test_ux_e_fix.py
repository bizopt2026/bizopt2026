"""UX 물결 E 마무리: 연락처 덧붙이기 경쟁·중복·대상 검사, 구성안 확인 동시성, 문구·접근성 손질."""
import re
import threading
from datetime import date
from pathlib import Path

from sqlalchemy import event

from app import intake as intake_lib
from app.ai.intake_tasks import Contact, IntakePlanOut
from app.models import Customer, Intake, Partner, Proposal, User
from tests.conftest import form_from_page, make_user, outlined_proposal, ready_bundle

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
CARD = ("명함.txt", "x".encode())
TODAY = date(2026, 11, 19)


def _c(p, **kw):
    base = dict(target_type="partner", target_id=p.id, person="박서연", role="팀장", phone="02-555-0142",
                email="seoyeon@hanbit.example", files=[1], reason="명함")
    return Contact(**{**base, **kw})


def _v(**kw):
    return {"person": "", "role": "", "phone": "", "email": "", "address": "", "note": "", **kw}


def _apply(client, it, form=None):
    return client.post(f"/intake/{it.id}/apply", data=form or form_from_page(client, it), follow_redirects=False)


# ---- I1 두 묶음이 같은 파트너에 덧붙일 때 한 줄도 잃지 않는다 ----

def test_interleaved_appends_keep_both_lines(app, user_client, db, settings, monkeypatch):
    p = Partner(name="한빛", contact="대표 02-111-2222")
    db.add(p)
    db.commit()
    a = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    b = ready_bundle(db, settings, [CARD], contacts=[_c(p, person="이민호", phone="010-9999-8888", email="minho@hanbit.example")])
    fa, fb = form_from_page(user_client, a), form_from_page(user_client, b)
    user = db.query(User).filter_by(username="kim").one()
    errors = []

    def run_b():
        with app.state.session_factory() as db2:
            try:
                intake_lib.apply(db2, settings, app.state.mirror, db2.get(Intake, b.id), db2.get(User, user.id), fb)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    orig = intake_lib.contact_line
    state = {"started": False}
    thread = threading.Thread(target=run_b)

    def hooked(v, files, today):
        if not state["started"]:  # A 가 연락처를 읽은 직후에 B 가 끼어든다 (A 가 쓰기 잠금을 쥐고 있으면 B 는 기다린다)
            state["started"] = True
            thread.start()
            thread.join(1.5)
        return orig(v, files, today)

    monkeypatch.setattr(intake_lib, "contact_line", hooked)
    assert _apply(user_client, a, fa).status_code == 303
    thread.join(15)
    assert not errors
    db.expire_all()
    contact = db.get(Partner, p.id).contact
    assert "02-555-0142" in contact and "010-9999-8888" in contact and contact.startswith("대표 02-111-2222\n")
    assert db.get(Intake, a.id).status == "applied" and db.get(Intake, b.id).status == "applied"
    text = app.state.mirror.path_for(db.get(Partner, p.id)).read_text(encoding="utf-8")
    assert "010-9999-8888" in text  # 지식 사본도 마지막 값


def test_append_refreshes_mirror_with_new_value(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    _apply(user_client, it)
    assert "02-555-0142" in app.state.mirror.path_for(db.get(Partner, p.id)).read_text(encoding="utf-8")


# ---- I2 중복 판정은 전화·이메일 토큰과 이름으로 ----

def test_dup_same_office_number_other_person_is_appended():
    existing = "김철수 부장 · 02-555-0142 · cs@hanbit.example"
    assert not intake_lib._already_listed(existing, _v(person="박서연", phone="02-555-0142", email="seoyeon@hanbit.example"))


def test_dup_email_must_be_whole_address():
    assert not intake_lib._already_listed("minkim@a.com", _v(person="김", email="kim@a.com"))
    assert intake_lib._already_listed("김 kim@a.com", _v(person="김", email="KIM@a.com"))


def test_dup_digits_across_phone_and_date_boundary():
    existing = "홍길동 · 02-555-0142 (명함, 2026-11-19 올리기)"
    assert not intake_lib._already_listed(existing, _v(person="새사람", phone="0142-2026"))
    assert not intake_lib._already_listed("대표 02-555-0142", _v(person="새사람", phone="55501"))


def test_dup_country_code_form_is_same_number():
    assert intake_lib._already_listed("박서연 010-1234-5678", _v(person="박서연", phone="+82 10-1234-5678"))
    assert intake_lib._already_listed("박서연 +82 10-1234-5678", _v(person="박서연", phone="010-1234-5678"))
    assert not intake_lib._already_listed("박서연 010-1234-5678", _v(person="박서연", phone="+82 10-1234-5679"))


def test_dup_needs_every_identifier_and_the_name():
    existing = "박서연 팀장 · 02-555-0142"
    assert intake_lib._already_listed(existing, _v(person="박서연", phone="02-555-0142"))
    assert not intake_lib._already_listed(existing, _v(person="박서연", phone="02-555-0142", email="new@hanbit.example"))
    assert intake_lib._already_listed(existing, _v(phone="02-555-0142"))  # 이름이 없으면 전화만 본다


def test_same_card_twice_is_skipped_but_other_person_is_appended(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    for contact in (_c(p), _c(p), _c(p, person="이민호", email="")):
        _apply(user_client, ready_bundle(db, settings, [CARD], contacts=[contact]))
    db.expire_all()
    lines = db.get(Partner, p.id).contact.split("\n")
    assert len(lines) == 2 and "박서연" in lines[0] and "이민호" in lines[1]


# ---- I3 폼의 대상이 이상하면 정리안 대상으로 돌아가지 않고 건너뛴다 ----

def test_invalid_form_target_is_skipped_and_reported(app, user_client, db, settings):
    p = Partner(name="한빛", contact="p")
    c = Customer(name="고객", contact="c")
    db.add_all([p, c])
    db.commit()
    for i, target in enumerate(["99999", "-1", "１", "abc"]):
        it = ready_bundle(db, settings, [CARD], contacts=[_c(p, phone=f"010-0000-{i:04d}", email="")])
        form = form_from_page(user_client, it)
        form["ct1__target"] = [target]
        _apply(user_client, it, form)
        db.expire_all()
        assert db.get(Partner, p.id).contact == "p" and db.get(Customer, c.id).contact == "c", target
        assert db.get(Intake, it.id).result[0]["type"] == "skipped", target


def test_absent_or_empty_target_uses_plan_target(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    form = form_from_page(user_client, it)
    del form["ct1__target"]
    _apply(user_client, it, form)
    db.expire_all()
    assert "02-555-0142" in db.get(Partner, p.id).contact


def test_invalid_solution_target_is_skipped(app, user_client, db, settings):
    from app.models import Solution

    s = Solution(name="솔루션", stage="boxed")
    db.add(s)
    db.commit()
    it = ready_bundle(db, settings, [("자료.txt", b"x")], additions=[{"solution_id": s.id, "files": [1], "reason": "r"}])
    form = form_from_page(user_client, it)
    key = next(k for k in form if k.endswith("__solution"))
    form[key] = ["99999"]
    _apply(user_client, it, form)
    db.expire_all()
    assert db.get(Intake, it.id).result[0]["type"] == "skipped"
    assert not db.get(Solution, s.id).files


# ---- M1 길이 제한과 보이지 않는 제어 문자 ----

def test_contact_fields_are_capped_and_invisible_marks_removed(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p, note="N" * 5000, person="P" * 500, phone="1" * 300)])
    item = it.plan["items"][0]
    assert len(item["note"]) == 60 and len(item["person"]) == 100 and len(item["phone"]) == 100
    form = form_from_page(user_client, it)
    form["ct1__address"] = ["A" * 100000 + "​‮"]
    form["ct1__role"] = ["R" * 1000]
    _apply(user_client, it, form)
    db.expire_all()
    contact = db.get(Partner, p.id).contact
    assert len(contact) < 1000 and "A" * 200 in contact and "A" * 201 not in contact and "R" * 100 in contact and "R" * 101 not in contact


def test_flat_removes_zero_width_and_bidi():
    assert intake_lib._flat("a​b‮c⁦d﻿e‏ f\ng") == "abcde f g"


# ---- M2 이름이 바뀐 대상 ----

def test_renamed_target_is_skipped_with_changed_reason_and_warned(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    form = form_from_page(user_client, it)
    p.name = "완전 다른 이름"
    db.commit()
    page = user_client.get(f"/intake/{it.id}").text
    card = next(x for x in re.findall(r'<section class="card plan-item".*?</section>', page, re.S) if 'id="item-ct1"' in x)
    assert "이름이 바뀌었습니다" in card and "다시 고르세요" in card and "(대상을 고르세요)" in card
    assert 'value="' + str(p.id) + '" selected' not in card
    _apply(user_client, it, form)
    db.expire_all()
    assert db.get(Partner, p.id).contact == ""
    reason = db.get(Intake, it.id).result[0]["reason"]
    assert "대상이 바뀌었거나 삭제되어" in reason


def test_renamed_target_can_be_confirmed_by_the_user(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    p.name = "한빛 주식회사"
    db.commit()
    form = form_from_page(user_client, it)
    assert form["ct1__target"] == [""]  # 고르기 전에는 비어 있다
    form["ct1__target"] = [str(p.id)]
    form["ct1__target_ok"] = ["on"]
    _apply(user_client, it, form)
    db.expire_all()
    assert "02-555-0142" in db.get(Partner, p.id).contact


def test_unchanged_target_has_no_warning(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    assert "이름이 바뀌었습니다" not in user_client.get(f"/intake/{it.id}").text


# ---- M3 대상이 저장 도중 사라져도 500 이 아니다 ----

def test_target_deleted_during_apply_is_skipped(app, user_client, db, settings, monkeypatch):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    form = form_from_page(user_client, it)
    pid = p.id
    orig = intake_lib._Run.existing_id

    def hooked(self, model, value):
        found = orig(self, model, value)
        if model is Partner and found == pid:
            with app.state.session_factory() as other:
                other.delete(other.get(Partner, pid))
                other.commit()
        return found

    monkeypatch.setattr(intake_lib._Run, "existing_id", hooked)
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert db.get(Intake, it.id).status == "applied" and db.get(Intake, it.id).result[0]["type"] == "skipped"


# ---- M4 줄바꿈 맞춤, M5 목록은 첫 줄만 ----

def test_contact_textarea_newlines_normalised(user_client, db):
    r = user_client.post("/partners", data={"name": "가", "contact": "a\r\nb\r\n"}, follow_redirects=False)
    assert r.status_code == 303
    r2 = user_client.post("/customers", data={"name": "나", "contact": "a\r\nb"}, follow_redirects=False)
    assert r2.status_code == 303
    db.expire_all()
    assert db.query(Partner).filter_by(name="가").one().contact == "a\nb"
    assert db.query(Customer).filter_by(name="나").one().contact == "a\nb"
    p = db.query(Partner).filter_by(name="가").one()
    user_client.post(f"/partners/{p.id}", data={"name": "가", "contact": "x\r\ny"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Partner, p.id).contact == "x\ny"


def test_list_pages_show_first_contact_line_only(user_client, db):
    p = Partner(name="가", contact="첫째 줄 02-1\n둘째 줄\n셋째 줄")
    c = Customer(name="나", contact="고객 첫줄\n고객 둘째")
    db.add_all([p, c])
    db.commit()
    page = user_client.get("/partners").text
    assert "첫째 줄 02-1" in page and "둘째 줄" not in page and "외 2줄" in page
    page = user_client.get("/customers").text
    assert "고객 첫줄" in page and "고객 둘째" not in page and "외 1줄" in page


# ---- M6 메뉴 배지: 따로 연 짧은 세션 ----

def _ready(db, name="kim"):
    user = db.query(User).filter_by(username=name).one()
    it = Intake(note="", status="ready", created_by_id=user.id)
    db.add(it)
    db.commit()


def test_nav_badge_uses_own_session_and_survives_errors(app, user_client, db, monkeypatch):
    _ready(db)
    page = user_client.get("/solutions").text
    assert re.search(r'<span class="nav-badge"[^>]*>1</span>', page)
    assert not hasattr(app.state, "db_ref")
    assert "db_ref" not in (Path(intake_lib.__file__).parent / "security.py").read_text(encoding="utf-8")
    assert "db_ref" not in (Path(intake_lib.__file__).parent / "db.py").read_text(encoding="utf-8")

    def boom():
        raise RuntimeError("no db")

    from app.main import _intake_ready_count
    assert _intake_ready_count(boom, db.query(User).filter_by(username="kim").one()) == 0


def test_nav_badge_no_query_for_anonymous(app, user_client, db):
    _ready(db)
    from fastapi.testclient import TestClient
    seen = []

    @event.listens_for(app.state.engine, "before_cursor_execute")
    def grab(conn, cursor, statement, params, context, executemany):
        if "intakes" in statement:
            seen.append(statement)

    try:
        assert TestClient(app).get("/login").status_code == 200
    finally:
        event.remove(app.state.engine, "before_cursor_execute", grab)
    assert seen == []


# ---- I4 구성안 확인 동시성 ----

def test_concurrent_checks_of_two_items_are_both_stored(app, user_client, db, settings, monkeypatch):
    from app.routers import proposal_outline as po

    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[2]["needs_check"] = ["값 하나", "값 둘"]
    slides[2]["checked"] = []
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    user = db.query(User).filter_by(username="kim").one()
    orig = po._load
    barrier = threading.Barrier(2)

    def hooked(db_, proposal_id):
        result = orig(db_, proposal_id)
        try:
            barrier.wait(1.0)  # 둘 다 읽은 뒤에 쓰게 만든다 (잠금을 먼저 잡는 구현이면 한쪽이 기다리다 넘어간다)
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(po, "_load", hooked)
    errors = []

    def run(i, text):
        with app.state.session_factory() as s:
            try:
                po.check_item(p.id, 2, item=str(i), text=text, done="1", user=s.get(User, user.id), db=s)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    threads = [threading.Thread(target=run, args=(0, "값 하나")), threading.Thread(target=run, args=(1, "값 둘"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    db.expire_all()
    assert sorted(db.get(Proposal, p.id).outline["slides"][2]["checked"]) == ["값 둘", "값 하나"]


def test_app_js_serialises_confirm_requests_and_refinds_card():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "checkQueue" in js and "document.getElementById(card.id)" in js
    assert "if (!sel) return" in js  # kind-select 가 없어도 오류가 나지 않는다


def test_app_js_summary_shows_placeholder_when_cleared():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "data-empty" in js or "dataset.empty" in js


# ---- M7~M12 화면 문구·접근성 ----

def test_summaries_have_context_for_screen_readers(app, user_client, db, settings):
    p = Partner(name="한빛", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [CARD], contacts=[_c(p)])
    page = user_client.get(f"/intake/{it.id}").text
    assert "<summary>고치기</summary>" not in page
    assert 'aria-label="' in page and "고치기" in page
    assert re.search(r'<summary aria-label="[^"]*고치기">', page)


def test_unplaced_card_shows_read_status_next_to_name(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("빈파일.txt", b"x"), ("b.txt", b"y")], contacts=[])
    page = user_client.get(f"/intake/{it.id}").text
    card = next(x for x in re.findall(r'<section class="card plan-item".*?</section>', page, re.S) if "빈파일.txt" in x)
    li = re.search(r"<li>[^<]*빈파일\.txt.*?</li>", card, re.S).group(0)
    assert "badge" in li


def test_dup_follow_up_note_names_the_other_item(app, user_client, db, settings):
    acts = [{"draft": {"title": "1차 미팅", "follow_ups": [{"task": "견적서 재송부하기", "due": "2026-11-25"}]}, "raw_from": 1, "files": [1], "reason": "r"},
            {"draft": {"title": "2차 통화", "follow_ups": [{"task": "견적서를 재송부", "due": "2026-11-25"}]}, "raw_from": 2, "files": [2], "reason": "r"}]
    it = ready_bundle(db, settings, [("a.txt", b"1"), ("b.txt", b"2")], activities=acts)
    page = user_client.get(f"/intake/{it.id}").text
    assert "「1차 미팅」 항목에도 비슷한 할 일이 있습니다" in page


def test_outline_wording_matches_slide_badges(user_client, db, settings):
    p = outlined_proposal(db, settings)
    slides = [dict(s) for s in p.outline["slides"]]
    slides[1]["needs_check"] = ["근거 자료 없음: 값"]
    slides[3]["needs_check"] = ["a"]
    p.outline = {**p.outline, "slides": slides}
    db.commit()
    page = user_client.get(f"/proposals/{p.id}/outline").text
    assert re.search(r'class="count-evidence">근거 문제 1장', page) and re.search(r'class="count-decide">확인할 점 \d+장', page)
    assert '<span class="badge evidence">근거 문제</span>' in page and '<span class="badge decide">확인할 점</span>' in page
