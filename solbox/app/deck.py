"""구성안(dict) → .pptx. 템플릿 없이 빈 레이아웃 위에 고정 디자인으로 직접 그린다.

입력은 `proposals.clean_outline` / `slide_from_form` 이 만드는 형태이지만, 키가 빠져 있어도 견딘다.
"""

import copy
import math
import re
import unicodedata
from datetime import date
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.util import Emu, Inches, Pt

FONT = "맑은 고딕"
TEXT = RGBColor(0x22, 0x22, 0x22)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
LIGHT = RGBColor(0xEA, 0xF1, 0xF8)
STRIPE = RGBColor(0xF5, 0xF8, 0xFB)
MUTED = RGBColor(0x66, 0x66, 0x66)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LINE = RGBColor(0xBF, 0xCD, 0xDB)

# --- 좌표 (인치) ---
SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.6
CONTENT_W = SLIDE_W - 2 * MARGIN
TITLE_Y, TITLE_H = 0.4, 0.85
MSG_Y, MSG_H = 1.3, 0.75
BODY_Y = 2.2
BODY_BOTTOM = 6.85
BODY_H = BODY_BOTTOM - BODY_Y
NUM_X, NUM_Y, NUM_W, NUM_H = 11.9, 6.95, 0.85, 0.35
COVER_BAND_Y, COVER_BAND_H = 2.1, 3.1
COLUMN_GAP = 0.4
STEP_GAP = 0.55

# --- 넘침 규칙 ---
MAX_BULLETS_PER_SLIDE = 10
MAX_TABLE_ROWS = 8
MAX_COLUMN_ITEMS = 8
MAX_STEPS = 6
LONG_ITEM_CHARS = 120
MIN_FONT_PT = 12

