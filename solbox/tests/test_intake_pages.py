"""올리기로 들어가는 길: 홈 올리기 카드, 목록 버튼, 직접 입력 안내, 끌어다 놓기 스크립트."""
import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "app.js"


@pytest.mark.parametrize("url,manual", [("/solutions", "/solutions/new"), ("/activities", "/activities/new")])
def test_list_primary_is_upload_and_manual_stays(user_client, url, manual):
    head = re.search(r'<div class="page-head">.*?</div>\s*(?=<form|<div)', user_client.get(url).text, re.S).group(0)
    assert 'class="button primary" href="/intake/new">올리기</a>' in head
    assert f'href="{manual}">직접 입력</a>' in head and head.count("primary") == 1
    assert user_client.get(manual).status_code == 200


@pytest.mark.parametrize("url,manual", [("/solutions", "/solutions/new"), ("/activities", "/activities/new")])
def test_empty_list_suggests_upload_first(user_client, url, manual):
    empty = re.search(r'<div class="empty">.*?</div>', user_client.get(url).text, re.S).group(0)
    assert 'href="/intake/new"' in empty and f'href="{manual}"' in empty
    assert empty.index("/intake/new") < empty.index(manual) and empty.count("primary") == 1


def test_searching_empty_list_has_no_upload_prompt(user_client):
    page = user_client.get("/solutions?q=없는이름").text
    assert "조건에 맞는 항목이 없습니다." in page and 'class="empty"' not in page


def test_activity_new_points_to_upload(user_client):
    page = user_client.get("/activities/new").text
    assert "녹취록·사진·자료 파일은 올리기에서 한 번에 정리할 수 있습니다" in page
    assert page.index('href="/intake/new"') < page.index('action="/activities/raw"')
    card = re.search(r'<form[^>]*class="card stack".*?</form>', page, re.S).group(0)
    assert card.count('class="primary"') == 1 and 'name="raw_text"' in card  # 메모 붙여 넣기 흐름 그대로


def test_intake_new_has_examples_and_plain_file_input(user_client):
    page = user_client.get("/intake/new").text
    for line in ("제품 소개서·스펙 → 솔루션", "미팅 녹취록·현장 사진 → 활동 기록", "명함·회사 소개 → 고객·파트너"):
        assert line in page
    form = re.search(r'<form[^>]*action="/intake".*?</form>', page, re.S).group(0)
    tag = re.search(r'<input[^>]*type="file"[^>]*>', form).group(0)
    assert "multiple" in tag and 'name="files"' in tag and "hidden" not in tag  # 스크립트 없이도 고를 수 있다
    assert 'enctype="multipart/form-data"' in form and form.count("primary") == 1


def test_dropzone_limits_are_exposed_to_script(user_client, settings):
    page = user_client.get("/intake/new").text
    zone = re.search(r'<div[^>]*data-dropzone[^>]*>', page).group(0)
    assert 'data-max-files="20"' in zone and f'data-max-mb="{settings.max_upload_mb}"' in zone
    home = user_client.get("/").text
    assert 'data-dropzone' in home and 'data-max-files="20"' in home


def test_script_handles_drop_and_warns():
    js = APP_JS.read_text(encoding="utf-8")
    assert "DataTransfer" in js and "dataTransfer" in js and '"drop"' in js
    assert "개 선택" in js and "합계" in js and "최대" in js  # 개수·합계·초과 안내
    assert "<script" not in js
