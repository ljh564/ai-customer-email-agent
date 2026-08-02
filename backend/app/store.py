"""邮件持久化仓储层。

``EmailStore`` 负责在 Pydantic 业务模型和 SQLAlchemy ORM 模型之间转换。
接口层和 workflow 层都只操作 ``EmailRecord``，不直接接触数据库表结构，
这样可以减少业务逻辑对数据库实现的依赖。
"""

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.db import SessionLocal, init_db
from app.db_models import EmailORM, EscalationTicketORM, ReviewActionORM, WorkflowStepORM
from app.mail_client import mojibake_score
from app.models import AgentMetrics, EmailAttachment, EmailCreate, EmailRecord, EscalationTicket, KnowledgeHit, ReviewAction, ReviewActionRecord, WorkflowStep


class EmailStore:
    """邮件仓储对象。

    这里封装了邮件列表、单封查询、保存、审核记录、去重和日志清理等操作。
    """

    def __init__(self) -> None:
        """初始化数据库，并在空库时写入少量示例邮件。"""
        init_db()
        with SessionLocal() as session:
            email_count = session.scalar(select(func.count()).select_from(EmailORM))
            if email_count == 0:
                for sample in seed_emails():
                    self._save(session, sample)
                session.commit()

    def list(self) -> list[EmailRecord]:
        with SessionLocal() as session:
            rows = session.scalars(
                select(EmailORM)
                .options(selectinload(EmailORM.steps), selectinload(EmailORM.review_actions), selectinload(EmailORM.escalation_tickets))
                .order_by(EmailORM.created_at.desc())
            ).all()
            return [self._to_record(row) for row in rows]

    def get(self, email_id: str) -> EmailRecord | None:
        with SessionLocal() as session:
            row = session.scalar(
                select(EmailORM)
                .options(selectinload(EmailORM.steps), selectinload(EmailORM.review_actions), selectinload(EmailORM.escalation_tickets))
                .where(EmailORM.id == email_id)
            )
            return self._to_record(row) if row else None

    def create(self, payload: EmailCreate) -> EmailRecord:
        return EmailRecord(**payload.model_dump())

    def exists_provider_message(self, provider: str, provider_message_id: str) -> bool:
        """根据邮箱服务商 message id 判断邮件是否已经导入。"""
        if not provider_message_id:
            return False
        with SessionLocal() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(EmailORM)
                    .where(
                        EmailORM.provider == provider,
                        EmailORM.provider_message_id == provider_message_id,
                    )
                )
                > 0
            )

    def list_provider_message_ids(self, provider: str, limit: int = 200) -> set[str]:
        with SessionLocal() as session:
            rows = session.scalars(
                select(EmailORM.provider_message_id)
                .where(EmailORM.provider == provider)
                .order_by(EmailORM.created_at.desc())
                .limit(limit)
            ).all()
            return {value for value in rows if value}

    def save(self, email: EmailRecord) -> EmailRecord:
        """保存邮件以及它的执行轨迹。"""
        with SessionLocal() as session:
            saved = self._save(session, email)
            session.commit()
            session.refresh(saved)
            return self.get(saved.id) or email

    def record_review(self, email_id: str, payload: ReviewAction) -> None:
        """记录人工审核动作。

        如果连续提交完全相同的动作、备注和修改稿，则不重复写入，避免用户反复点击
        “升级/撤销”时审核历史无限膨胀。
        """
        with SessionLocal() as session:
            latest = session.scalar(
                select(ReviewActionORM)
                .where(ReviewActionORM.email_id == email_id)
                .order_by(ReviewActionORM.created_at.desc())
            )
            if (
                latest is not None
                and latest.action == payload.action
                and latest.note == payload.note
                and latest.revised_reply == payload.revised_reply
            ):
                return
            session.add(
                ReviewActionORM(
                    email_id=email_id,
                    action=payload.action,
                    note=payload.note,
                    revised_reply=payload.revised_reply,
                )
            )
            session.commit()

    def open_escalation_ticket(self, email_id: str, *, reason: str, created_by: str) -> EscalationTicket:
        """为邮件创建或复用一条未关闭的升级工单。"""
        with SessionLocal() as session:
            ticket = session.scalar(
                select(EscalationTicketORM)
                .where(
                    EscalationTicketORM.email_id == email_id,
                    EscalationTicketORM.status.in_(("open", "assigned")),
                )
                .order_by(EscalationTicketORM.created_at.desc())
            )
            if ticket is None:
                ticket = EscalationTicketORM(
                    email_id=email_id,
                    status="open",
                    reason=reason,
                    created_by=created_by,
                )
                session.add(ticket)
            else:
                ticket.reason = reason or ticket.reason
                ticket.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(ticket)
            return self._ticket_to_model(ticket)

    def update_escalation_ticket(self, email_id: str, *, action: str, note: str, operator: str) -> EscalationTicket:
        """处理升级工单：接单、解决或退回审核。"""
        with SessionLocal() as session:
            ticket = session.scalar(
                select(EscalationTicketORM)
                .where(EscalationTicketORM.email_id == email_id)
                .order_by(EscalationTicketORM.created_at.desc())
            )
            if ticket is None:
                ticket = EscalationTicketORM(email_id=email_id, status="open", created_by=operator)
                session.add(ticket)

            if action == "assign":
                ticket.status = "assigned"
                ticket.assigned_to = operator
            elif action == "resolve":
                ticket.status = "resolved"
                ticket.assigned_to = ticket.assigned_to or operator
                ticket.resolution_note = note
            elif action == "return_to_review":
                ticket.status = "returned"
                ticket.assigned_to = ticket.assigned_to or operator
                ticket.resolution_note = note
            ticket.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(ticket)
            return self._ticket_to_model(ticket)

    def cleanup_workflow_steps(self, retention_days: int = 30) -> int:
        """清理已结束邮件的旧执行轨迹。"""
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        closed_statuses = ("processed", "ready_to_send", "escalated", "sent")
        with SessionLocal() as session:
            closed_email_ids = select(EmailORM.id).where(
                EmailORM.updated_at < cutoff,
                EmailORM.status.in_(closed_statuses),
            )
            result = session.execute(delete(WorkflowStepORM).where(WorkflowStepORM.email_id.in_(closed_email_ids)))
            session.commit()
            return result.rowcount or 0

    def delete_corrupted_provider_messages(self, provider: str) -> int:
        with SessionLocal() as session:
            rows = session.scalars(select(EmailORM).where(EmailORM.provider == provider)).all()
            corrupted_ids = [
                row.id
                for row in rows
                if is_corrupted_text(row.subject) or is_corrupted_text(row.body)
            ]
            if not corrupted_ids:
                return 0
            result = session.execute(delete(EmailORM).where(EmailORM.id.in_(corrupted_ids)))
            session.commit()
            return result.rowcount or 0

    def _save(self, session: Session, email: EmailRecord) -> EmailORM:
        email.updated_at = datetime.utcnow()
        row = session.get(EmailORM, email.id)
        if row is None:
            row = EmailORM(id=email.id)
            session.add(row)

        for field, value in self._email_columns(email).items():
            setattr(row, field, value)

        session.execute(delete(WorkflowStepORM).where(WorkflowStepORM.email_id == email.id))
        session.flush()
        row.steps = [
            WorkflowStepORM(
                email_id=email.id,
                position=index,
                name=step.name,
                status=step.status,
                summary=step.summary,
                detail=step.detail,
                confidence=step.confidence,
                timestamp=step.timestamp,
            )
            for index, step in enumerate(email.steps)
        ]
        return row

    @staticmethod
    def _email_columns(email: EmailRecord) -> dict:
        return {
            "customer_name": email.customer_name,
            "customer_email": email.customer_email,
            "subject": email.subject,
            "body": email.body,
            "attachments": [attachment.model_dump() for attachment in email.attachments],
            "provider": email.provider,
            "provider_message_id": email.provider_message_id,
            "category": email.category,
            "priority": email.priority,
            "status": email.status,
            "processing_status": email.processing_status,
            "processing_stage": email.processing_stage,
            "processing_progress": email.processing_progress,
            "processing_message": email.processing_message,
            "processing_started_at": email.processing_started_at,
            "processing_finished_at": email.processing_finished_at,
            "confidence": email.confidence,
            "detected_language": email.detected_language,
            "preprocessing_flags": email.preprocessing_flags,
            "risk_level": email.risk_level,
            "risk_flags": email.risk_flags,
            "should_escalate": email.should_escalate,
            "analysis_reason": email.analysis_reason,
            "knowledge_hits": [hit.model_dump() for hit in email.knowledge_hits],
            "draft_reply": email.draft_reply,
            "agent_metrics": email.agent_metrics.model_dump(),
            "review_note": email.review_note,
            "created_at": email.created_at,
            "updated_at": email.updated_at,
        }

    @staticmethod
    def _to_record(row: EmailORM) -> EmailRecord:
        return EmailRecord(
            id=row.id,
            customer_name=row.customer_name,
            customer_email=row.customer_email,
            subject=row.subject,
            body=row.body,
            attachments=[EmailAttachment(**attachment) for attachment in (row.attachments or [])],
            provider=row.provider,
            provider_message_id=row.provider_message_id,
            category=row.category,
            priority=row.priority,
            status=row.status,
            processing_status=row.processing_status,
            processing_stage=row.processing_stage,
            processing_progress=row.processing_progress,
            processing_message=row.processing_message,
            processing_started_at=row.processing_started_at,
            processing_finished_at=row.processing_finished_at,
            confidence=row.confidence,
            detected_language=row.detected_language,
            preprocessing_flags=row.preprocessing_flags or [],
            risk_level=row.risk_level,
            risk_flags=row.risk_flags or [],
            should_escalate=row.should_escalate,
            analysis_reason=row.analysis_reason,
            knowledge_hits=[KnowledgeHit(**hit) for hit in (row.knowledge_hits or [])],
            draft_reply=row.draft_reply,
            agent_metrics=AgentMetrics(**(row.agent_metrics or {})),
            review_note=row.review_note,
            steps=[
                WorkflowStep(
                    name=step.name,
                    status=step.status,
                    summary=step.summary,
                    detail=step.detail,
                    confidence=step.confidence,
                    timestamp=step.timestamp,
                )
                for step in row.steps
            ],
            review_actions=[
                ReviewActionRecord(
                    action=action.action,
                    note=action.note,
                    revised_reply=action.revised_reply,
                    created_at=action.created_at,
                )
                for action in row.review_actions
            ],
            escalation_ticket=EmailStore._ticket_to_model(row.escalation_tickets[0]) if row.escalation_tickets else None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _ticket_to_model(row: EscalationTicketORM) -> EscalationTicket:
        return EscalationTicket(
            id=row.id,
            email_id=row.email_id,
            status=row.status,
            reason=row.reason,
            created_by=row.created_by,
            assigned_to=row.assigned_to,
            resolution_note=row.resolution_note,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def seed_emails() -> list[EmailRecord]:
    samples = [
        EmailCreate(
            customer_name="Lina Chen",
            customer_email="lina@example.com",
            subject="Refund request for duplicate subscription charge",
            body=(
                "Hi team, I was charged twice for my Pro subscription this month. "
                "Please refund the duplicate payment and confirm when this is resolved."
            ),
        ),
        EmailCreate(
            customer_name="Marcus Lee",
            customer_email="marcus@example.com",
            subject="Cannot log in after password reset",
            body=(
                "I reset my password three times but the app still says the token is invalid. "
                "I need access before my client meeting today."
            ),
        ),
        EmailCreate(
            customer_name="Priya Shah",
            customer_email="priya@example.com",
            subject="Very unhappy with support response time",
            body=(
                "This is my third email. Nobody has helped us and our team is blocked. "
                "If this continues we will cancel the annual contract."
            ),
        ),
        EmailCreate(
            customer_name="Wang Ming",
            customer_email="wangming@example.com",
            subject="重复扣费需要退款",
            body="你好，我这个月订阅被重复扣费了两次，请帮我退款。如果没人处理，我会考虑取消合同。",
        ),
    ]

    from app.workflow import process_email

    return [process_email(EmailRecord(**sample.model_dump()), use_llm=False) for sample in samples]


def is_corrupted_text(value: str | None) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    question_count = text.count("?")
    mojibake_markers = (
        "锟", "閿", "�", "Ã", "Â", "浣", "犲", "ソ", "鏂", "鎬", "煡", "鐪",
        "閫", "氱", "洿", "规", "嵁", "涓", "湪", "瀹", "㈡", "湇",
    )
    marker_count = sum(text.count(marker) for marker in mojibake_markers)
    cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    common_chinese = set("的一是在不了有和人这中大为上个我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经")
    common_chinese_count = sum(1 for char in text if char in common_chinese)
    random_cjk_like = cjk_count >= 30 and common_chinese_count / max(cjk_count, 1) < 0.08
    control_count = sum(1 for char in text if "\x80" <= char <= "\x9f")
    private_use_count = sum(1 for char in text if "\ue000" <= char <= "\uf8ff")
    hangul_count = sum(1 for char in text if "\uac00" <= char <= "\ud7af")
    return (
        text == "�"
        or "�" in text
        or "????" in text
        or "锟" in text
        or "瑞-偶" in text
        or marker_count >= 3
        or mojibake_score(text) >= 4
        or random_cjk_like
        or control_count >= 2
        or private_use_count >= 2
        or hangul_count >= 2
        or question_count / max(len(text), 1) > 0.25
    )