_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _clean(value) -> str:
    """XML 에 쓸 수 없는 문자를 없앤다. 줄바꿈은 \\n 하나로 맞춘다."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _ILLEGAL_XML.sub("", text)


def _list(slide: dict, key: str) -> list:
    value = slide.get(key)
    return list(value) if isinstance(value, (list, tuple)) else []


# ---------------------------------------------------------------- 줄 수 어림

LINE_SPACING = 1.25  # 글자 크기에 곱해 한 줄 높이를 어림한다
_SAFETY = 1.08       # 글꼴마다 폭이 달라 넉넉히 잡는다


def _char_em(ch: str) -> float:
    if ch == " ":
        return 0.32
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.0
    return 0.6


def _est_lines(text, pt: float, width_in: float) -> int:
    """`text` 가 폭 `width_in`(인치) 에 `pt` 크기로 들어갈 때 줄 수를 어림한다. 줄바꿈 문자는 새 줄이다."""
    avail = max(width_in, 0.5) * 72 / pt
    total = 0
    for line in _clean(text).split("\n"):
        em = sum(_char_em(ch) for ch in line) * _SAFETY
        total += max(1, math.ceil(em / avail))
    return total


def _line_h(pt: float) -> float:
    return pt * LINE_SPACING / 72


def _message_style(message: str) -> tuple[int, int]:
    """핵심 메시지의 (글자 크기, 줄 수)."""
    pt = 18 if len(message) <= 70 else 16
    return pt, _est_lines(message, pt, CONTENT_W - 0.1)


def _message_shift(slide: dict) -> float:
    """핵심 메시지가 여러 줄일 때 본문이 내려가는 만큼(인치)."""
    message = _clean(slide.get("key_message"))
    if not message:
        return 0.0
    pt, lines = _message_style(message)
    return (lines - 1) * _line_h(pt)


def _table_font(columns: list, rows: list) -> int:
    return 11 if len(columns) > 6 else 16 if len(columns) <= 4 and len(rows) <= 6 else 14


def _table_row_heights(columns: list, rows: list, pt: float) -> list[float]:
    """머리글 + 각 행의 높이(인치). 칸 안 글이 줄바꿈되는 만큼 키운다."""
    count = max(len(columns), 1)
    cell_w = CONTENT_W / count - 0.2
    base = _line_h(pt) + 0.3
    heights = []
    for r, cells in enumerate([columns, *rows]):
        cells = list(cells) if isinstance(cells, (list, tuple)) else [cells]
        lines = max([_est_lines(c, pt, cell_w) for c in cells[:count]] or [1])
        heights.append(max(base, lines * _line_h(pt) + 0.2))
    return heights


def _fit_table_font(columns: list, rows: list, avail: float = BODY_H) -> tuple[int, list[float]]:
    """처음 글자 크기에서 표가 본문 영역을 넘으면 16 → 14 → 12 → 11 로 내린다."""
    start = _table_font(columns, rows)
    sizes = [s for s in (16, 14, 12, 11) if s <= start] or [11]
    for pt in sizes:
        heights = _table_row_heights(columns, rows, pt)
        if sum(heights) <= avail:
            return pt, heights
    return sizes[-1], _table_row_heights(columns, rows, sizes[-1])


def _table_chunks(columns: list, rows: list, first_shift: float = 0.0) -> list[list]:
    """표 행을 장별로 나눈다. 장당 MAX_TABLE_ROWS 행 이하이고, 가장 작은 글자(11pt)로도 본문 영역에 들어가게 한다."""
    chunks, current, used = [], [], 0.0
    budget = BODY_H - first_shift  # 첫 장만 핵심 메시지가 여러 줄이면 그만큼 좁다
    head = _table_row_heights(columns, [], 11)[0]
    for row in rows:
        h = _table_row_heights(columns, [row], 11)[1]
        if current and (len(current) >= MAX_TABLE_ROWS or head + used + h > budget):
            chunks.append(current)
            current, used, budget = [], 0.0, BODY_H
        current.append(row)
        used += h
    return chunks + [current] if current or not chunks else chunks


# ---------------------------------------------------------------- 나누기

def _chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


def paginate(slide: dict) -> list[dict]:
    """넘치는 장을 여러 장으로 나눈다. 빈 목록은 돌려주지 않는다.

    나뉜 장은 제목에 `(2)`, `(3)` … 이 붙고, key_message / sources / needs_check 는 첫 장에만 남는다.
    """
    kind = slide.get("kind")
    if kind == "table":
        parts = [{"rows": c} for c in _table_chunks(_list(slide, "columns"), _list(slide, "rows"), _message_shift(slide))]
    elif kind == "two_column":
        left, right = _chunks(_list(slide, "left"), MAX_COLUMN_ITEMS), _chunks(_list(slide, "right"), MAX_COLUMN_ITEMS)
        count = max(len(left), len(right))
        parts = [{"left": left[i] if i < len(left) else [], "right": right[i] if i < len(right) else []} for i in range(count)]
    elif kind == "steps":
        parts = [{"steps": c} for c in _chunks(_list(slide, "steps"), MAX_STEPS)]
    elif kind in ("bullets", "closing"):
        parts = [{"bullets": c} for c in _chunks(_list(slide, "bullets"), MAX_BULLETS_PER_SLIDE)]
    else:
        return [copy.deepcopy(slide)]
    result = []
    for i, part in enumerate(parts):
        page = copy.deepcopy(slide)
        page.update(part)
        if i:
            page["title"] = f"{slide.get('title') or ''} ({i + 1})".strip()
            page["key_message"] = ""
            page["sources"] = []
            page["needs_check"] = []
            page["checked"] = []
        result.append(page)
    return result


# ---------------------------------------------------------------- 글자 도우미

def _style_run(run, size: float, *, bold=False, color=TEXT):
    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color
    font.name = FONT
    rpr = run._r.get_or_add_rPr()
    for old in rpr.findall(f"{{{_A}}}ea"):
        rpr.remove(old)
    ea = etree.SubElement(rpr, f"{{{_A}}}ea")
    ea.set("typeface", FONT)
    latin = rpr.find(f"{{{_A}}}latin")
    if latin is not None:
        latin.addnext(ea)


def _write(paragraph, text, size, *, bold=False, color=TEXT):
    """문단에 글자를 쓴다. 줄바꿈은 같은 문단 안의 줄 바꿈이 된다."""
    for i, line in enumerate(_clean(text).split("\n")):
        if i:
            paragraph.add_line_break()
        run = paragraph.add_run()
        run.text = line
        _style_run(run, size, bold=bold, color=color)


def _bullet(paragraph, size):
    ppr = paragraph._p.get_or_add_pPr()
    indent = int(Pt(size) * 1.1)
    ppr.set("marL", str(indent))
    ppr.set("indent", str(-indent))
    color = etree.SubElement(ppr, f"{{{_A}}}buClr")
    etree.SubElement(color, f"{{{_A}}}srgbClr").set("val", str(ACCENT))
    char = etree.SubElement(ppr, f"{{{_A}}}buChar")
    char.set("char", "•")


def _textbox(slide, x, y, w, h, *, anchor=MSO_ANCHOR.TOP, fit=False, margin=0.05):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = Inches(margin)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE if fit else MSO_AUTO_SIZE.NONE
    return box


def _fill_bullets(tf, items, size, *, gap=8):
    first = True
    for item in items:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(gap)
        _write(p, item, size)
        _bullet(p, size)


def _shrink_if_long(items, size) -> float:
    """한 항목이 120자를 넘으면 두 단계(4pt) 줄인다. 12pt 아래로는 내려가지 않는다."""
    if any(len(_clean(i)) > LONG_ITEM_CHARS for i in items):
        return min(size, max(MIN_FONT_PT, size - 4))
    return size


def _rect(slide, x, y, w, h, fill, *, line=None, shape=MSO_SHAPE.RECTANGLE):
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(1)
    style = shp._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}style")
    if style is not None:  # 테마의 그림자·윤곽 참조를 떼어 납작한 도형으로 둔다
        shp._element.remove(style)
    return shp


# ---------------------------------------------------------------- 그리기

def _header(slide, data: dict, *, key_message=True) -> float:
    """제목과 핵심 메시지를 그리고 본문이 시작할 y(인치)를 돌려준다. 핵심 메시지가 여러 줄이면 그만큼 내려간다."""
    title = _clean(data.get("title"))
    size = 28 if len(title) <= 30 else 24 if len(title) <= 45 else 20
    box = _textbox(slide, MARGIN, TITLE_Y, CONTENT_W, TITLE_H, anchor=MSO_ANCHOR.MIDDLE)
    _write(box.text_frame.paragraphs[0], title, size, bold=True, color=ACCENT)
    _rect(slide, MARGIN + 0.05, TITLE_Y + TITLE_H + 0.03, 1.2, 0.05, ACCENT)
    message = _clean(data.get("key_message"))
    body_y = BODY_Y
    if key_message and message:
        pt, lines = _message_style(message)
        msg_h = MSG_H + (lines - 1) * _line_h(pt)
        box = _textbox(slide, MARGIN, MSG_Y + 0.08, CONTENT_W, msg_h, anchor=MSO_ANCHOR.MIDDLE)
        _write(box.text_frame.paragraphs[0], message, pt, bold=True)
        body_y = BODY_Y + (lines - 1) * _line_h(pt)
    return body_y


def _draw_cover(slide, data: dict, customer_name: str, today: date):
    _rect(slide, 0, 0, SLIDE_W, 0.25, ACCENT)
    _rect(slide, 0, COVER_BAND_Y, SLIDE_W, COVER_BAND_H, ACCENT)
    title = _clean(data.get("title"))
    size = 40 if len(title) <= 24 else 34 if len(title) <= 40 else 28
    box = _textbox(slide, 1.0, COVER_BAND_Y + 0.25, SLIDE_W - 2.0, 1.7, anchor=MSO_ANCHOR.BOTTOM)
    _write(box.text_frame.paragraphs[0], title, size, bold=True, color=WHITE)
    subtitle = _clean(data.get("subtitle"))
    if subtitle:
        box = _textbox(slide, 1.0, COVER_BAND_Y + 2.0, SLIDE_W - 2.0, 0.9)
        _write(box.text_frame.paragraphs[0], subtitle, 20 if len(subtitle) <= 50 else 16, color=WHITE)
    box = _textbox(slide, 1.0, COVER_BAND_Y + COVER_BAND_H + 0.5, SLIDE_W - 2.0, 1.4)
    tf = box.text_frame
    p = tf.paragraphs[0]
    if _clean(customer_name):
        _write(p, customer_name, 20, bold=True)
        p = tf.add_paragraph()
    _write(p, f"{today.year}. {today.month}. {today.day}.", 16, color=MUTED)


def _draw_bullets(slide, data: dict):
    items = _list(data, "bullets")
    top = _header(slide, data)
    if not items:
        return
    size = _shrink_if_long(items, 20 if len(items) <= 7 else 16)
    box = _textbox(slide, MARGIN, top, CONTENT_W, BODY_BOTTOM - top, fit=True)
    _fill_bullets(box.text_frame, items, size, gap=10 if size >= 20 else 6)


def _draw_closing(slide, data: dict):
    items = _list(data, "bullets")
    message = _clean(data.get("key_message"))
    if not items and not message:
        box = _textbox(slide, MARGIN, 2.5, CONTENT_W, 2.5, anchor=MSO_ANCHOR.MIDDLE)
        p = box.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _write(p, data.get("title"), 40, bold=True, color=ACCENT)
        return
    _header(slide, data, key_message=False)
    if items:
        size = _shrink_if_long(items, 20 if len(items) <= 7 else 16)
        top = BODY_Y - 0.2
        box = _textbox(slide, MARGIN, top, CONTENT_W, 3.0 if message else BODY_H + 0.2, fit=True)
        _fill_bullets(box.text_frame, items, size, gap=8)
    if message:
        y = 5.35 if items else 3.2
        _rect(slide, MARGIN, y, CONTENT_W, 1.2, LIGHT)
        box = _textbox(slide, MARGIN + 0.3, y, CONTENT_W - 0.6, 1.2, anchor=MSO_ANCHOR.MIDDLE)
        p = box.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _write(p, message, 22 if len(message) <= 60 else 18, bold=True, color=ACCENT)


def _column_size(items: list) -> float:
    return _shrink_if_long(items, 18 if len(items) <= 5 else 16)


def _column_need(items: list, width: float) -> float:
    """한쪽 상자에 항목을 다 담는 데 필요한 높이(머리띠 포함)."""
    size = _column_size(items)
    text_w = width - 0.3 - 0.1 - size * 1.1 / 72
    return 0.75 + sum(_est_lines(i, size, text_w) * _line_h(size) + 6 / 72 for i in items) + 0.15


def _draw_two_column(slide, data: dict):
    top = _header(slide, data)
    width = (CONTENT_W - COLUMN_GAP) / 2
    sides = {side: _list(data, side) for side in ("left", "right")}
    room = BODY_BOTTOM - top
    height = min(room, max(3.0, *(_column_need(items, width) for items in sides.values())))
    for i, side in enumerate(("left", "right")):  # 두 상자는 더 큰 쪽 높이로 맞춘다
        x = MARGIN + i * (width + COLUMN_GAP)
        items = sides[side]
        size = _column_size(items)
        _rect(slide, x, top, width, height, STRIPE, line=LINE)
        _rect(slide, x, top, width, 0.6, ACCENT)
        head = _textbox(slide, x + 0.15, top, width - 0.3, 0.6, anchor=MSO_ANCHOR.MIDDLE)
        _write(head.text_frame.paragraphs[0], data.get(f"{side}_title"), 18, bold=True, color=WHITE)
        if items:
            body = _textbox(slide, x + 0.15, top + 0.75, width - 0.3, height - 0.9, fit=True)
            _fill_bullets(body.text_frame, items, size, gap=6)


def _draw_table(slide, data: dict):
    columns, rows = _list(data, "columns"), _list(data, "rows")
    top = _header(slide, data)
    count = len(columns)
    size, heights = _fit_table_font(columns, rows, BODY_BOTTOM - top)
    frame = slide.shapes.add_table(len(rows) + 1, count, Inches(MARGIN), Inches(top), Inches(CONTENT_W), Inches(sum(heights)))
    table = frame.table
    for c in range(count):
        table.columns[c].width = Emu(int(Inches(CONTENT_W) / count))
    for r in range(len(rows) + 1):
        table.rows[r].height = Inches(heights[r])
        cells = columns if r == 0 else rows[r - 1]
        cells = list(cells) if isinstance(cells, (list, tuple)) else [cells]
        for c in range(count):
            cell = table.cell(r, c)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = cell.margin_right = Inches(0.1)
            cell.fill.solid()
            cell.fill.fore_color.rgb = ACCENT if r == 0 else (STRIPE if r % 2 == 0 else WHITE)
            tf = cell.text_frame
            tf.word_wrap = True
            _write(tf.paragraphs[0], cells[c] if c < len(cells) else "", size, bold=r == 0, color=WHITE if r == 0 else TEXT)


def _draw_steps(slide, data: dict):
    steps = [s for s in _list(data, "steps") if isinstance(s, dict)]
    shift = _header(slide, data) - BODY_Y
    n = len(steps)
    width = (CONTENT_W - STEP_GAP * (n - 1)) / n
    name_size = 18 if n <= 4 else 16
    detail_texts = [s.get("detail") or "" for s in steps]
    detail_size = _shrink_if_long(detail_texts, 16 if n <= 4 else 14)
    top, name_h, detail_h = BODY_Y + shift + 0.4, 1.0, 2.9 - shift
    for i, step in enumerate(steps):
        x = MARGIN + i * (width + STEP_GAP)
        _rect(slide, x, top, width, name_h, ACCENT)
        head = _textbox(slide, x + 0.1, top, width - 0.2, name_h, anchor=MSO_ANCHOR.MIDDLE)
        p = head.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _write(p, step.get("name"), name_size, bold=True, color=WHITE)
        _rect(slide, x, top + name_h, width, detail_h, STRIPE, line=LINE)
        body = _textbox(slide, x + 0.1, top + name_h + 0.1, width - 0.2, detail_h - 0.2, fit=True)
        _write(body.text_frame.paragraphs[0], step.get("detail"), detail_size)
        if i < n - 1:
            _rect(slide, x + width + 0.08, top + name_h / 2 - 0.2, STEP_GAP - 0.16, 0.4, ACCENT, shape=MSO_SHAPE.RIGHT_ARROW)


def _page_number(slide, number: int):
    box = _textbox(slide, NUM_X, NUM_Y, NUM_W, NUM_H, anchor=MSO_ANCHOR.MIDDLE)
    p = box.text_frame.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    _write(p, str(number), 12, color=MUTED)


def _notes(slide, data: dict, source_labels: dict[str, str] | None = None):
    labels = source_labels or {}
    sources = [_clean(labels.get(s, s) if isinstance(s, str) else s) for s in _list(data, "sources") if _clean(s).strip()]
    checks = [_clean(s) for s in _list(data, "needs_check") if _clean(s).strip()]
    done = {_clean(s) for s in _list(data, "checked")}
    blocks = []
    if sources:
        blocks.append("\n".join(["[출처]", *sources]))
    if [c for c in checks if c not in done]:
        blocks.append("\n".join(["[확인 필요]", *[c for c in checks if c not in done]]))
    if [c for c in checks if c in done]:
        blocks.append("\n".join(["[확인함]", *[c for c in checks if c in done]]))
    if blocks:
        slide.notes_slide.notes_text_frame.text = "\n\n".join(blocks)


def _draw_body(slide, data: dict):
    """종류별로 그린다. 그릴 내용이 없는 장은 제목 + 핵심 메시지만 남는다."""
    kind = data.get("kind")
    if kind == "two_column":
        _draw_two_column(slide, data)
    elif kind == "table" and _list(data, "columns") and _list(data, "rows"):
        _draw_table(slide, data)
    elif kind == "steps" and any(isinstance(s, dict) for s in _list(data, "steps")):
        _draw_steps(slide, data)
    elif kind == "closing":
        _draw_closing(slide, data)
    elif kind in ("table", "steps"):
        _header(slide, data)
    else:
        _draw_bullets(slide, data)


def build_deck(
    outline: dict, out_path: Path, *, customer_name: str = "", today: date | None = None,
    source_labels: dict[str, str] | None = None,
) -> int:
    """구성안으로 .pptx 를 쓰고 만든 슬라이드 수를 돌려준다. 노트의 출처는 `source_labels`(경로→이름)가 있으면 이름으로 적는다."""
    source = (outline or {}).get("slides")
    if not isinstance(source, list) or not source:
        raise ValueError("구성안에 슬라이드가 없습니다.")
    today = today or date.today()
    pages = [page for s in source if isinstance(s, dict) for page in paginate(s)]
    if not pages:
        raise ValueError("구성안에 슬라이드가 없습니다.")
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    for number, data in enumerate(pages, start=1):
        slide = prs.slides.add_slide(blank)
        if data.get("kind") == "cover":
            _draw_cover(slide, data, customer_name, today)
        else:
            _draw_body(slide, data)
            _page_number(slide, number)
        _notes(slide, data, source_labels)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out_path)
    return len(pages)
