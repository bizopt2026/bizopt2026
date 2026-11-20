"""UX wave F: 방향 카드 접기, 비슷한 할 일 한 번에 빼기, 연락처 메모 길이."""
import re
from datetime import date

from app import intake as intake_lib
from app.ai.intake_tasks import Contact, IntakePlanOut
from app.models import Activity, Partner
from tests.conftest import form_from_page, ready_bundle

TODAY = date(2026, 11, 19)


# ---- R3 연락처 메모 ----

def test_prompt_limits_contact_note_to_identifying_words():
    from app.ai.tasks import load_prompt
    prompt = load_prompt("intake_analyze", today="2026-11-19", note="(없음)", solutions="(없음)", customers="(없음)", partners="(없음)")
    rule = next(line for line in prompt.splitlines() if line.startswith("- `contacts`"))
    assert "사람 이름, 직책·부서, 전화, 이메일, 주소만" in rule
    assert "대표번호" in rule and "구매 담당" in rule
    assert "슬로건" in rule and "제품 설명" in rule and "회사 소개 문구" in rule


def _partner_plan(db, settings, note):
    p = Partner(name="한빛센서테크(주)", contact="")
    db.add(p)
    db.commit()
    it = ready_bundle(db, settings, [("명함.txt", b"x")], contacts=[Contact(
        target_type="partner", target_id=p.id, person="박서연", phone="02-555-0142", note=note, files=[1], reason="명함")])
    return it.plan["items"][0]


def test_clean_plan_caps_long_contact_note(app, user_client, db, settings):
    item = _partner_plan(db, settings, "스마트한 내일을 여는 " * 20)
    assert len(item["note"]) == 60 and item["note"].endswith("…")
    assert item["note"][:-1] == ("스마트한 내일을 여는 " * 20)[:59].rstrip()


def test_clean_plan_keeps_short_contact_note(app, user_client, db, settings):
    assert _partner_plan(db, settings, "대표번호")["note"] == "대표번호"
    exact = "가" * 60
    assert _partner_plan(db, settings, exact)["note"] == exact


# ---- R2 비슷한 할 일 빼기 ----

def _fu(task, due="2026-11-25"):
    return {"task": task, "owner": "", "due": due}


def _dup_bundle(db, settings, second=None):
    second = second or [_fu("견적서를 재송부"), _fu("착수 보고", "2026-12-01")]
    drafts = [{"title": "1차", "date": "2026-11-17", "follow_ups": [_fu("견적서 재송부하기")]},
              {"title": "2차", "date": "2026-11-18", "follow_ups": second}]
    acts = [{"draft": d, "raw_from": i, "files": [i], "reason": "r"} for i, d in enumerate(drafts, 1)]
    return ready_bundle(db, settings, [("m1.txt", b"a"), ("m2.txt", b"b")], activities=acts)


def _card(page, key):
    cards = re.findall(r'<section class="card plan-item".*?</section>', page, re.S)
    card = next(c for c in cards if f'id="item-{key}"' in c)
    i = card.index('<details class="plan-edit"')
    return card[:i], card[i:]


def _tasks(db):
    db.expire_all()
    return sorted(a.title + ":" + ",".join(f["task"] for f in a.follow_ups) for a in db.query(Activity))


