import io
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import intake as intake_lib
from app import records
from app.models import Activity, Customer, Intake, Partner, Solution, SolutionFile, User
from tests.conftest import form_from_page, make_user, ready_bundle, ready_intake


def _apply(client, it, form):
    return client.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False)


def _login(app, db, username):
    make_user(db, username)
    c = TestClient(app)
    assert c.post("/login", data={"username": username, "password": "pw1234"}, follow_redirects=False).status_code == 303
    return c


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def _new_solution(db):
    return db.query(Solution).filter_by(name="스마트 조명 제어").one()


def test_apply_creates_everything(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    r = _apply(user_client, it, form_from_page(user_client, it))
    assert r.status_code == 303 and r.headers["location"] == f"/intake/{it.id}"
    db.expire_all()
    assert it.status == "applied" and it.applied_at is not None and len(it.result) == 4  # 파트너(루미텍) + 솔루션 + 자료 추가 + 활동 기록
    assert [e["type"] for e in it.result] == ["partner", "solution", "solution", "activity"]
    new = _new_solution(db)
    assert new.stage == "intake" and new.partner.name == "루미텍" and [f.original_name for f in new.files] == ["소개서.txt"]
    assert new.summary == "재실 감지 조명" and new.tags == "조명" and new.files[0].kind == "intro"  # 이름에서 짐작한 자료 종류
    assert (settings.knowledge_dir / "solutions" / str(new.id) / "card.md").exists()
    assert (settings.uploads_dir / "solutions" / str(new.id) / new.files[0].stored_name).read_text(encoding="utf-8") == "스마트 조명 소개"
    mirror = app.state.mirror
    assert "스마트 조명 소개" in mirror.path_for(new.files[0]).read_text(encoding="utf-8")
    assert len(s1.files) == 1 and s1.files[0].original_name == "스펙.txt" and s1.files[0].kind == "spec"
    a = db.query(Activity).one()
    assert a.customers == [c1] and s1 in a.solutions and new in a.solutions and a.raw_text.endswith("통화 내용")
    assert a.title == "한빛병원 통화" and a.kind == "tapping" and a.created_by_id == it.created_by_id
    assert [f.target_type for f in it.files] == ["solution", "solution", "activity"]
    assert [f.target_id for f in it.files] == [new.id, s1.id, a.id]
    assert not (settings.intake_work_dir / str(it.id)).exists()
    assert (settings.uploads_dir / "intake" / str(it.id)).exists()  # 원본은 남는다
    assert len(list((settings.uploads_dir / "intake" / str(it.id)).iterdir())) == 3
    assert mirror.path_for(a).exists() and mirror.path_for(new.partner).exists()
    assert "한빛병원 통화" in mirror.path_for(c1).read_text(encoding="utf-8")
    assert "스펙.txt" in mirror.path_for(s1).read_text(encoding="utf-8")
    assert "루미텍" in mirror.path_for(new).read_text(encoding="utf-8")
    assert "스마트 조명 제어" in mirror.path_for(new.partner).read_text(encoding="utf-8")
    page = user_client.get(f"/intake/{it.id}").text  # 저장 완료 화면: 만들어진 기록 링크
    for href in (f"/solutions/{new.id}", f"/partners/{new.partner.id}", f"/activities/{a.id}", f"/solutions/{s1.id}"):
        assert f'href="{href}"' in page
    assert "저장 완료" in page and f'action="/intake/{it.id}/apply"' not in page


def test_apply_twice_creates_once(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    assert _apply(user_client, it, form).headers["location"] == f"/intake/{it.id}"
    r = _apply(user_client, it, form)
    assert r.status_code == 303 and r.headers["location"] == f"/intake/{it.id}?notice=not_ready"
    assert "이미 저장했거나 저장할 수 없는 상태입니다." in user_client.get(r.headers["location"]).text
    assert db.query(Solution).filter_by(name="스마트 조명 제어").count() == 1 and db.query(Activity).count() == 1
    assert db.query(Partner).count() == 1 and db.query(SolutionFile).count() == 2


def test_apply_concurrent_creates_once(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    n = 6
    clients = [_login_again(app) for _ in range(n)]
    barrier = threading.Barrier(n)
    codes, errors = [], []

    def go(c):
        try:
            barrier.wait(timeout=20)
            codes.append(_apply(c, it, form).status_code)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors and codes == [303] * n
    db.expire_all()
    assert it.status == "applied" and len(it.result) == 4
    assert db.query(Solution).filter_by(name="스마트 조명 제어").count() == 1 and db.query(Partner).count() == 1
    assert db.query(Activity).count() == 1 and db.query(SolutionFile).count() == 2
    assert len(list((settings.uploads_dir / "solutions").rglob("*.txt"))) == 2


def _login_again(app):
    c = TestClient(app)
    assert c.post("/login", data={"username": "kim", "password": "pw1234"}, follow_redirects=False).status_code == 303
    return c


def test_apply_failure_rolls_back(app, user_client, db, settings, monkeypatch):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    real = records.apply_activity_fields

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(records, "apply_activity_fields", boom)
    r = _apply(user_client, it, form)
    assert r.status_code == 500 and "저장하지 못했습니다" in r.text
    db.expire_all()
    assert it.status == "ready" and "저장하지 못했습니다" in it.error and it.result == [] and it.applied_at is None
    assert db.query(Solution).filter_by(name="스마트 조명 제어").count() == 0 and db.query(Partner).count() == 0
    assert db.query(SolutionFile).count() == 0 and db.query(Activity).count() == 0
    assert not any(p.is_file() for p in (settings.uploads_dir / "solutions").rglob("*"))  # 복사한 파일을 지웠다
    assert [f.target_type for f in it.files] == ["", "", ""]
    assert (settings.intake_work_dir / str(it.id)).exists() and not list(settings.knowledge_dir.rglob("스마트*"))
    assert "저장하지 못했습니다" in user_client.get(f"/intake/{it.id}").text  # 다시 확인 화면에 오류가 보인다
    monkeypatch.setattr(records, "apply_activity_fields", real)
    r = _apply(user_client, it, form)
    assert r.status_code == 303
    db.expire_all()
    assert it.status == "applied" and it.error == "" and db.query(Activity).count() == 1 and len(it.result) == 4


def test_skipped_items_not_created(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["skip_s1"] = ["on"]
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert db.query(Solution).filter_by(name="스마트 조명 제어").count() == 0 and db.query(Partner).count() == 0
    # 제외한 항목의 파일(소개서.txt)은 "저장하지 않은 파일" 로 결과 끝에 남는다
    assert [e["type"] for e in it.result] == ["solution", "activity", "unsaved"] and it.result[-1]["label"] == "소개서.txt"
    assert len(s1.files) == 1
    assert [f.target_type for f in it.files] == ["", "solution", "activity"]
    assert db.query(Activity).one().solutions == [s1]


def test_user_edits_are_used(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form.update({
        "s1__name": ["스마트 조명 제어 Pro"], "s1__summary": ["고친 소개"], "s1__description": ["고친 설명"],
        "s1__tags": ["조명, 센서 ,"], "s1__partner_new": ["다른회사"], "s1__kind_1": ["spec"],
        "act1__title": ["고친 제목"], "act1__kind": ["meeting"], "act1__date": ["2026-11-18"],
        "act1__summary_md": ["## 고친 정리"], "act1__fu_task": ["견적 보내기"], "act1__fu_owner": ["김"], "act1__fu_due": ["2026-12-01"],
        "act1__ds_customer": ["한빛병원"], "act1__ds_content": ["조명 교체 문의"],
    })
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    new = db.query(Solution).filter_by(name="스마트 조명 제어 Pro").one()
    assert (new.summary, new.description, new.tags, new.files[0].kind) == ("고친 소개", "고친 설명", "조명,센서", "spec")
    assert new.partner.name == "다른회사" and db.query(Partner).filter_by(name="루미텍").count() == 0
    a = db.query(Activity).one()
    assert (a.title, a.kind, a.date.isoformat(), a.summary_md) == ("고친 제목", "meeting", "2026-11-18", "## 고친 정리")
    assert a.follow_ups == [{"task": "견적 보내기", "owner": "김", "due": "2026-12-01", "done": False}]
    assert a.demand_signals == [{"customer": "한빛병원", "content": "조명 교체 문의"}]


def test_add_target_can_be_changed_to_existing_solution(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    s2 = Solution(name="다른 솔루션")
    db.add(s2)
    db.commit()
    form = form_from_page(user_client, it)
    assert form["a1__solution"] == [str(s1.id)]
    form["a1__solution"] = [str(s2.id)]
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert len(s2.files) == 1 and len(s1.files) == 0
    assert [f.target_id for f in it.files][1] == s2.id


def test_forged_form_cannot_change_structure(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form.update({"s9__name": ["가짜"], "zz__kind": ["x"], "skip_zz": ["on"], "s1__files": ["1,2,3"], "s1__kind_99": ["spec"],
                 "s1__partner": ["99999"], "s1__kind_1": ["bogus"], "a1__kind_2": ["bogus"],
                 "act1__solution_ids": [str(s1.id), "99999", "x"]})
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    new = _new_solution(db)
    assert len(it.result) == 4 and db.query(Solution).count() == 2 and db.query(Activity).count() == 1
    assert [f.original_name for f in new.files] == ["소개서.txt"] and new.files[0].kind == "intro" and new.partner.name == "루미텍"
    assert len(s1.files) == 1 and s1.files[0].kind == "spec"  # 잘못된 값은 기본값으로
    assert db.query(Activity).one().solutions == [s1, new]


def test_deleted_target_is_skipped_and_reported(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    db.delete(s1)
    db.commit()
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert it.status == "applied"
    assert [e["type"] for e in it.result] == ["partner", "solution", "activity", "skipped"]
    assert it.result[-1]["id"] is None and "삭제" in it.result[-1]["label"]
    assert db.query(SolutionFile).count() == 1 and [f.target_type for f in it.files] == ["solution", "", "activity"]
    assert db.query(Activity).one().solutions == [_new_solution(db)]
    page = user_client.get(f"/intake/{it.id}").text
    assert "저장하지 못한 항목" in page and "삭제" in page


def test_existing_names_are_linked_not_duplicated(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    other = Solution(name=" 스마트 조명-제어 ", created_by_id=None)  # 그 사이 같은 이름이 등록되었다
    p = Partner(name="루미 텍")
    db.add_all([other, p])
    db.commit()
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert db.query(Solution).count() == 2 and db.query(Partner).count() == 1
    assert [f.original_name for f in other.files] == ["소개서.txt"] and other.name == " 스마트 조명-제어 " and other.partner_id is None
    assert [e["type"] for e in it.result] == ["solution", "solution", "activity"]
    assert "이미 등록" in it.result[0]["label"]
    assert db.query(Activity).one().solutions == [s1, other]


def test_customer_and_partner_items(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("명함.txt", "연락처".encode())],
                      customers=[{"name": "새고객", "industry": "병원", "contact": "02-1", "memo": "m", "files": [1], "reason": "r"}],
                      partners=[{"name": "새파트너", "contact": "031", "description": "d", "files": [1], "reason": "r2"}])
    assert [i["key"] for i in it.plan["items"]] == ["p1", "c1"]
    form = form_from_page(user_client, it)
    assert form["p1__name"] == ["새파트너"] and form["c1__industry"] == ["병원"]
    form["c1__memo"] = ["고친 메모"]
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert db.query(Partner).one().contact == "031" and db.query(Customer).one().memo == "고친 메모"
    assert [e["type"] for e in it.result] == ["partner", "customer"]
    assert (settings.knowledge_dir / "customers" / f"{db.query(Customer).one().id}.md").exists()


def test_empty_name_rerenders_without_creating(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["s1__name"] = [""]
    form["act1__title"] = ["입력 유지 제목"]
    form["s1__summary"] = ["유지할 소개"]
    r = _apply(user_client, it, form)
    assert r.status_code == 400 and "이름을 입력하세요" in r.text
    assert "입력 유지 제목" in r.text and "유지할 소개" in r.text
    db.expire_all()
    assert it.status == "ready" and db.query(Solution).count() == 1 and db.query(Partner).count() == 0 and db.query(Activity).count() == 0
    assert db.query(SolutionFile).count() == 0
    form["s1__name"] = ["스마트 조명 제어"]
    assert _apply(user_client, it, form).status_code == 303  # 고쳐서 다시 저장할 수 있다


def test_empty_activity_and_blank_party_names_are_rejected(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["act1__raw_text"] = ["  "]
    form["act1__summary_md"] = [""]
    r = _apply(user_client, it, form)
    assert r.status_code == 400 and "메모" in r.text
    db.expire_all()
    assert db.query(Activity).count() == 0 and db.query(Partner).count() == 0 and it.status == "ready"
    form["skip_act1"] = ["on"]  # 제외한 항목은 검사하지 않는다
    assert _apply(user_client, it, form).status_code == 303


def test_nothing_selected(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    for key in ("s1", "a1", "act1"):
        form[f"skip_{key}"] = ["on"]
    r = _apply(user_client, it, form)
    assert r.status_code == 400 and "저장할 항목이 없습니다." in r.text
    db.expire_all()
    assert it.status == "ready" and db.query(Activity).count() == 0
    with pytest.raises(ValueError, match="저장할 항목이 없습니다."):
        intake_lib.apply(db, settings, app.state.mirror, it, db.query(User).filter_by(username="kim").one(),
                         {"skip_s1": "on", "skip_a1": "on", "skip_act1": "on"})
    db.expire_all()
    assert it.status == "ready"


def test_apply_requires_ready(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    it.status = "failed"
    db.commit()
    r = _apply(user_client, it, form)
    assert r.status_code == 303 and r.headers["location"].endswith("?notice=not_ready")
    assert db.query(Activity).count() == 0 and db.query(Partner).count() == 0


def test_discard_keeps_originals(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    r = user_client.post(f"/intake/{it.id}/discard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/intake/{it.id}"
    db.expire_all()
    assert it.status == "discarded"
    assert not (settings.intake_work_dir / str(it.id)).exists()
    assert len(list((settings.uploads_dir / "intake" / str(it.id)).iterdir())) == 3
    assert "버린 묶음" in user_client.get(f"/intake/{it.id}").text
    r = _apply(user_client, it, form)  # 버린 묶음은 저장할 수 없다
    assert r.status_code == 303 and db.query(Activity).count() == 0
    assert user_client.post(f"/intake/{it.id}/discard", follow_redirects=False).status_code == 303  # 다시 눌러도 그대로


def test_applied_cannot_be_discarded(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    _apply(user_client, it, form_from_page(user_client, it))
    r = user_client.post(f"/intake/{it.id}/discard", follow_redirects=False)
    assert r.status_code == 400
    db.expire_all()
    assert it.status == "applied"


def test_only_owner_or_admin(app, user_client, admin_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    lee = _login(app, db, "lee")
    assert lee.get(f"/intake/{it.id}").status_code == 403
    assert lee.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False).status_code == 403
    assert lee.post(f"/intake/{it.id}/discard", follow_redirects=False).status_code == 403
    db.expire_all()
    assert it.status == "ready" and db.query(Activity).count() == 0
    assert admin_client.get(f"/intake/{it.id}").status_code == 200
    assert _apply(admin_client, it, form).status_code == 303
    db.expire_all()
    assert it.status == "applied"


def test_activity_detail_lists_original_files_and_download_permission(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    _apply(user_client, it, form_from_page(user_client, it))
    db.expire_all()
    a = db.query(Activity).one()
    lee = _login(app, db, "lee")
    page = lee.get(f"/activities/{a.id}").text
    assert "원본 파일" in page and "녹취.txt" in page and f'href="/intake/files/{it.files[2].id}/download"' in page
    assert "소개서.txt" not in page
    r = lee.get(f"/intake/files/{it.files[2].id}/download")
    assert r.status_code == 200 and r.content.decode("utf-8") == "한빛병원 통화 내용"
    assert lee.get(f"/intake/files/{it.files[0].id}/download").status_code == 403  # 기록에 연결되지 않은 파일
    assert lee.get(f"/intake/files/{it.files[1].id}/download").status_code == 403
    assert user_client.get(f"/intake/files/{it.files[0].id}/download").status_code == 200
    assert user_client.post(f"/activities/{a.id}/delete", follow_redirects=False).status_code == 303  # 기록이 지워지면 다시 주인·관리자만
    db.expire_all()
    assert [f.target_type for f in it.files][2] == ""
    assert lee.get(f"/intake/files/{it.files[2].id}/download").status_code == 403
    manual = Activity(title="직접 쓴 기록", raw_text="메모")
    db.add(manual)
    db.commit()
    assert "원본 파일" not in lee.get(f"/activities/{manual.id}").text


def test_image_attached_to_solution_kept_as_unsupported(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("사진.png", _png()), ("소개.txt", "카메라 소개".encode())],
                      solutions=[{"name": "카메라 솔루션", "files": [1, 2], "reason": "사진과 소개"}])
    assert it.plan["items"][0]["files"] == [1, 2]
    assert _apply(user_client, it, form_from_page(user_client, it)).status_code == 303
    db.expire_all()
    s = db.query(Solution).filter_by(name="카메라 솔루션").one()
    by_name = {f.original_name: f for f in s.files}
    assert by_name["사진.png"].extract_status == "unsupported" and by_name["소개.txt"].extract_status == "ok"
    assert (settings.uploads_dir / "solutions" / str(s.id) / by_name["사진.png"].stored_name).read_bytes() == _png()
    mirror = app.state.mirror
    assert not mirror.path_for(by_name["사진.png"]).exists() and mirror.path_for(by_name["소개.txt"]).exists()
    assert (settings.uploads_dir / "intake" / str(it.id) / it.files[0].stored_name).read_bytes() == _png()  # 원본 유지


def test_review_page_shows_cards_and_fields(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    for text in ("새 솔루션", "솔루션에 자료 추가", "활동 기록", "이렇게 정리한 이유", "제품 소개서", "기존 솔루션 스펙", "통화 녹취록",
                 "소개서.txt", "스펙.txt", "녹취.txt", "AI 공조 최적제어", "원문"):
        assert text in page, text
    for name in ("s1__name", "s1__summary", "s1__description", "s1__tags", "s1__partner", "s1__partner_new", "s1__kind_1", "skip_s1",
                 "a1__solution", "a1__kind_2", "skip_a1", "act1__kind", "act1__date", "act1__title", "act1__summary_md", "act1__raw_text",
                 "act1__fu_task", "act1__ds_content", "act1__solution_ids", "act1__customer_ids", "skip_act1"):
        assert f'name="{name}"' in page, name
    assert f'action="/intake/{it.id}/discard"' in page and page.count('class="primary"') == 1


def test_review_page_unplaced_guidance(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("a.txt", "가".encode()), ("b.txt", "나".encode())], solutions=[{"name": "솔", "files": [1]}])
    page = user_client.get(f"/intake/{it.id}").text
    assert "정리하지 못한 파일" in page and 'href="/solutions/new"' in page and 'href="/activities/new"' in page
    assert 'name="skip_u1"' not in page
    only_unplaced = ready_bundle(db, settings, [("c.txt", "다".encode())])
    r = _apply(user_client, only_unplaced, form_from_page_or_empty(user_client, only_unplaced))
    assert r.status_code == 400 and "저장할 항목이 없습니다." in r.text


def form_from_page_or_empty(client, it):
    try:
        return form_from_page(client, it)
    except AssertionError:
        return {}


def test_escaping_of_ai_text_on_review_page(app, user_client, db, settings):
    evil = "<script>alert(1)</script>"
    it = ready_bundle(
        db, settings, [("a.txt", "가".encode()), ("b.txt", "나".encode())],
        solutions=[{"name": evil, "summary": evil, "reason": evil, "files": [1]}],
        activities=[{"draft": {"title": '"><b>굵게</b>', "discussion": [evil]}, "raw_from": 2, "files": [2], "reason": evil}],
    )
    page = user_client.get(f"/intake/{it.id}").text
    assert evil not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<b>굵게</b>" not in page
    assert _apply(user_client, it, form_from_page(user_client, it)).status_code == 303
    db.expire_all()
    assert db.query(Solution).filter_by(name=evil).count() == 1  # 저장은 글 그대로, 화면에서 이스케이프된다
    assert evil not in user_client.get(f"/intake/{it.id}").text


def test_recover_applying(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    it.status = "applying"
    db.commit()
    assert intake_lib.recover_applying(db) == 1
    db.refresh(it)
    assert it.status == "ready" and intake_lib.recover_applying(db) == 0


# ---- 리뷰 보완 ----

def test_activity_detail_title_and_original_section_once(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    _apply(user_client, it, form_from_page(user_client, it))
    a = db.query(Activity).one()
    page = user_client.get(f"/activities/{a.id}").text
    title = page[page.index("<title>"):page.index("</title>")]
    assert "<section" not in title and page.count("원본 파일</h2>") == 1


def test_odd_digit_characters_do_not_crash(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["act1__solution_ids"] = ["²", str(s1.id)]
    form["a1__solution"] = ["²"]
    r = _apply(user_client, it, form)
    assert r.status_code == 303
    r = user_client.get(f"/intake/{it.id}")
    assert r.status_code == 200


def test_stuck_applying_can_be_rechecked(app, user_client, db, settings):
    from datetime import datetime, timedelta

    it, s1, c1 = ready_intake(db, settings)
    it.status = "applying"
    db.commit()
    page = user_client.get(f"/intake/{it.id}").text
    assert "다시 확인" not in page  # 방금 시작한 저장은 기다린다
    assert user_client.post(f"/intake/{it.id}/recheck", follow_redirects=False).status_code == 303
    db.expire_all()
    assert it.status == "applying"
    db.execute(Intake.__table__.update().where(Intake.id == it.id).values(updated_at=datetime.now() - timedelta(minutes=30)))
    db.commit()
    assert "다시 확인" in user_client.get(f"/intake/{it.id}").text
    assert user_client.post(f"/intake/{it.id}/recheck", follow_redirects=False).status_code == 303
    db.expire_all()
    assert it.status == "ready"


def test_release_is_retried_once(app, user_client, db, settings, monkeypatch):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    monkeypatch.setattr(records, "apply_activity_fields", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    real = intake_lib._release_once
    calls = []

    def flaky(db_, intake_id, token, message):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("locked")
        return real(db_, intake_id, token, message)

    monkeypatch.setattr(intake_lib, "_release_once", flaky)
    assert _apply(user_client, it, form).status_code == 500
    db.expire_all()
    assert len(calls) == 2 and it.status == "ready"


def test_file_used_by_solution_and_activity_targets_activity(app, user_client, db, settings):
    it = ready_bundle(
        db, settings, [("통화.txt", "고객 통화 내용".encode())],
        solutions=[{"name": "새 솔", "files": [1]}],
        activities=[{"draft": {"title": "통화"}, "raw_from": 1, "files": [1]}],
    )
    _apply(user_client, it, form_from_page(user_client, it))
    db.expire_all()
    a = db.query(Activity).one()
    assert (it.files[0].target_type, it.files[0].target_id) == ("activity", a.id)
    assert db.query(SolutionFile).count() == 1  # 솔루션에는 자기 사본이 있다


def test_addition_target_reused_id_is_skipped(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    assert it.plan["items"][1]["solution_name"] == s1.name
    form = form_from_page(user_client, it)
    s1.name = "전혀 다른 솔루션"  # 번호가 다른 솔루션을 가리키게 된 경우
    db.commit()
    del form["a1__solution"]  # 사용자가 고르지 않았다
    assert _apply(user_client, it, form).status_code == 303
    db.expire_all()
    assert "skipped" in [e["type"] for e in it.result] and len(s1.files) == 0
    it2, s2, _ = ready_intake(db, settings)
    form = form_from_page(user_client, it2)
    s2.name = "또 다른 이름"
    other = Solution(name="고른 솔루션")
    db.add(other)
    db.commit()
    form["a1__solution"] = [str(other.id)]  # 직접 고른 대상은 그대로 쓴다
    _apply(user_client, it2, form)
    db.expire_all()
    assert len(other.files) == 1


def test_empty_title_falls_back_to_summary_line(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("a.txt", "가".encode())], activities=[{"draft": {"title": "x"}, "files": [1]}])
    form = form_from_page(user_client, it)
    form["act1__title"] = [""]
    form["act1__raw_text"] = [""]
    form["act1__summary_md"] = ["\n첫 줄 요약\n둘째"]
    _apply(user_client, it, form)
    assert db.query(Activity).one().title == "첫 줄 요약"


def test_minimal_plan_items_render(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    it.plan = {"items": [{"kind": k, "key": f"{k[:3]}{i}"} for i, k in enumerate(
        ["new_solution", "add_to_solution", "activity", "new_customer", "new_partner", "unplaced"])]}
    db.commit()
    r = user_client.get(f"/intake/{it.id}")
    assert r.status_code == 200
