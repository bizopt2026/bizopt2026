"""DB 모델."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Column, Date, DateTime, ForeignKey, Integer, String, Table, Text, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from app.db import Base


class Stamped:
    """작성자와 생성·수정 시각."""

    @declared_attr
    def created_by_id(cls) -> Mapped[int | None]:
        return mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), default=datetime.now, onupdate=datetime.now
    )


def _link_table(name: str, other: str, other_table: str) -> Table:
    return Table(
        name,
        Base.metadata,
        Column("activity_id", ForeignKey("activities.id", ondelete="CASCADE"), primary_key=True),
        Column(f"{other}_id", ForeignKey(f"{other_table}.id", ondelete="CASCADE"), primary_key=True),
    )


activity_solutions = _link_table("activity_solutions", "solution", "solutions")
activity_customers = _link_table("activity_customers", "customer", "customers")
activity_businesses = _link_table("activity_businesses", "business", "businesses")
activity_partners = _link_table("activity_partners", "partner", "partners")

proposal_solutions = Table(
    "proposal_solutions",
    Base.metadata,
    Column("proposal_id", ForeignKey("proposals.id", ondelete="CASCADE"), primary_key=True),
    Column("solution_id", ForeignKey("solutions.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, default="")
    password_hash: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="user")  # admin / user
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)


class Partner(Stamped, Base):
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    contact: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    memo: Mapped[str] = mapped_column(String, default="")

    solutions: Mapped[list[Solution]] = relationship(back_populates="partner")


class Customer(Stamped, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    industry: Mapped[str] = mapped_column(String, default="")
    contact: Mapped[str] = mapped_column(String, default="")
    memo: Mapped[str] = mapped_column(String, default="")

    businesses: Mapped[list[Business]] = relationship(back_populates="customer")
    proposals: Mapped[list[Proposal]] = relationship(back_populates="customer")


class Business(Stamped, Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    description: Mapped[str] = mapped_column(String, default="")
    period: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="")
    memo: Mapped[str] = mapped_column(String, default="")

    customer: Mapped[Customer] = relationship(back_populates="businesses")
    proposals: Mapped[list[Proposal]] = relationship(back_populates="business")


class Solution(Stamped, Base):
    __tablename__ = "solutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    partner_id: Mapped[int | None] = mapped_column(ForeignKey("partners.id"), nullable=True)
    stage: Mapped[str] = mapped_column(String, default="intake")
    usage_direction: Mapped[str] = mapped_column(String, default="")
    applicable_types: Mapped[str] = mapped_column(String, default="")
    tags: Mapped[str] = mapped_column(String, default="")  # 쉼표 구분

    partner: Mapped[Partner | None] = relationship(back_populates="solutions")
    files: Mapped[list[SolutionFile]] = relationship(
        back_populates="solution", cascade="all, delete-orphan"
    )
    activities: Mapped[list[Activity]] = relationship(
        secondary=activity_solutions, back_populates="solutions"
    )
    proposals: Mapped[list[Proposal]] = relationship(
        secondary=proposal_solutions, back_populates="solutions"
    )


class SolutionFile(Stamped, Base):
    __tablename__ = "solution_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    solution_id: Mapped[int] = mapped_column(ForeignKey("solutions.id"))
    original_name: Mapped[str] = mapped_column(String, default="")
    stored_name: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String, default="related")
    extract_status: Mapped[str] = mapped_column(String, default="")  # ok / failed / unsupported
    size: Mapped[int] = mapped_column(Integer, default=0)

    solution: Mapped[Solution] = relationship(back_populates="files")


class Activity(Stamped, Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, default="meeting")
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    title: Mapped[str] = mapped_column(String, default="")
    raw_text: Mapped[str] = mapped_column(Text)
    summary_md: Mapped[str] = mapped_column(Text, default="")
    # [{"task","owner","due","done"}]
    follow_ups: Mapped[list] = mapped_column(JSON, default=list)
    # [{"customer","content"}]
    demand_signals: Mapped[list] = mapped_column(JSON, default=list)

    solutions: Mapped[list[Solution]] = relationship(
        secondary=activity_solutions, back_populates="activities"
    )
    customers: Mapped[list[Customer]] = relationship(secondary=activity_customers)
    businesses: Mapped[list[Business]] = relationship(secondary=activity_businesses)
    partners: Mapped[list[Partner]] = relationship(secondary=activity_partners)


class AIJob(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default="")
    error_kind: Mapped[str] = mapped_column(String, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Intake(Stamped, Base):
    """올리기 묶음: 한 번에 올린 파일들과 그 정리 상태."""

    __tablename__ = "intakes"

    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="uploaded")  # uploaded / analyzing / ready / applying(저장 중) / applied / failed / discarded
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[list] = mapped_column(JSON, default=list)  # 저장으로 만들어진 기록 [{"type", "id", "label"}]
    job_id: Mapped[int | None] = mapped_column(ForeignKey("ai_jobs.id"), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    apply_token: Mapped[str | None] = mapped_column(String, nullable=True)  # 저장 중(applying)인 선점의 정체. 그 선점만 저장을 끝내거나 풀 수 있다

    files: Mapped[list["IntakeFile"]] = relationship(
        back_populates="intake", cascade="all, delete-orphan", order_by="IntakeFile.id"
    )
    job: Mapped[AIJob | None] = relationship()


class IntakeFile(Base):
    __tablename__ = "intake_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    intake_id: Mapped[int] = mapped_column(ForeignKey("intakes.id"))
    original_name: Mapped[str] = mapped_column(String, default="")
    stored_name: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String, default="other")  # document / image / audio / other
    read_status: Mapped[str] = mapped_column(String, default="")  # ok / failed / unsupported / needs_transcript / pending_transcript
    read_error: Mapped[str | None] = mapped_column(String, nullable=True)  # 읽지 못한 이유 (받아쓰기 실패 등, 화면에 보이는 한국어)
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 받아쓰기 진행 0~100
    size: Mapped[int] = mapped_column(Integer, default=0)
    target_type: Mapped[str] = mapped_column(String, default="")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)

    intake: Mapped[Intake] = relationship(back_populates="files")


class Proposal(Stamped, Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String, default="")
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    request_memo: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="new")
    extra_solution_ids: Mapped[list] = mapped_column(JSON, default=list)
    directions: Mapped[list] = mapped_column(JSON, default=list)
    selected_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    outline_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deck_name: Mapped[str] = mapped_column(String, default="")
    deck_built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_job_id: Mapped[int | None] = mapped_column(ForeignKey("ai_jobs.id"), nullable=True)
    chat_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 이 번호 이하의 대화는 현재 방향의 것이 아니다

    customer: Mapped[Customer] = relationship(back_populates="proposals")
    business: Mapped[Business | None] = relationship(back_populates="proposals")
    solutions: Mapped[list[Solution]] = relationship(
        secondary=proposal_solutions, back_populates="proposals"
    )
    messages: Mapped[list[ProposalMessage]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan", order_by="ProposalMessage.id"
    )
    active_job: Mapped[AIJob | None] = relationship(foreign_keys=[active_job_id])


class ProposalMessage(Base):
    __tablename__ = "proposal_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"))
    role: Mapped[str] = mapped_column(String)  # user / assistant
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)

    proposal: Mapped[Proposal] = relationship(back_populates="messages")


class ResearchTopic(Stamped, Base):
    __tablename__ = "research_topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    keywords: Mapped[str] = mapped_column(String, default="")
    area: Mapped[str] = mapped_column(String)  # market / tech / policy
    is_active: Mapped[bool] = mapped_column(default=True)


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    slot: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 예약 실행이 맡은 예정 시각
    trigger: Mapped[str] = mapped_column(String)  # schedule / manual
    status: Mapped[str] = mapped_column(String, default="running")  # running / done / partial / failed
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    reports: Mapped[list[ResearchReport]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ResearchReport.id"
    )


class ResearchReport(Base):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_runs.id"))
    kind: Mapped[str] = mapped_column(String)  # area / suggest
    area: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    trends: Mapped[list] = mapped_column(JSON, default=list)
    insights: Mapped[list] = mapped_column(JSON, default=list)
    suggestions: Mapped[list] = mapped_column(JSON, default=list)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("ai_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending / done / failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=datetime.now)

    run: Mapped[ResearchRun] = relationship(back_populates="reports")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