def test_flagged_follow_up_has_checkbox_in_collapsed_summary(app, user_client, db, settings):
    it = _dup_bundle(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    head1, edit1 = _card(page, "act1")
    head2, edit2 = _card(page, "act2")
    assert 'name="act2__fu_drop" value="0"' in head2 and "이 할 일 빼기: 견적서를 재송부" in head2
    assert 'name="act2__fu_drop" value="0" checked' not in head2 and "checked" not in re.search(r'<input[^>]*fu_drop[^>]*>', head2).group(0)
    assert head2.count("fu_drop") == 1  # 착수 보고는 표시되지 않았다
    assert "fu_drop" not in head1 and "fu_drop" not in edit2


def test_unflagged_items_have_no_drop_checkbox(app, user_client, db, settings):
    it = ready_bundle(db, settings, [("m1.txt", b"a")], activities=[
        {"draft": {"title": "1차", "follow_ups": [_fu("견적서 재송부하기")]}, "raw_from": 1, "files": [1], "reason": "r"}])
    assert "fu_drop" not in user_client.get(f"/intake/{it.id}").text


def test_apply_with_drop_removes_exactly_that_follow_up(app, user_client, db, settings):
    it = _dup_bundle(db, settings)
    form = form_from_page(user_client, it)
    form["act2__fu_drop"] = ["0"]
    r = user_client.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert _tasks(db) == ["1차:견적서 재송부하기", "2차:착수 보고"]


def test_apply_without_drop_keeps_everything(app, user_client, db, settings):
    it = _dup_bundle(db, settings)
    user_client.post(f"/intake/{it.id}/apply", data=form_from_page(user_client, it), follow_redirects=False)
    assert _tasks(db) == ["1차:견적서 재송부하기", "2차:견적서를 재송부,착수 보고"]


def test_forged_drop_index_is_ignored(app, user_client, db, settings):
    it = _dup_bundle(db, settings)
    form = form_from_page(user_client, it)
    form["act2__fu_drop"] = ["1", "7", "-1", "x", ""]  # 1번은 표시되지 않은 행
    form["act1__fu_drop"] = ["0"]  # 표시된 것이 없는 항목
    user_client.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False)
    assert _tasks(db) == ["1차:견적서 재송부하기", "2차:견적서를 재송부,착수 보고"]


def test_drop_state_survives_failed_apply(app, user_client, db, settings):
    it = _dup_bundle(db, settings)
    form = form_from_page(user_client, it)
    form["act2__fu_drop"] = ["0"]
    form["act2__summary_md"], form["act2__raw_text"] = [""], [""]  # 저장할 수 없는 입력
    page = user_client.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False).text
    head2, _ = _card(page, "act2")
    assert re.search(r'<input[^>]*fu_drop[^>]*checked', head2)


# ---- R1 방향 카드: 근거·확인할 점은 5개까지만 ----

def _dir_page(user_client, db, settings, n_rationale, n_checks):
    from tests.conftest import outlined_proposal
    p = outlined_proposal(db, settings)
    rationale = [{"text": f"근거 {i}번", "sources": [f"solutions/{i}/card.md"]} for i in range(1, n_rationale + 1)]
    checks = [f"확인 항목 {i}번" for i in range(1, n_checks + 1)]
    p.directions = [dict(p.directions[0], rationale=rationale, needs_check=checks), p.directions[1]]
    p.selected_index, p.status = None, "directions"
    db.commit()
    page = user_client.get(f"/proposals/{p.id}").text
    return re.findall(r'<article class="card dir-card.*?</article>', page, re.S)[0]


def test_long_direction_card_folds_after_five(app, user_client, db, settings):
    card = _dir_page(user_client, db, settings, 8, 7)
    inner = card.split("<summary>근거와 확인할 점 보기</summary>")[1]
    assert inner.count('<details class="dir-rest">') == 2
    assert "나머지 3개 더 보기" in inner and "나머지 2개 더 보기" in inner
    rest_r = inner.split("나머지 3개 더 보기")[1].split("</details>")[0]
    assert "근거 6번" in rest_r and "근거 8번" in rest_r and "근거 5번" not in rest_r
    first_r = inner.split("나머지 3개 더 보기")[0]
    assert "근거 5번" in first_r and "근거 6번" not in first_r
    rest_c = inner.split("나머지 2개 더 보기")[1].split("</details>")[0]
    assert "확인 항목 6번" in rest_c and "확인 항목 7번" in rest_c and "확인 항목 5번" not in rest_c
    # 출처 목록은 접힌 부분 밖에 전부 있고, 접힌 근거의 윗첨자 링크가 그 번호로 간다
    assert inner.index("나머지 3개 더 보기") < inner.index('id="d0-src-8"')
    assert 'href="#d0-src-8"' in rest_r
    src_part = inner.split("<dt>출처</dt>")[1]
    assert "<details" not in src_part.split("</dl>")[0] and src_part.count('id="d0-src-') == 8


def test_short_direction_card_has_no_nested_details(app, user_client, db, settings):
    card = _dir_page(user_client, db, settings, 5, 5)
    assert card.count("<details") == 1 and "나머지" not in card
