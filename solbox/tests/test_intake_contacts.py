"""올리기 `add_contact`: 이미 등록된 파트너·고객의 연락처 칸에 한 줄을 덧붙인다 (덮어쓰지 않는다)."""
import re
from datetime import date

from app import intake as intake_lib
from app.ai import intake_tasks
from app.ai.intake_tasks import Contact, IntakePlanOut, NewCustomer, NewPartner
from app.models import Customer, Intake, IntakeFile, Partner
from tests.conftest import form_from_page, ready_bundle

TODAY = date(2026, 11, 19)
CARD = ("명함.txt", "박서연 팀장 02-555-0142 seoyeon@hanbit.example".encode())


def _world(db, contact="대표전화 02-111-2222"):
    p = Partner(name="한빛센서테크(주)", contact=contact, memo="메모 그대로")
    c = Customer(name="한빛병원", contact="")
    db.add_all([p, c])
    db.commit()
    return p, c


def _contact(p, **kw):
    base = dict(target_type="partner", target_id=p.id, person="박서연", role="팀장", phone="02-555-0142",
                email="seoyeon@hanbit.example", files=[1], reason="명함")
    return Contact(**{**base, **kw})


def _plan(db, settings, p, **kw):
    it = ready_bundle(db, settings, [CARD], contacts=[_contact(p, **kw)] if kw is not None else [])
    return it


def _apply(client, it, form=None):
    return client.post(f"/intake/{it.id}/apply", data=form or form_from_page(client, it), follow_redirects=False)


def _clean(db, settings, files=1, **plan):
    from tests.conftest import ready_bundle as rb
    it = rb(db, settings, [(f"f{i}.txt", b"x") for i in range(files)])
    return it, intake_lib.clean_plan(db, it, IntakePlanOut(**plan), TODAY, settings)


# ---- 모델 ----

def test_contact_model_is_lenient():
    c = Contact.model_validate({"target_type": 5, "target_id": "7", "phone": 25550142, "person": None, "files": ["1", "x", 2], "email": ["a"]})
    assert (c.target_type, c.target_id, c.phone, c.person, c.files, c.email) == ("5", 7, "25550142", "", [1, 2], "")
    out = IntakePlanOut.model_validate({"contacts": [{"target_type": "partner", "target_id": 1, "phone": "1"}, "bad", 3, {"target_id": {}}]})
    assert len(out.contacts) == 2  # 이상한 항목만 버리고 분석은 계속한다
    assert IntakePlanOut.model_validate({"contacts": "x"}).contacts == []


# ---- 정리안 ----

def test_clean_plan_builds_add_contact_with_target_name(app, user_client, db, settings):
    p, c = _world(db)
    it, plan = _clean(db, settings, contacts=[_contact(p), Contact(target_type="customer", target_id=c.id, phone="031-1", files=[1])])
    assert [i["kind"] for i in plan["items"]] == ["add_contact", "add_contact"]
    a, b = plan["items"]
    assert a["key"] == "ct1" and b["key"] == "ct2"
    assert (a["target_type"], a["target_id"], a["target_name"]) == ("partner", p.id, "한빛센서테크(주)")
    assert (a["person"], a["role"], a["phone"], a["email"]) == ("박서연", "팀장", "02-555-0142", "seoyeon@hanbit.example")
    assert b["target_name"] == "한빛병원" and a["files"] == [1]


def test_clean_plan_drops_bad_targets_and_empty_contacts(app, user_client, db, settings):
    p, c = _world(db)
    it, plan = _clean(
        db, settings,
        contacts=[_contact(p, target_id=999), _contact(p, target_type="solution"), _contact(p, target_type="customer", target_id=p.id + 100),
                  _contact(p, person="", phone="", email="", address="", role="팀장", note="메모"), _contact(p, target_id=None)])
    assert [i["kind"] for i in plan["items"]] == ["unplaced"]  # 파일은 정리하지 못한 파일로 남는다


def test_clean_plan_merges_same_contact_and_keeps_two_people(app, user_client, db, settings):
    p, _ = _world(db)
    it, plan = _clean(db, settings, files=2, contacts=[_contact(p, files=[1]), _contact(p, files=[2], reason="다른 사진"),
                                                         _contact(p, person="이도윤", phone="02-555-0199", email="", files=[2])])
    items = plan["items"]
    assert [i["kind"] for i in items] == ["add_contact", "add_contact"]
    assert items[0]["files"] == [1, 2] and items[1]["person"] == "이도윤"


def test_existing_partner_with_contact_becomes_add_contact(app, user_client, db, settings):
    p, c = _world(db)
    it, plan = _clean(
        db, settings, files=2,
        partners=[NewPartner(name="한빛 센서테크(주)", contact="박서연 팀장 02-555-0142 / seoyeon@hanbit.example", files=[1], reason="명함")],
        customers=[NewCustomer(name="한빛병원", contact="031-123-4567", files=[2]), NewCustomer(name="한빛병원", files=[2])])
    assert [i["kind"] for i in plan["items"]] == ["add_contact", "add_contact"]
    a, b = plan["items"]
    assert (a["target_type"], a["target_id"], a["phone"], a["email"]) == ("partner", p.id, "02-555-0142", "seoyeon@hanbit.example")
    assert "박서연 팀장" in a["note"] and "02-555" not in a["note"] and "@" not in a["note"]
    assert (b["target_type"], b["target_id"], b["phone"]) == ("customer", c.id, "031-123-4567")


