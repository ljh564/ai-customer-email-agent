"""API 数据模型。

这些 Pydantic 模型是前后端交互的稳定契约：
- 输入模型用于校验前端提交的数据。
- 输出模型用于约束接口返回结构。
- 业务流程内部也复用这些模型，保证 workflow、store、frontend 对字段理解一致。
"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

EmailCategory = Literal["refund", "complaint", "technical", "billing", "product_question", "other"]
EmailStatus = Literal["new", "processed", "human_review", "ready_to_send", "needs_revision", "escalated", "sent", "irrelevant"]
EmailProcessingStatus = Literal["queued", "running", "completed", "failed"]
RiskLevel = Literal["low", "medium", "high"]
ReviewActionType = Literal["approve", "revise", "escalate", "undo_escalate"]
KnowledgeDocumentStatus = Literal["processing", "indexed", "failed", "needs_reindex"]
EscalationStatus = Literal["open", "assigned", "resolved", "returned"]
KnowledgeReliability = Literal["strong", "medium", "weak"]


class EmailCreate(BaseModel):
    """创建邮件时的输入结构。"""

    customer_name: str = Field(min_length=1)
    customer_email: str = Field(min_length=3)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=10)
    attachments: list["EmailAttachment"] = Field(default_factory=list)


class EmailAttachment(BaseModel):
    filename: str
    content_type: str = ""
    size_bytes: int = 0
    text_preview: str = ""
    parse_status: str = "metadata_only"
    status_message: str = ""
    parse_report: dict = Field(default_factory=dict)


class KnowledgeHit(BaseModel):
    """RAG 返回的一条知识库依据。"""

    title: str
    source: str
    snippet: str
    score: float
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    category_score: float = 0.0
    category: str = "other"
    match_reason: str = ""
    reliability: KnowledgeReliability = "weak"
    page_number: int | None = None
    section_title: str = ""


class KnowledgeDocument(BaseModel):
    id: str
    title: str
    source: str
    chunk_count: int
    current_version: int = 0
    status: KnowledgeDocumentStatus = "indexed"
    status_message: str = ""
    parse_report: dict = Field(default_factory=dict)
    created_at: datetime


class KnowledgeDocumentDetail(KnowledgeDocument):
    content: str


class KnowledgeDocumentVersion(BaseModel):
    id: str
    document_id: str
    version_number: int
    title: str
    source: str
    content_hash: str
    content_snapshot: str = ""
    chunk_count: int
    status: KnowledgeDocumentStatus = "indexed"
    status_message: str = ""
    parse_report: dict = Field(default_factory=dict)
    note: str = ""
    created_at: datetime


class OperationLog(BaseModel):
    id: str
    scope: str
    action: str
    title: str
    summary: str
    detail: dict = Field(default_factory=dict)
    created_at: datetime


class KnowledgeDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    source: str = Field(default="", max_length=160)
    content: str = Field(min_length=20)


class KnowledgeDocumentUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=20)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=2)
    category: EmailCategory | None = None
    limit: int = Field(default=3, ge=1, le=10)


class WorkflowStep(BaseModel):
    """Agent 工作流中的单个执行步骤。"""

    name: str
    status: Literal["complete", "warning", "blocked"]
    summary: str
    detail: str
    confidence: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AgentMetrics(BaseModel):
    """单封邮件的成本与 token 估算指标。"""

    llm_calls: int = 0
    semantic_llm_calls: int = 0
    draft_llm_calls: int = 0
    embedding_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    rag_latency_ms: int = 0
    estimated_cost_cny: float = 0.0


class ReviewActionRecord(BaseModel):
    action: ReviewActionType
    note: str = ""
    revised_reply: str = ""
    created_at: datetime


class EscalationTicket(BaseModel):
    """邮件升级后生成的内部处理工单。"""

    id: str
    email_id: str
    status: EscalationStatus = "open"
    reason: str = ""
    created_by: str = ""
    assigned_to: str = ""
    resolution_note: str = ""
    created_at: datetime
    updated_at: datetime


class EmailRecord(BaseModel):
    """系统中完整的一封邮件记录。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    customer_name: str
    customer_email: str
    subject: str
    body: str
    attachments: list[EmailAttachment] = Field(default_factory=list)
    provider: Literal["manual", "qq", "outlook", "gmail"] = "manual"
    provider_message_id: str = ""
    category: EmailCategory | None = None
    priority: RiskLevel = "medium"
    status: EmailStatus = "new"
    processing_status: EmailProcessingStatus = "queued"
    processing_stage: str = "queued"
    processing_progress: int = Field(default=5, ge=0, le=100)
    processing_message: str = "等待 Agent 处理"
    processing_started_at: datetime | None = None
    processing_finished_at: datetime | None = None
    confidence: float = 0.0
    detected_language: Literal["en", "zh"] = "en"
    preprocessing_flags: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    risk_flags: list[str] = Field(default_factory=list)
    should_escalate: bool = False
    analysis_reason: str = ""
    knowledge_hits: list[KnowledgeHit] = Field(default_factory=list)
    draft_reply: str = ""
    agent_metrics: AgentMetrics = Field(default_factory=AgentMetrics)
    review_note: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    review_actions: list[ReviewActionRecord] = Field(default_factory=list)
    escalation_ticket: EscalationTicket | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ReviewAction(BaseModel):
    action: ReviewActionType
    note: str = ""
    revised_reply: str = ""


class EscalationUpdate(BaseModel):
    action: Literal["assign", "resolve", "return_to_review"]
    note: str = ""


MailProvider = Literal["qq", "outlook", "gmail"]
MailAuthMode = Literal["imap_smtp", "api_oauth"]


class MailSourceConfigInput(BaseModel):
    """管理员提交的邮件源配置。

    密钥字段允许留空，表示继续沿用服务器现有配置；这样编辑主机或邮箱地址时
    不需要把旧密钥返回到浏览器。
    """

    provider: MailProvider
    auth_mode: MailAuthMode = "imap_smtp"
    email_address: str = ""
    credential: str = ""
    imap_host: str = ""
    imap_port: int | None = None
    smtp_host: str = ""
    smtp_port: int | None = None
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""


class MailSourceInfo(BaseModel):
    provider: MailProvider
    auth_mode: MailAuthMode = "imap_smtp"
    label: str
    active: bool = False
    configured: bool = False
    secret_configured: bool = False
    imap_secret_configured: bool = False
    oauth_secret_configured: bool = False
    email_address: str = ""
    imap_host: str = ""
    imap_port: int | None = None
    smtp_host: str = ""
    smtp_port: int | None = None
    tenant_id: str = ""
    client_id: str = ""
    redirect_uri: str = ""


class MailOAuthStartResult(BaseModel):
    authorization_url: str


class MailSourceState(BaseModel):
    active_provider: MailProvider
    sources: list[MailSourceInfo]


class MailSourceSwitchResult(BaseModel):
    message: str
    state: MailSourceState
