"""코드값과 화면에 보일 한국어 이름."""
from datetime import datetime

STAGES = {
    "intake": "아이템 접수",
    "tapping": "사전 태핑",
    "survey": "수요조사",
    "boxed": "박스 등록",
    "hold": "보류",
}

ACTIVITY_KINDS = {
    "meeting": "회의",
    "partner": "파트너 미팅",
    "tapping": "고객 태핑",
    "survey": "수요조사",
    "other": "기타",
}

FILE_KINDS = {"intro": "소개", "spec": "스펙", "related": "관련"}

INTAKE_STATUSES = {
    "uploaded": "올림",
    "analyzing": "정리 중",
    "ready": "확인 대기",
    "applying": "저장 중",
    "applied": "저장 완료",
    "failed": "실패",
    "discarded": "버림",
}

INTAKE_FILE_KINDS = {"document": "문서", "image": "사진", "audio": "음성", "other": "기타"}

INTAKE_ITEM_KINDS = {
    "new_solution": "새 솔루션",
    "add_to_solution": "솔루션에 자료 추가",
    "add_contact": "연락처 추가",
    "activity": "활동 기록",
    "new_customer": "새 고객",
    "new_partner": "새 파트너",
    "unplaced": "정리하지 못한 파일",
}

READ_STATUSES = {
    "ok": "읽음",
    "failed": "읽지 못함",
    "unsupported": "보관만 함",
    "needs_transcript": "받아쓰기 필요",
    "pending_transcript": "받아쓰기 대기",
}

JOB_STATUSES = {
    "pending": "대기",
    "running": "진행",
    "done": "완료",
    "failed": "실패",
}

JOB_KINDS = {
    "activity_structure": "활동 기록 AI 정리",
    "solution_draft": "솔루션 소개 초안",
    "proposal_directions": "제안 방향 만들기",
    "proposal_chat": "제안 채팅 답변",
    "proposal_outline": "제안서 구성안 만들기",
    "research_area": "리서치 분야 조사",
    "research_suggest": "리서치 발굴 방향 제안",
    "intake_analyze": "올린 파일 정리",
    "ai_ping": "AI 연결 확인",
}

# AI 작업 종류별 예상 시간 (대기 안내에 쓴다)
JOB_EXPECTED = {
    "activity_structure": "보통 20초 안에",
    "solution_draft": "보통 30초 안에",
    "proposal_directions": "보통 1분 안에",
    "proposal_chat": "보통 1분 안에",
    "proposal_outline": "보통 1분 안에",
    "research_area": "분야마다 1~2분",
    "research_suggest": "보통 1분 안에",
    "intake_analyze": "보통 1분 안에",
    "ai_ping": "보통 10초 안에",
}

ERROR_KINDS = {
    "not_installed": "Claude CLI가 설치되어 있지 않습니다. 서버에 설치되어 있는지 확인해 주세요.",
    "auth": "Claude 로그인이 필요합니다. 서버에서 Claude에 다시 로그인해 주세요.",
    "limit": "Claude 사용 한도에 도달했습니다. 잠시 뒤에 다시 시도해 주세요.",
    "network": "네트워크 연결에 문제가 있습니다. 연결을 확인한 뒤 다시 시도해 주세요.",
    "timeout": "제한 시간 안에 작업이 끝나지 않았습니다. 잠시 뒤에 다시 시도해 주세요.",
    "bad_output": "AI가 예상하지 못한 형식으로 답했습니다. 다시 시도해 주세요.",
    "unknown": "알 수 없는 오류가 발생했습니다. 잠시 뒤에 다시 시도해 주세요.",
}

PROPOSAL_STATUSES = {
    "new": "대상 지정",
    "directions": "방향 검토",
    "refining": "구체화",
    "outlined": "구성안",
    "deck": "제안서 완료",
}

SLIDE_KINDS = {
    "cover": "표지",
    "bullets": "글머리",
    "two_column": "2단 비교",
    "table": "표",
    "steps": "단계",
    "closing": "마무리",
}

RESEARCH_AREAS = {"market": "시장", "tech": "기술", "policy": "정책"}

SUGGESTION_KINDS = {"new": "새로 찾을 솔루션", "push": "기존 솔루션 밀기"}

RUN_STATUSES = {"running": "진행", "done": "완료", "partial": "일부 실패", "failed": "실패"}

WEEKDAYS = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}


def elapsed_text(started_at: datetime, now: datetime | None = None) -> str:
    """"35초 경과", "1분 35초 경과" 처럼 시작 뒤 지난 시간."""
    seconds = max(0, int(((now or datetime.now()) - started_at).total_seconds()))
    minutes, rest = divmod(seconds, 60)
    return (f"{minutes}분 {rest}초" if minutes and rest else f"{minutes}분" if minutes else f"{rest}초") + " 경과"


def wait_detail(job, now: datetime | None = None, expected: bool = True) -> str:
    """진행 중인 작업 옆에 붙일 안내: "(보통 1분 안에 끝납니다 · 35초 경과)". 시작 전이면 빈 글."""
    if job.started_at is None:
        return ""
    hint = JOB_EXPECTED.get(job.kind, "") if expected else ""
    parts = []
    if hint:
        parts.append(hint + " 끝납니다" if hint.endswith("안에") else hint)
    parts.append(elapsed_text(job.started_at, now))
    return "(" + " · ".join(parts) + ")"
