"""올리기 확인 화면: 접힌 요약 카드, 고정 저장 막대, 저장하지 않은 파일, 비슷한 후속 조치."""
import re
from datetime import date
from pathlib import Path

from app import intake as intake_lib
from app.ai.intake_tasks import IntakePlanOut
from app.models import Intake, Solution
from tests.conftest import form_from_page, ready_bundle, ready_intake

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"


def _cards(page: str) -> list[str]:
    return re.findall(r'<section class="card plan-item".*?</section>', page, re.S)


def _card(page: str, key: str) -> str:
    return next(c for c in _cards(page) if f'id="item-{key}"' in c)


def _split(card: str) -> tuple[str, str]:
    """카드를 (요약 부분, 고치기 부분) 으로 가른다."""
    i = card.index('<details class="plan-edit"')
    return card[:i], card[i:]


def _post(client, it, form):
    return client.post(f"/intake/{it.id}/apply", data=form, follow_redirects=False)


# ---- U1a 접힌 요약 카드 ----

def test_cards_collapsed_with_summary_and_skip_outside_details(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    assert len(_cards(page)) == 3
    for card in _cards(page):
        head, edit = _split(card)
        assert re.match(r'<details class="plan-edit"\s*>', edit) and re.search(r'<summary aria-label="[^"]+ 고치기">고치기</summary>', edit)
        assert 'name="skip_' in head and 'name="skip_' not in edit  # 제외 체크는 접어도 보인다
        assert "이렇게 정리한 이유" in head and "plan-" in head
    head, edit = _split(_card(page, "s1"))
    assert "스마트 조명 제어" in head and "재실 감지 조명" in head and "루미텍" in head and "조명" in head
    assert 'name="s1__name"' in edit and 'name="s1__name"' not in head
    head, _ = _split(_card(page, "a1"))
    assert "AI 공조 최적제어" in head and "스펙.txt" in head


def test_activity_summary_shows_key_facts(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    head, edit = _split(_card(page, "act1"))
    assert "한빛병원 통화" in head and "2026-11-17" in head and "고객 태핑" in head
    assert "후속 조치 0" in head and "수요 단서 0" in head
    assert "한빛병원" in head and "AI 공조 최적제어" in head  # 연결된 이름 칩
    assert "미등록 후보: 스마트 조명 제어" in head and "새로 등록:" not in head  # AI가 제안했지만 아직 체크하지 않은 이름
    assert "전력비 문의" in head  # 정리본 앞부분 (글자만)
    assert "<textarea" not in head and "<input" not in head.replace('name="skip_act1"', "").replace("<input type=\"checkbox\"", "")


def test_summary_lines_are_plain_text_first_three(db, settings, user_client):
    long = "# 제목\n\n## 참석자\n- 김부장\n\n## 논의 내용\n- **굵게** 첫째 줄\n- 둘째 줄 [링크](http://x)\n- 셋째 줄\n- 넷째 줄"
    it = ready_bundle(db, settings, [("a.txt", b"x")], activities=[{"draft": {"title": "t"}, "raw_from": 1, "files": [1]}])
    it.plan = {"items": [{**it.plan["items"][0], "summary_md": long}]}
    db.commit()
    cards = intake_lib.review_cards(db, it)
    lines = cards[0]["brief"]["lines"]
    assert lines == ["굵게 첫째 줄", "둘째 줄 링크", "셋째 줄"]  # 제목 줄과 참석자는 건너뛴다


def test_new_name_inputs_nested_closed(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    _, edit = _split(_card(user_client.get(f"/intake/{it.id}").text, "act1"))
    assert '<details class="plan-newnames"' in edit
    nested = edit[edit.index('<details class="plan-newnames"'):]
    assert re.match(r'<details class="plan-newnames"\s*>', nested) and "새 이름 직접 추가" in nested
    assert 'name="act1__new_solutions" placeholder' in nested
    assert nested.index("</details>") > nested.index('name="act1__new_partners"')  # 세 칸 모두 안에 있다
    # AI가 제안한 이름의 체크 칸은 고치기 안에 그대로 있다
    assert 'name="act1__new_solutions" value="스마트 조명 제어"> 스마트' in edit  # 체크 전


def test_collapsed_page_posts_every_field(app, user_client, db, settings):
    """접힌 <details> 안의 칸도 제출된다: 화면의 모든 칸 이름이 고치기 안에 있고, 그대로 보내면 저장된다."""
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    for card in _cards(page):
        head, edit = _split(card)
        assert not re.search(r'<(input|textarea|select)[^>]*name="(?!skip_)', head)
    form = form_from_page(user_client, it)
    assert form["act1__title"] == ["한빛병원 통화"] and form["s1__name"] == ["스마트 조명 제어"]
    assert _post(user_client, it, form).status_code == 303
    db.expire_all()
    assert it.status == "applied" and len(it.result) == 4


def test_failed_apply_opens_only_cards_with_errors_or_edits(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["act1__summary_md"], form["act1__raw_text"] = [""], [""]  # 저장할 수 없는 입력
    form["s1__summary"] = ["내가 고친 소개"]  # 고친 칸
    form["act1__new_solutions"] = ["스마트 조명 제어", "직접 쓴 이름"]  # 체크한 새 이름과 직접 추가한 이름
    r = _post(user_client, it, form)
    assert r.status_code == 400
    page = r.text
    assert re.search(r'<details class="plan-edit"\s+open', _card(page, "act1"))
    assert re.search(r'<details class="plan-edit"\s+open', _card(page, "s1"))
    assert "새로 등록: 스마트 조명 제어" in _card(page, "act1") and "새로 등록: 직접 쓴 이름" in _card(page, "act1")
    assert 'class="plan-edit" open' in _card(page, "s1") and "내가 고친 소개" in _card(page, "s1")
    assert re.search(r'<details class="plan-edit"\s*>', _card(page, "a1"))  # 건드리지 않은 카드는 접힌 채


def test_toggle_all_buttons_and_script(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    box = re.search(r'<span class="toggle-all[^"]*"[^>]*>.*?</span>', page, re.S).group(0)
    assert 'data-target="plan-cards"' in box and "모두 펼치기" in box and "모두 접기" in box and "hidden" in box
    assert 'id="plan-cards"' in page
    js = APP_JS.read_text(encoding="utf-8")
    assert "details.plan-edit" in js and "data-sum" in js


# ---- U1b 고정 저장 막대 ----

def test_savebar_counts_and_placement(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    page = user_client.get(f"/intake/{it.id}").text
    bar = re.search(r'<div class="row savebar plan-savebar".*?</div>\s*</form>', page, re.S).group(0)
    assert "이대로 저장 (3건)" in bar and "3건 저장" in bar and "제외" not in bar.replace("제외하", "")
    assert 'class="primary"' in bar and "다시 정리" not in bar and "버리기" not in bar
    assert page.index("plan-savebar") > page.index('id="item-act1"') and page.index("다시 정리") > page.index("plan-savebar")
    assert 'data-total="3"' in bar


def test_savebar_counts_excluded_on_rerender(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    form["skip_a1"] = ["on"]
    form["act1__summary_md"], form["act1__raw_text"] = [""], [""]
    page = _post(user_client, it, form).text
    bar = re.search(r'<div class="row savebar plan-savebar".*?</div>\s*</form>', page, re.S).group(0)
    assert "이대로 저장 (2건)" in bar and "2건 저장 · 1건 제외" in bar


def test_script_updates_counts_live():
    js = APP_JS.read_text(encoding="utf-8")
    assert "plan-savebar" in js and "건 제외" in js and "이대로 저장 (" in js


# ---- U2 저장하지 않은 파일 ----

def _bundle_with_unplaced(db, settings):
    files = [("소개서.txt", "소개".encode()), ("녹취.txt", "통화 내용".encode()), ("메모.txt", "짧음".encode())]
    return ready_bundle(
        db, settings, files,
        solutions=[{"name": "신규 조명", "summary": "s", "files": [1], "reason": "소개서"}],
        activities=[{"draft": {"title": "통화"}, "raw_from": 2, "files": [2], "reason": "통화"}],
        unplaced=[{"files": [3], "reason": "내용이 너무 짧아요"}],
    )


def test_applied_page_lists_unsaved_files(app, user_client, db, settings):
    it = _bundle_with_unplaced(db, settings)
    form = form_from_page(user_client, it)
    form["skip_s1"] = ["on"]  # 솔루션 항목을 제외했다
    assert _post(user_client, it, form).status_code == 303
    db.expire_all()
    unsaved = [e for e in it.result if e["type"] == "unsaved"]
    assert {e["label"] for e in unsaved} == {"소개서.txt", "메모.txt"}
    assert not (settings.intake_work_dir / str(it.id)).exists()  # 작업 폴더에 기대지 않는다
    page = user_client.get(f"/intake/{it.id}").text
    assert "저장하지 않은 파일" in page
    sec = page[page.index("저장하지 않은 파일"):]
    assert "소개서.txt" in sec and "메모.txt" in sec and "내용이 너무 짧아요" in sec and "제외" in sec
    assert "녹취.txt" not in sec  # 저장된 파일은 없다
    for f in it.files:
        if f.original_name in ("소개서.txt", "메모.txt"):
            assert f'href="/intake/files/{f.id}/download"' in sec
    assert 'href="/solutions/new"' in sec and 'href="/activities/new"' in sec
    assert "저장한 기록" in page and "통화" in page


def test_applied_without_unsaved_has_no_section(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    _post(user_client, it, form_from_page(user_client, it))
    assert "저장하지 않은 파일" not in user_client.get(f"/intake/{it.id}").text


def test_old_applied_bundle_without_unsaved_data_renders(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    it.status, it.result = "applied", [{"type": "solution", "id": s1.id, "label": "AI 공조 최적제어"},
                                       {"type": "skipped", "id": None, "label": "솔루션에 추가: 대상 솔루션이 삭제되어 자료를 추가하지 못했습니다."}]
    db.commit()
    page = user_client.get(f"/intake/{it.id}").text
    assert page.count("AI 공조 최적제어") >= 1 and "저장하지 못한 항목" in page and "저장하지 않은 파일" not in page


def test_skipped_item_keeps_reason_and_files(app, user_client, db, settings):
    it, s1, c1 = ready_intake(db, settings)
    form = form_from_page(user_client, it)
    db.delete(s1)
    db.commit()
    _post(user_client, it, form)
    db.expire_all()
    skipped = [e for e in it.result if e["type"] == "skipped"][0]
    assert "삭제" in skipped["reason"] and [f["name"] for f in skipped["files"]] == ["스펙.txt"]
    page = user_client.get(f"/intake/{it.id}").text
    assert "저장하지 못한 항목" in page and "스펙.txt" in page


# ---- U3 비슷한 후속 조치 ----

def _acts(db, settings, *drafts):
    files = [(f"m{i}.txt", f"내용 {i}".encode()) for i in range(1, len(drafts) + 1)]
    acts = [{"draft": d, "raw_from": i, "files": [i], "reason": "r"} for i, d in enumerate(drafts, 1)]
    return ready_bundle(db, settings, files, activities=acts)


def _fu(task, due="2026-11-25", owner=""):
    return {"task": task, "owner": owner, "due": due}


def test_same_task_different_wording_is_flagged(db, settings, user_client):
    it = _acts(db, settings,
               {"title": "1차", "date": "2026-11-17", "follow_ups": [_fu("견적서 재송부하기"), _fu("현장 방문", "2026-12-01")]},
               {"title": "2차", "date": "2026-11-18", "follow_ups": [_fu("견적서를 재송부"), _fu("착수 보고", "2026-12-01")]})
    first, second = it.plan["items"]
    assert "dup_follow_ups" not in first
    assert second["dup_follow_ups"] == [{"row": 0, "task": "견적서를 재송부", "of": first["key"]}]
    assert [f["task"] for f in second["draft"]["follow_ups"]] == ["견적서를 재송부", "착수 보고"]  # 지우지 않는다


def test_identical_normalized_text_is_flagged_even_with_different_due(db, settings, user_client):
    it = _acts(db, settings,
               {"title": "1차", "follow_ups": [_fu("  계약서 검토! ", "2026-11-25")]},
               {"title": "2차", "follow_ups": [_fu("계약서  검토", "2026-12-30")]})
    assert it.plan["items"][1]["dup_follow_ups"][0]["task"] == "계약서  검토"


def test_different_tasks_same_date_not_flagged(db, settings, user_client):
    it = _acts(db, settings,
               {"title": "1차", "follow_ups": [_fu("견적서 재송부"), _fu("회의록 정리")]},
               {"title": "2차", "follow_ups": [_fu("현장 사전 점검"), _fu("계약서 초안 작성")]})
    assert all("dup_follow_ups" not in i for i in it.plan["items"])


def test_similar_text_different_due_not_flagged(db, settings, user_client):
    it = _acts(db, settings,
               {"title": "1차", "follow_ups": [_fu("견적서 재송부하기", "2026-11-25")]},
               {"title": "2차", "follow_ups": [_fu("견적서를 재송부", "2026-12-25")]})
    assert all("dup_follow_ups" not in i for i in it.plan["items"])


def test_same_activity_not_compared_with_itself(db, settings, user_client):
    it = _acts(db, settings, {"title": "1차", "follow_ups": [_fu("견적서 재송부"), _fu("견적서 재송부")]})
    assert "dup_follow_ups" not in it.plan["items"][0]


def test_review_card_shows_dup_note_and_summary(app, user_client, db, settings):
    it = _acts(db, settings,
               {"title": "1차", "follow_ups": [_fu("견적서 재송부하기")]},
               {"title": "2차", "follow_ups": [_fu("견적서를 재송부"), _fu("착수 보고", "2026-12-01")]})
    page = user_client.get(f"/intake/{it.id}").text
    head1, edit1 = _split(_card(page, "act1"))
    head2, edit2 = _split(_card(page, "act2"))
    assert "비슷한 할 일" not in head1 and "항목에도 비슷한 할 일이 있습니다" not in edit1
    assert "비슷한 할 일 1건 확인" in head2 and "후속 조치 2" in head2
    assert edit2.count("「1차」 항목에도 비슷한 할 일이 있습니다") == 1
    assert 'name="act2__fu_task" value="견적서를 재송부"' in edit2  # 저절로 지우지 않는다
    # 칸을 비우고 다시 보내면 안내가 사라진다
    form = form_from_page(user_client, it)
    form["act2__fu_task"] = ["", "착수 보고"]
    form["act2__summary_md"], form["act2__raw_text"] = [""], [""]
    page = _post(user_client, it, form).text
    head2, edit2 = _split(_card(page, "act2"))
    assert "비슷한 할 일" not in head2 and "항목에도 비슷한" not in edit2


def test_prompt_dedupes_follow_ups_across_files():
    from app.ai.tasks import load_prompt
    prompt = load_prompt("intake_analyze", today="2026-11-19", note="(없음)", solutions="(없음)", customers="(없음)", partners="(없음)")
    rule = next(line for line in prompt.splitlines() if "같은 할 일" in line)
    assert "활동 기록 한 곳" in rule and "가장 최근" in rule and "반복하지" in rule
    assert "`reason` 은 화면에 그대로 보이므로" in prompt  # 항목 이름 금지 규칙은 그대로
