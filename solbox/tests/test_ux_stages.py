"""발굴 단계 배지와 상세 화면의 진행 막대."""
import re

from app.models import Partner, Solution


def _make(db, stage, name="S"):
    s = Solution(name=name, stage=stage, created_by_id=1)
    db.add(s)
    db.commit()
    return s


def test_list_shows_a_badge_per_stage(user_client, db):
    for i, stage in enumerate(("intake", "tapping", "survey", "boxed", "hold")):
        _make(db, stage, f"S{i}")
    html = user_client.get("/solutions").text
    for stage, name in (("intake", "아이템 접수"), ("tapping", "사전 태핑"), ("survey", "수요조사"), ("boxed", "박스 등록"), ("hold", "보류")):
        assert f'<span class="badge stage-{stage}">{name}</span>' in html


def test_home_stage_counts_use_badges(user_client, db):
    _make(db, "survey")
    html = user_client.get("/").text
    assert 'class="badge stage-survey"' in html and 'class="badge stage-hold"' in html


def test_partner_detail_lists_solution_stage(user_client, db):
    p = Partner(name="P")
    db.add(p)
    db.flush()
    s = _make(db, "tapping")
    s.partner_id = p.id
    db.commit()
    assert 'class="badge stage-tapping"' in user_client.get(f"/partners/{p.id}").text


def test_detail_stage_bar_marks_current_and_past(user_client, db):
    s = _make(db, "survey")
    html = user_client.get(f"/solutions/{s.id}").text
    bar = re.search(r'<form[^>]*class="stagebar".*?</form>', html, re.S).group(0)
    assert bar.count('aria-current="step"') == 1
    assert re.search(r'class="step current"[^>]*aria-current="step"[^>]*value="survey"|value="survey"[^>]*aria-current="step"', bar)
    assert bar.count("done") == 2  # 접수, 태핑
    assert 'value="hold"' in bar
    assert '<p class="sr-only">현재 단계: <strong>수요조사</strong></p>' in html


def test_hold_has_no_current_step_in_the_bar(user_client, db):
    s = _make(db, "hold")
    bar = re.search(r'<form[^>]*class="stagebar".*?</form>', user_client.get(f"/solutions/{s.id}").text, re.S).group(0)
    assert bar.count('aria-current="step"') == 1 and bar.count("done") == 0
    assert re.search(r'class="step hold current"', bar)


def test_extra_solution_choices_show_stage_badge(user_client, db):
    _make(db, "survey")
    from app.models import Customer
    db.add(Customer(name="c"))
    db.commit()
    assert 'class="badge stage-survey"' in user_client.get("/proposals/new").text


def test_current_step_is_disabled_and_same_stage_post_writes_nothing(user_client, db, settings):
    s = _make(db, "survey")
    html = user_client.get(f"/solutions/{s.id}").text
    assert re.search(r'value="survey"[^>]*aria-current="step" disabled', html)
    assert html.count("disabled") == 1
    card = settings.knowledge_dir / "solutions" / str(s.id) / "card.md"
    assert not card.exists()
    r = user_client.post(f"/solutions/{s.id}/stage", data={"stage": "survey"}, follow_redirects=False)
    assert r.status_code == 303 and not card.exists()  # 쓰기·사본 갱신 없음
    user_client.post(f"/solutions/{s.id}/stage", data={"stage": "boxed"})
    assert card.exists()


def test_past_steps_carry_a_check_prefix(user_client, db):
    s = _make(db, "survey")
    html = user_client.get(f"/solutions/{s.id}").text
    assert ">✓ 아이템 접수<" in html and ">✓ 사전 태핑<" in html and ">✓ 수요조사<" not in html
