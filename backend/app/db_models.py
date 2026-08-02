"""SQLAlchemy 数据库表模型。

这里定义的是数据库持久化结构，不直接作为接口返回值。
对外 API 使用 ``models.py`` 中的 Pydantic 模型，仓储层 ``store.py`` 和
``knowledge.py`` 负责两者之间的转换。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class EmailORM(Base):
    """邮件主表。

    一封邮件包含原始正文、分类结果、RAG 命中、回复草稿、成本指标和审核状态。
    执行轨迹与审核历史拆到子表，避免主表字段过长且便于独立清理。
    """

    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(255))
    customer_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    provider: Mapped[str] = mapped_column(String(32), default="manual")
    provider_message_id: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[str] = mapped_column(String(32), default="medium")
    status: Mapped[str] = mapped_column(String(32), default="new")
    processing_status: Mapped[str] = mapped_column(String(32), default="queued")
    processing_stage: Mapped[str] = mapped_column(String(64), default="queued")
    processing_progress: Mapped[int] = mapped_column(Integer, default=5)
    processing_message: Mapped[str] = mapped_column(String(500), default="")
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    detected_language: Mapped[str] = mapped_column(String(8), default="en")
    preprocessing_flags: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(32), default="low")
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    should_escalate: Mapped[bool] = mapped_column(Boolean, default=False)
    analysis_reason: Mapped[str] = mapped_column(Text, default="")
    knowledge_hits: Mapped[list] = mapped_column(JSON, default=list)
    draft_reply: Mapped[str] = mapped_column(Text, default="")
    agent_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    steps: Mapped[list["WorkflowStepORM"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="WorkflowStepORM.position",
    )
    review_actions: Mapped[list["ReviewActionORM"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="ReviewActionORM.created_at",
    )
    escalation_tickets: Mapped[list["EscalationTicketORM"]] = relationship(
        back_populates="email",
        cascade="all, delete-orphan",
        order_by="EscalationTicketORM.created_at.desc()",
    )


class UserORM(Base):
    """后台用户表。

    企业后台需要知道“谁在操作系统”。这里先实现最小可用的 RBAC 账号模型：
    - admin：系统管理员，拥有全部权限。
    - manager：客服主管，可审核邮件、管理知识库和查看日志。
    - agent：客服人员，可处理邮件和审核队列中的日常操作。

    密码不会明文保存，``password_hash`` 存储的是 PBKDF2 派生后的哈希字符串。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(32), default="agent", index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkflowStepORM(Base):
    """邮件 Agent 执行轨迹表。"""

    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[EmailORM] = relationship(back_populates="steps")


class ReviewActionORM(Base):
    """人工审核动作记录表。"""

    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email_id: Mapped[str] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    revised_reply: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[EmailORM] = relationship(back_populates="review_actions")


class EscalationTicketORM(Base):
    """升级工单表。

    客服人员点击“升级处理”后，不只是给邮件打标签，而是在这里生成一条内部工单。
    manager/admin 可以接单、处理完成或退回审核队列。
    """

    __tablename__ = "escalation_tickets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    email_id: Mapped[str] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    assigned_to: Mapped[str] = mapped_column(String(80), default="")
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[EmailORM] = relationship(back_populates="escalation_tickets")


class OperationLogORM(Base):
    """系统运行日志表。

    记录知识库版本操作、邮箱同步、真实发信、后台处理等跨模块事件。
    """

    __tablename__ = "operation_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MailSourceConfigORM(Base):
    """邮件源配置表。

    普通连接参数保存在 ``config`` 中；授权码、应用密码和客户端密钥经过
    Fernet 加密后写入 ``encrypted_secrets``，接口不会返回密文或明文。
    """

    __tablename__ = "mail_source_configs"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    encrypted_secrets: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class KnowledgeDocumentORM(Base):
    """知识库文档主表。"""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="indexed")
    status_message: Mapped[str] = mapped_column(Text, default="")
    parse_report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    chunks: Mapped[list["KnowledgeChunkORM"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="KnowledgeChunkORM.position",
    )
    versions: Mapped[list["KnowledgeDocumentVersionORM"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="KnowledgeDocumentVersionORM.version_number.desc()",
    )


class KnowledgeDocumentVersionORM(Base):
    """知识库文档版本表。"""

    __tablename__ = "knowledge_document_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(500), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_snapshot: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="indexed")
    status_message: Mapped[str] = mapped_column(Text, default="")
    parse_report: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped[KnowledgeDocumentORM] = relationship(back_populates="versions")


class KnowledgeChunkORM(Base):
    """知识库 chunk 表。

    ``embedding`` 保存 JSON 向量，``embedding_vector`` 由数据库迁移层添加，
    用于 pgvector 检索。这里保留 JSON 字段是为了兼容 SQLite 和便于调试。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(500), index=True)
    category: Mapped[str] = mapped_column(String(64), default="other", index=True)
    content: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_title: Mapped[str] = mapped_column(String(255), default="")
    embedding_model: Mapped[str] = mapped_column(String(120), default="")
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document: Mapped[KnowledgeDocumentORM] = relationship(back_populates="chunks")