def test_existing_partner_without_contact_is_still_dropped(app, user_client, db, settings):
    _world(db)
    it, plan = _clean(db, settings, partners=[NewPartner(name="한빛센서테크(주)", description="소개", files=[1])])
    assert [i["kind"] for i in plan["items"]] == ["unplaced"]


def test_item_order_contact_after_additions(app, user_client, db, settings):
    p, _ = _world(db)
    it, plan = _clean(db, settings, partners=[NewPartner(name="새회사", files=[1])], contacts=[_contact(p)])
    assert [i["key"] for i in plan["items"]] == ["p1", "ct1"]


# ---- 확인 화면 ----

def test_review_card_summary_and_form(app, user_client, db, settings):
    p, c = _world(db)
    it = _plan(db, settings, p)
    page = user_client.get(f"/intake/{it.id}").text
    card = next(x for x in re.findall(r'<section class="card plan-item".*?</section>', page, re.S) if 'id="item-ct1"' in x)
    head, edit = card[:card.index('<details class="plan-edit"')], card[card.index('<details class="plan-edit"'):]
    assert "연락처 추가" in head and "한빛센서테크(주)" in head
    assert "박서연 팀장 · 02-555-0142 · seoyeon@hanbit.example (" in head and "올리기)" in head
    assert 'name="skip_ct1"' in head
    for field in ("person", "role", "phone", "email", "address", "note"):
        assert f'name="ct1__{field}"' in edit
    assert 'name="ct1__target"' in edit
    options = re.findall(r'<option value="(\d+)"', edit.split('name="ct1__target"')[1].split("</select>")[0])
    assert options == [str(p.id)]  # 같은 종류의 등록된 기록만 (고객 한빛병원은 없다)
    assert "대표전화 02-111-2222" not in head  # 기존 내용을 고치는 칸은 없다


