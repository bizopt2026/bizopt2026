import dataclasses
import io
import re
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.intake import file_kind
from app.main import create_app
from app.models import Intake
from tests.conftest import make_user

_LOGIN = {"username": "lee", "password": "pw1234"}


def _png_bytes(size=(40, 30), mode="RGB", fmt="PNG", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def _upload(client, files, note="", **kw):
    return client.post("/intake", data={"note": note}, files=[("files", f) for f in files], follow_redirects=False, **kw)


def test_upload_creates_intake_and_workdir(user_client, db, settings):
    files = [("files", ("소개서.txt", "AI 공조 솔루션 소개".encode(), "text/plain")),
             ("files", ("녹취.txt", "한빛병원 통화 내용".encode(), "text/plain"))]
    r = user_client.post("/intake", data={"note": "신규 솔루션 후보"}, files=files, follow_redirects=False)
    it = db.query(Intake).one()
    assert r.headers["location"] == f"/intake/{it.id}" and it.status == "analyzing" and it.note == "신규 솔루션 후보"
    assert [(f.original_name, f.kind, f.read_status) for f in it.files] == [("소개서.txt", "document", "ok"), ("녹취.txt", "document", "ok")]
    work = settings.intake_work_dir / str(it.id)
    assert (work / "1-소개서.txt.md").read_text(encoding="utf-8").startswith("# 소개서.txt")
    assert "녹취.txt" in (work / "파일목록.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name,kind", [("a.PPTX", "document"), ("사진.JPG", "image"), ("회의.m4a", "audio"), ("x.zip", "other"), ("noext", "other")])
def test_file_kind(name, kind):
    assert file_kind(name) == kind


def test_image_is_downscaled_and_oriented(user_client, db, settings):
    img = Image.new("RGB", (4000, 3000), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # 시계 방향 90도 회전 표시
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    _upload(user_client, [("큰사진.jpg", buf.getvalue(), "image/jpeg")])
    it = db.query(Intake).one()
    f = it.files[0]
    assert (f.kind, f.read_status) == ("image", "ok")
    copy = settings.intake_work_dir / str(it.id) / "1-큰사진.jpg.jpg"
    with Image.open(copy) as out:
        assert out.size == (1500, 2000)  # 방향이 바로잡혀 세로가 길고, 긴 변이 2000


def test_transparent_png_keeps_png(user_client, db, settings):
    _upload(user_client, [("로고.png", _png_bytes(mode="RGBA", color=(1, 2, 3, 0)), "image/png")])
    it = db.query(Intake).one()
    assert it.files[0].read_status == "ok"
    assert (settings.intake_work_dir / str(it.id) / "1-로고.png.png").is_file()


def test_broken_image_marked_failed_and_others_continue(user_client, db, settings):
    r = _upload(user_client, [("x.png", b"not an image", "image/png"), ("메모.txt", b"hello", "text/plain")])
    assert r.status_code == 303
    it = db.query(Intake).one()
    assert [(f.original_name, f.read_status) for f in it.files] == [("x.png", "failed"), ("메모.txt", "ok")]
    assert not (settings.intake_work_dir / str(it.id) / "1-x.png.jpg").exists()
    assert (settings.intake_work_dir / str(it.id) / "2-메모.txt.md").is_file()


def test_decompression_bomb_is_failed(user_client, db, settings, monkeypatch):
    from app import intake as intake_lib

    monkeypatch.setattr(intake_lib, "MAX_IMAGE_PIXELS", 1000)  # 40x30 = 1200 > 1000
    _upload(user_client, [("폭탄.png", _png_bytes(), "image/png")])
    assert db.query(Intake).one().files[0].read_status == "failed"


def test_audio_kept_with_needs_transcript(user_client, db, settings):
    r = _upload(user_client, [("회의.m4a", b"\x00\x01fake", "audio/mp4")])
    it = db.query(Intake).one()
    f = it.files[0]
    assert (f.kind, f.read_status) == ("audio", "needs_transcript")
    assert (settings.uploads_dir / "intake" / str(it.id) / f.stored_name).read_bytes() == b"\x00\x01fake"
    assert not any(p.name != "파일목록.md" for p in (settings.intake_work_dir / str(it.id)).iterdir())
    page = user_client.get(r.headers["location"]).text
    assert "받아쓰기 필요 · 글자로 풀어 올려 주세요" in page


def test_other_kind_is_unsupported_but_kept(user_client, db, settings):
    _upload(user_client, [("x.zip", b"PK", "application/zip")])
    f = db.query(Intake).one().files[0]
    assert (f.kind, f.read_status) == ("other", "unsupported")
    assert (settings.uploads_dir / "intake" / str(f.intake_id) / f.stored_name).is_file()


def test_limits(user_client, db, settings, app):
    files = [(f"f{i:02d}.txt", f"내용 {i}".encode(), "text/plain") for i in range(1, 22)]
    r = _upload(user_client, files)
    assert r.status_code == 200 and "f21.txt" in r.text  # 받지 못한 파일 안내가 상세 화면에 보인다
    it = db.query(Intake).one()
    assert len(it.files) == 20 and "f21.txt" not in [f.original_name for f in it.files]

    r = _upload(user_client, [("빈.txt", b"", "text/plain"), ("ok.txt", b"x", "text/plain")])
    assert r.status_code == 200 and "빈.txt" in r.text and "빈 파일" in r.text
    assert [f.original_name for f in db.query(Intake).order_by(Intake.id).all()[-1].files] == ["ok.txt"]

    tiny = create_app(dataclasses.replace(settings, max_upload_mb=0))
    try:
        c = TestClient(tiny)
        c.post("/login", data={"username": "kim", "password": "pw1234"})  # 같은 data_dir 이라 같은 DB
        before = db.query(Intake).count()
        r = c.post("/intake", data={"note": ""}, files=[("files", ("a.txt", b"abc", "text/plain"))], follow_redirects=False)
        assert r.status_code == 400 and "올릴 파일을 선택하세요." in r.text
        db.expire_all()
        assert db.query(Intake).count() == before
        assert not any((settings.uploads_dir / "intake" / str(n)).exists() for n in (before + 1,))
    finally:
        tiny.state.engine.dispose()


def test_same_names_and_special_chars(user_client, db, settings):
    weird = '제안서: "최종"?.txt'
    _upload(user_client, [("a.txt", b"one", "text/plain"), ("a.txt", b"two", "text/plain"), (weird, b"three", "text/plain")])
    it = db.query(Intake).one()
    assert len({f.stored_name for f in it.files}) == 3
    originals = [(settings.uploads_dir / "intake" / str(it.id) / f.stored_name).read_bytes() for f in it.files]
    assert originals == [b"one", b"two", b"three"]
    work = settings.intake_work_dir / str(it.id)
    assert len([p for p in work.iterdir() if p.name.endswith(".md") and p.name != "파일목록.md"]) == 3
    r = user_client.get(f"/intake/files/{it.files[2].id}/download")
    assert r.content == b"three"
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd and unquote(cd.split("filename*=UTF-8''")[1]) == weird
    assert re.search(r'filename="[\x20-\x7e]+"', cd)  # ASCII 대체 이름


def test_stored_name_uses_lowercase_extension_only(user_client, db):
    _upload(user_client, [("Big.TXT", b"x", "text/plain"), ("weird.t$t", b"y", "text/plain")])
    names = [f.stored_name for f in db.query(Intake).one().files]
    assert [n.split(".", 1)[1] if "." in n else "" for n in names] == ["txt", ""]


def test_only_owner_and_admin_can_see(user_client, admin_client, app, db):
    _upload(user_client, [("a.txt", b"x", "text/plain")])
    it = db.query(Intake).one()
    fid = it.files[0].id
    make_user(db, "lee")
    other = TestClient(app)
    other.post("/login", data=_LOGIN)
    for c, code in ((user_client, 200), (admin_client, 200), (other, 403)):
        assert c.get(f"/intake/{it.id}").status_code == code
        assert c.get(f"/intake/files/{fid}/download").status_code == code
    assert f"/intake/{it.id}" in user_client.get("/intake").text
    assert f"/intake/{it.id}" in admin_client.get("/intake").text
    assert f"/intake/{it.id}" not in other.get("/intake").text
    assert other.get("/intake/9999").status_code == 404


def test_list_filters_by_status_and_paginates(user_client, db):
    for _ in range(3):
        _upload(user_client, [("a.txt", b"x", "text/plain")])
    first = db.query(Intake).order_by(Intake.id).first()
    first.status = "applied"
    db.commit()
    page = user_client.get("/intake?status=applied").text
    assert f'href="/intake/{first.id}"' in page and len(re.findall(r'href="/intake/\d+"', page)) == 1
    page = user_client.get("/intake").text
    assert page.index(f'href="/intake/{first.id + 2}"') < page.index(f'href="/intake/{first.id}"')  # 최신순


def test_no_files_rejected(user_client, db):
    r = user_client.post("/intake", data={"note": "메모만"}, follow_redirects=False)
    assert r.status_code == 400 and "올릴 파일을 선택하세요." in r.text and "메모만" in r.text
    r = user_client.post("/intake", data={"note": ""}, files=[("files", ("", b"", "application/octet-stream"))], follow_redirects=False)
    assert r.status_code == 400
    assert db.query(Intake).count() == 0


def test_note_limit(user_client, db):
    r = _upload(user_client, [("a.txt", b"x", "text/plain")], note="가" * 501)
    assert r.status_code == 400 and db.query(Intake).count() == 0
    r = _upload(user_client, [("a.txt", b"x", "text/plain")], note="가" * 500)
    assert r.status_code == 303


def test_new_page_has_multiple_file_input(user_client):
    html = user_client.get("/intake/new").text
    assert re.search(r'<input[^>]*type="file"[^>]*name="files"[^>]*multiple', html) or re.search(r'<input[^>]*name="files"[^>]*multiple', html)
    assert 'name="note"' in html and "올리기" in html


def test_nav_has_upload_first(user_client, admin_client):
    for c in (user_client, admin_client):
        nav = re.search(r"<nav>(.*?)</nav>", c.get("/").text, re.S).group(1)
        first = re.search(r"<a [^>]*>([^<]*)</a>", nav)
        assert first.group(1) == "올리기"
    html = user_client.get("/intake/new").text
    assert re.search(r'<a href="/intake" aria-current="page">올리기</a>', html)


def test_intake_cascades_files(user_client, db):
    from app.models import IntakeFile

    _upload(user_client, [("a.txt", b"x", "text/plain")])
    db.delete(db.query(Intake).one())
    db.commit()
    assert db.query(IntakeFile).count() == 0