def test_review_form_default_roundtrips_to_apply(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    assert form["ct1__target"] == [str(p.id)] and form["ct1__phone"] == ["02-555-0142"]


# ---- 저장 ----

def test_apply_appends_one_line_and_never_overwrites(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    r = _apply(user_client, it)
    assert r.status_code == 303 and r.headers["location"] == f"/intake/{it.id}"
    db.expire_all()
    p = db.get(Partner, p.id)
    lines = p.contact.split("\n")
    assert lines[0] == "대표전화 02-111-2222" and len(lines) == 2  # 기존 글은 그대로, 한 줄만 더해진다
    assert re.fullmatch(r"박서연 팀장 · 02-555-0142 · seoyeon@hanbit\.example \(.+, \d{4}-\d{2}-\d{2} 올리기\)", lines[1])
    assert p.memo == "메모 그대로" and p.description == ""
    assert it.status == "applied" and it.result == [{"type": "partner", "id": p.id, "label": "한빛센서테크(주) (연락처 추가)"}]
    assert (it.files[0].target_type, it.files[0].target_id) == ("partner", p.id)
    assert lines[1] in app.state.mirror.path_for(p).read_text(encoding="utf-8")  # 지식 사본도 갱신
    page = user_client.get(f"/intake/{it.id}").text
    assert f'href="/partners/{p.id}"' in page


def test_apply_to_empty_contact_has_no_blank_line(app, user_client, db, settings):
    p, c = _world(db)
    it = ready_bundle(db, settings, [CARD], contacts=[Contact(target_type="customer", target_id=c.id, phone="031-1", files=[1])])
    _apply(user_client, it)
    db.expire_all()
    assert db.get(Customer, c.id).contact.startswith("031-1 (") and "\n" not in db.get(Customer, c.id).contact
    assert it.result[0]["type"] == "customer"


def test_apply_skips_same_phone_or_email(app, user_client, db, settings):
    p, _ = _world(db, contact="박서연 025550142\n기타")
    it = _plan(db, settings, p, email="")
    _apply(user_client, it)
    db.expire_all()
    assert db.get(Partner, p.id).contact == "박서연 025550142\n기타"
    assert it.status == "applied"  # 한 항목뿐이어도 묶음은 끝난다 (결과에는 건너뜀 표시)
    assert it.result[0]["type"] == "skipped" and "이미 있는 연락처" in it.result[0]["reason"]
    assert "이미 있는 연락처" in user_client.get(f"/intake/{it.id}").text
    p2 = Partner(name="둘째", contact="박서연 메일 SeoYeon@Hanbit.example")
    db.add(p2)
    db.commit()
    it2 = ready_bundle(db, settings, [CARD], contacts=[_contact(p2, phone="")])
    _apply(user_client, it2)
    db.expire_all()
    assert db.get(Partner, p2.id).contact == "박서연 메일 SeoYeon@Hanbit.example"


def test_same_card_twice_adds_once(app, user_client, db, settings):
    p, _ = _world(db)
    for _ in range(2):
        _apply(user_client, _plan(db, settings, p))
    db.expire_all()
    assert db.get(Partner, p.id).contact.count("02-555-0142") == 1


def test_apply_deleted_target_is_skipped(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    db.delete(p)
    db.commit()
    r = _apply(user_client, it, form)
    assert r.status_code in (303, 400)
    db.expire_all()
    if it.status == "applied":
        assert it.result[0]["type"] == "skipped" and "삭제" in it.result[0]["reason"]


def test_apply_target_id_reused_by_other_record_is_skipped(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    pid = p.id
    db.delete(p)
    db.commit()
    other = Partner(name="전혀 다른 회사", contact="원래 글")
    other.id = pid
    db.add(other)
    db.commit()
    _apply(user_client, it, form)
    db.expire_all()
    assert db.get(Partner, pid).contact == "원래 글"
    assert it.result[0]["type"] == "skipped"


def test_form_target_must_be_existing_record_of_right_type(app, user_client, db, settings):
    p, c = _world(db)
    p2 = Partner(name="둘째 파트너", contact="")
    db.add(p2)
    db.commit()
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    form["ct1__target"] = [str(p2.id)]  # 같은 종류의 다른 등록 파트너는 고를 수 있다
    _apply(user_client, it, form)
    db.expire_all()
    assert "02-555-0142" in db.get(Partner, p2.id).contact and db.get(Partner, p.id).contact == "대표전화 02-111-2222"
    assert db.get(Customer, c.id).contact == ""

    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    form["ct1__target"] = [str(c.id + 50)]  # 없는 번호
    form["ct1__phone"] = ["031-999-0000"]
    _apply(user_client, it, form)
    db.expire_all()
    assert it.result[0]["type"] == "skipped"  # 직접 고른 값이 없는 번호면 정리안의 대상으로 돌아가지 않고 건너뛴다
    assert db.get(Customer, c.id).contact == "" and "031-999-0000" not in db.get(Partner, p.id).contact


def test_form_cannot_change_target_type(app, user_client, db, settings):
    p, c = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    form["ct1__target"] = [str(c.id)]
    form["ct1__target_type"] = ["customer"]  # 종류는 정리안에서만 온다
    _apply(user_client, it, form)
    db.expire_all()
    assert db.get(Customer, c.id).contact == ""  # 고객 번호가 파트너 번호와 같아도 파트너 칸에만 쓴다
    assert it.result[0]["type"] in ("partner", "skipped")


def test_apply_requires_some_contact_field(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    for f in ("person", "phone", "email", "address"):
        form[f"ct1__{f}"] = [""]
    r = _apply(user_client, it, form)
    assert r.status_code == 400 and "연락처 추가" in r.text and it.status == "ready"


def test_newlines_in_fields_stay_one_line(app, user_client, db, settings):
    p, _ = _world(db)
    it = _plan(db, settings, p)
    form = form_from_page(user_client, it)
    form["ct1__note"] = ["첫줄\n둘째줄\t끝"]
    _apply(user_client, it, form)
    db.expire_all()
    assert len(db.get(Partner, p.id).contact.split("\n")) == 2


def test_partner_and_customer_contact_forms_are_multiline(user_client, db):
    p, c = _world(db)
    assert '<textarea name="contact"' in user_client.get(f"/partners/{p.id}/edit").text
    assert '<textarea name="contact"' in user_client.get(f"/customers/{c.id}/edit").text


# ---- 프롬프트 ----

def test_prompt_has_contact_rule_and_registered_ids(app, user_client, db, settings, fake_claude):
    p, c = _world(db)
    it = ready_bundle(db, settings, [CARD])
    it.status, it.plan = "analyzing", None
    from app.models import AIJob
    job = AIJob(kind="intake_analyze", input={"intake_id": it.id}, requester_id=it.created_by_id, priority=0, status="pending")
    db.add(job)
    db.flush()
    it.job_id = job.id
    db.commit()
    from app.ai.queue import process_next
    fake_claude.set(mode="ok", result="{}")
    process_next(app.state.session_factory, app.state.runner, settings)
    stdin = fake_claude.calls()[0]["stdin"]
    assert f"id {p.id}: 한빛센서테크(주)" in stdin and f"id {c.id}: 한빛병원" in stdin
    assert "contacts" in stdin and "target_type" in stdin
    assert "중복" in stdin and "고치거나 지우" in stdin
    assert "unplaced" in stdin


def test_prompt_file_text():
    from pathlib import Path
    text = (Path(intake_tasks.__file__).parent / "prompts" / "intake_analyze.md").read_text(encoding="utf-8")
    assert "`contacts`" in text and "새로 만들지 마세요" in text
