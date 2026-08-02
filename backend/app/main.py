"""FastAPI 接口入口。

这个文件只负责 HTTP API 编排：
- 邮件相关接口：创建/同步/处理/审核/发送。
- 知识库相关接口：上传、增删改查、版本回退、重新索引。
- 运行日志接口：展示和清理邮件 Agent 轨迹、知识库操作日志。

真正的业务逻辑分别下沉到 ``workflow.py``、``knowledge.py``、``mail_client.py``
和 ``store.py``，这样接口层保持薄而清晰。
"""

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

from app.auth import (
    CurrentUser,
    LoginRequest,
    LoginResponse,
    UserProfile,
    authenticate_user,
    create_access_token,
    ensure_default_users,
    require_roles,
    to_profile,
)
from app.knowledge import (
    cleanup_operation_logs,
    create_knowledge_document,
    create_knowledge_document_upload_job_with_duplicate_policy,
    delete_knowledge_document,
    delete_knowledge_document_version,
    get_knowledge_document,
    ingest_knowledge_documents,
    index_knowledge_document,
    list_knowledge_document_versions,
    list_knowledge_documents,
    list_operation_logs,
    record_operation_log,
    reindex_knowledge_document,
    restore_knowledge_document_version,
    search_knowledge,
    update_knowledge_document,
    update_knowledge_document_from_upload,
)
from app.mail_client import MailClientConfigError
from app.mail_sources import (
    PROVIDER_LABELS,
    begin_oauth,
    complete_oauth,
    configure_and_activate,
    fetch_active_emails,
    get_active_provider,
    get_mail_source_state,
    send_active_email,
)
from app.models import (
    EmailCreate,
    EmailRecord,
    EscalationUpdate,
    KnowledgeDocument,
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeDocumentUpdate,
    KnowledgeDocumentVersion,
    KnowledgeHit,
    KnowledgeSearchRequest,
    MailSourceConfigInput,
    MailOAuthStartResult,
    MailSourceState,
    MailSourceSwitchResult,
    OperationLog,
    ReviewAction,
)
from app.store import EmailStore
from app.workflow import process_email, regenerate_draft_reply, sanitize_customer_reply

app = FastAPI(title="Customer Email Agent API")
ensure_default_users()

# 本项目的前端是本地 Vite 应用，因此只开放本地开发端口的跨域访问。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = EmailStore()


@app.get("/")
def health() -> dict[str, str]:
    """健康检查接口，用于确认后端服务已启动。"""
    return {"message": "Customer Email Agent API is alive"}


@app.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    """账号密码登录，返回前端后续请求使用的 Bearer Token。"""
    user = authenticate_user(payload.username, payload.password)
    if not user:
        record_operation_log(
            scope="auth",
            action="login_failed",
            title=payload.username,
            summary=f"用户 {payload.username} 登录失败。",
            detail={"username": payload.username},
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")
    record_operation_log(
        scope="auth",
        action="login_success",
        title=user.username,
        summary=f"用户 {user.username} 登录成功。",
        detail={"user_id": user.id, "username": user.username, "role": user.role},
    )
    return LoginResponse(access_token=create_access_token(user), user=to_profile(user))


@app.get("/auth/me", response_model=UserProfile)
def get_me(current_user: CurrentUser) -> UserProfile:
    """返回当前登录用户信息，前端据此控制菜单和按钮权限。"""
    return to_profile(current_user)


# ------------------------- 邮件处理接口 -------------------------


@app.get("/emails", response_model=list[EmailRecord])
def list_emails(
    current_user: CurrentUser,
    provider: str | None = Query(default=None),
) -> list[EmailRecord]:
    """返回当前角色有权查看的邮件列表。

    Agent 发起升级后即完成权限移交，升级中的邮件只对 manager/admin
    可见；主管取消升级后，邮件会重新回到 Agent 的列表中。
    """
    # 默认只展示当前活动邮箱，避免切换邮箱后把 QQ、Gmail、Outlook 的工单
    # 混在同一队列里。provider=all 用于人工查看历史邮件，但不会改变活动邮箱。
    active_provider = get_active_provider()
    selected_provider = active_provider if provider in (None, "", "active") else provider
    if selected_provider not in {"all", "manual", "qq", "outlook", "gmail"}:
        raise HTTPException(status_code=400, detail="不支持的邮件来源筛选条件")

    return [
        email
        for email in store.list()
        if can_user_access_email(current_user.role, email)
        and (selected_provider == "all" or email.provider == selected_provider)
    ]


@app.get("/emails/{email_id}", response_model=EmailRecord)
def get_email(email_id: str, current_user: CurrentUser) -> EmailRecord:
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    ensure_user_can_access_email(current_user.role, email)
    return email


@app.post("/emails/process", response_model=EmailRecord)
def create_and_process_email(payload: EmailCreate, current_user: CurrentUser) -> EmailRecord:
    """创建一封邮件并立即执行 Agent 工作流。"""
    email = store.create(payload)
    processed = process_email(email)
    saved = store.save(processed)
    record_operation_log(
        scope="email",
        action="process_email",
        title=saved.subject,
        summary=f"邮件「{saved.subject}」已完成 Agent 分类、检索和回复草稿生成。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "email_id": saved.id,
            "customer_email": saved.customer_email,
            "category": saved.category,
            "confidence": saved.confidence,
            "status": saved.status,
            "attachment_count": len(saved.attachments),
        },
    )
    return saved


@app.post("/emails/{email_id}/review", response_model=EmailRecord)
def review_email(email_id: str, payload: ReviewAction, current_user: CurrentUser) -> EmailRecord:
    """人工审核邮件草稿。

    审核动作包括通过、要求修改、升级处理和取消升级。每次审核都会写入
    review history，方便前端展示人工操作记录。取消升级仅允许 manager/admin。
    """
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    ensure_user_can_access_email(current_user.role, email)
    if payload.action == "undo_escalate" and current_user.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="只有客服主管或系统管理员可以取消升级处理")
    if payload.action in {"approve", "revise"} and has_active_escalation(email):
        raise HTTPException(status_code=400, detail="请先取消升级，或由客服主管处理升级工单后再审核")

    if payload.action == "approve":
        email.status = "ready_to_send"
        email.review_note = payload.note or default_review_note(email.detected_language, "approve")
    elif payload.action == "revise":
        email.status = "needs_revision"
        email.review_note = payload.note or default_review_note(email.detected_language, "revise")
        if payload.revised_reply:
            email.draft_reply = payload.revised_reply
    elif payload.action == "escalate":
        email.status = "escalated"
        email.review_note = payload.note or default_review_note(email.detected_language, "escalate")
    else:
        email.status = "human_review"
        email.review_note = payload.note or default_review_note(email.detected_language, "undo_escalate")

    saved = store.save(email)
    if payload.action == "escalate":
        ticket = store.open_escalation_ticket(email_id, reason=email.review_note, created_by=current_user.username)
        saved = store.get(email_id) or saved
        record_operation_log(
            scope="email",
            action="open_escalation_ticket",
            title=email.subject,
            summary=f"邮件「{email.subject}」已生成升级工单，等待客服主管处理。",
            detail={
                "operator": current_user.username,
                "operator_role": current_user.role,
                "email_id": email.id,
                "ticket_id": ticket.id,
                "ticket_status": ticket.status,
                "reason": ticket.reason,
            },
        )
    elif payload.action == "undo_escalate":
        ticket = store.update_escalation_ticket(email_id, action="return_to_review", note=email.review_note, operator=current_user.username)
        saved = store.get(email_id) or saved
        record_operation_log(
            scope="email",
            action="return_escalation_ticket",
            title=email.subject,
            summary=f"邮件「{email.subject}」升级工单已退回审核队列。",
            detail={
                "operator": current_user.username,
                "operator_role": current_user.role,
                "email_id": email.id,
                "ticket_id": ticket.id,
                "ticket_status": ticket.status,
                "note": email.review_note,
            },
        )
    store.record_review(email_id, payload)
    record_operation_log(
        scope="email",
        action=f"review_{payload.action}",
        title=email.subject,
        summary=f"邮件「{email.subject}」审核动作为 {payload.action}，当前状态为 {saved.status}。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "email_id": email.id,
            "customer_email": email.customer_email,
            "action": payload.action,
            "status": saved.status,
            "has_revised_reply": bool(payload.revised_reply),
        },
    )
    return store.get(email_id) or saved


def has_active_escalation(email: EmailRecord) -> bool:
    """判断邮件当前是否仍处在升级处理中。"""
    if email.status == "escalated":
        return True
    return email.escalation_ticket is not None and email.escalation_ticket.status in {"open", "assigned"}


def can_user_access_email(role: str, email: EmailRecord) -> bool:
    """判断角色是否可以访问邮件。

    升级代表 Agent 将处理权正式移交给更高权限角色，因此 Agent 在升级
    有效期间不能通过列表或详情接口继续查看、修改该邮件。
    """
    return role != "agent" or not has_active_escalation(email)


def ensure_user_can_access_email(role: str, email: EmailRecord) -> None:
    """阻止 Agent 绕过前端直接访问已经移交的升级邮件。"""
    if not can_user_access_email(role, email):
        raise HTTPException(status_code=404, detail="Email not found")


@app.post("/emails/{email_id}/escalation", response_model=EmailRecord)
def update_email_escalation(email_id: str, payload: EscalationUpdate, current_user: CurrentUser) -> EmailRecord:
    """客服主管处理升级工单。

    agent 只能发起升级；manager/admin 可以接单、关闭工单或退回审核队列。
    """
    if current_user.role not in {"admin", "manager"}:
        raise HTTPException(status_code=403, detail="Only manager or admin can handle escalation tickets")

    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    ticket = store.update_escalation_ticket(email_id, action=payload.action, note=payload.note, operator=current_user.username)
    if payload.action == "assign":
        email.status = "escalated"
        email.review_note = payload.note or f"升级工单已由 {current_user.username} 接单。"
    elif payload.action == "resolve":
        email.status = "human_review"
        email.review_note = payload.note or "升级工单已处理完成，邮件回到审核队列。"
    else:
        email.status = "human_review"
        email.review_note = payload.note or "升级工单已退回，邮件回到人工审核队列。"

    saved = store.save(email)
    record_operation_log(
        scope="email",
        action=f"escalation_{payload.action}",
        title=email.subject,
        summary=f"邮件「{email.subject}」升级工单执行 {payload.action}，当前工单状态为 {ticket.status}。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "email_id": email.id,
            "ticket_id": ticket.id,
            "ticket_status": ticket.status,
            "note": payload.note,
        },
    )
    return store.get(email_id) or saved


@app.post("/emails/{email_id}/draft/regenerate", response_model=EmailRecord)
def regenerate_email_draft(email_id: str, current_user: CurrentUser) -> EmailRecord:
    """重新生成当前邮件的回复草稿。"""
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    ensure_user_can_access_email(current_user.role, email)
    if email.status in {"new", "irrelevant"} or not email.category or email.category == "other":
        raise HTTPException(status_code=400, detail="Email must be processed as a customer support request before regenerating a draft")

    saved = store.save(regenerate_draft_reply(email))
    record_operation_log(
        scope="email",
        action="regenerate_draft",
        title=email.subject,
        summary=f"邮件「{email.subject}」已重新生成回复草稿。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "email_id": email.id,
            "customer_email": email.customer_email,
            "status": saved.status,
        },
    )
    return store.get(email_id) or saved


@app.get("/mail/source", response_model=MailSourceState)
def get_mail_source(_: CurrentUser) -> MailSourceState:
    """返回当前邮件源及各服务商的非敏感配置状态。"""
    return get_mail_source_state()


@app.put("/mail/source", response_model=MailSourceSwitchResult)
def switch_mail_source(
    payload: MailSourceConfigInput,
    current_user: UserProfile = Depends(require_roles(["admin"])),
) -> MailSourceSwitchResult:
    """测试管理员提交的连接；仅测试成功后保存并切换活动邮件源。"""
    previous = get_active_provider()
    try:
        state = configure_and_activate(payload)
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_operation_log(
        scope="mail",
        action="switch_mail_source",
        title=f"切换到 {PROVIDER_LABELS[payload.provider]}",
        summary=f"邮件源已从 {PROVIDER_LABELS[previous]} 切换到 {PROVIDER_LABELS[payload.provider]}。",
        detail={"operator": current_user.username, "previous_provider": previous, "provider": payload.provider},
    )
    return MailSourceSwitchResult(message="连接测试通过，邮件源已切换。", state=state)


@app.post("/mail/oauth/start", response_model=MailOAuthStartResult)
def start_mail_oauth(
    payload: MailSourceConfigInput,
    _: UserProfile = Depends(require_roles(["admin"])),
) -> MailOAuthStartResult:
    """生成 Gmail 或 Outlook 的授权地址，尚不改动当前活动邮件源。"""
    try:
        return MailOAuthStartResult(authorization_url=begin_oauth(payload))
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/auth/{provider}/callback")
def mail_oauth_callback(
    provider: str,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
) -> RedirectResponse:
    """接收 OAuth 回调，完成授权后回到前端系统设置。"""
    if provider not in {"gmail", "outlook"}:
        return RedirectResponse(url="/?mail_oauth=error&message=unsupported_provider", status_code=302)
    if error or not code or not state:
        # OAuth 服务商通常会在 error_description 中返回可操作的失败原因。
        # 优先透传该字段，避免前端只能看到笼统的 access_denied。
        message = error_description or error or "authorization_cancelled"
        return RedirectResponse(url=f"/?{urlencode({'mail_oauth': 'error', 'provider': provider, 'message': message})}", status_code=302)
    try:
        complete_oauth(provider, code=code, state=state)  # type: ignore[arg-type]
    except MailClientConfigError as exc:
        return RedirectResponse(
            url=f"/?{urlencode({'mail_oauth': 'error', 'provider': provider, 'message': str(exc)[:240]})}",
            status_code=302,
        )
    return RedirectResponse(url=f"/?{urlencode({'mail_oauth': 'success', 'provider': provider})}", status_code=302)


@app.post("/mail/import")
@app.post("/mail/qq/import", include_in_schema=False)
def import_mail(background_tasks: BackgroundTasks, current_user: CurrentUser, limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """从当前活动邮件源同步新邮件并加入后台处理队列。

    接口返回时不一定已经完成 Agent 分析，因为每封新邮件会通过 BackgroundTasks
    异步调用 ``process_imported_email``。这样前端可以先看到“已同步，处理中”，
    再轮询刷新最终分类结果。
    """
    try:
        provider = get_active_provider()
        known_message_ids = store.list_provider_message_ids(provider, limit=300)
        provider, imported = fetch_active_emails(limit=limit, known_message_ids=known_message_ids)
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    queued_emails: list[EmailRecord] = []
    skipped_count = 0
    for item in imported:
        if store.exists_provider_message(provider, item.provider_message_id):
            skipped_count += 1
            continue
        email = EmailRecord(
            **item.payload.model_dump(),
            provider=provider,
            provider_message_id=item.provider_message_id,
        )
        email.status = "new"
        email.review_note = "已同步，等待 Agent 后台处理。"
        email.processing_status = "queued"
        email.processing_stage = "queued"
        email.processing_progress = 5
        email.processing_message = "邮件已入队，等待 Agent 处理"
        saved = store.save(email)
        queued_emails.append(saved)
        background_tasks.add_task(process_imported_email, saved.id)

    record_operation_log(
        scope="mail",
        action="import_mail",
        title=f"{PROVIDER_LABELS[provider]}同步",
        summary=f"{PROVIDER_LABELS[provider]}同步完成，新增 {len(queued_emails)} 封邮件进入后台 Agent 处理。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "requested_limit": limit,
            "queued_count": len(queued_emails),
            "skipped_count": skipped_count,
            "provider": provider,
        },
    )

    return {
        "queued_count": len(queued_emails),
        "skipped_count": skipped_count,
        "emails": queued_emails,
    }


def process_imported_email(email_id: str) -> None:
    """后台处理从当前邮件源导入的邮件。"""
    email = store.get(email_id)
    if not email:
        return
    try:
        processed = process_email(email, progress_callback=store.save)
        saved = store.save(processed)
    except Exception as exc:
        failed = store.get(email_id) or email
        record_operation_log(
            scope="email",
            action="process_imported_email_failed",
            title=failed.subject,
            summary=f"邮件「{failed.subject}」后台 Agent 处理失败。",
            detail={
                "email_id": failed.id,
                "customer_email": failed.customer_email,
                "processing_stage": failed.processing_stage,
                "error_type": type(exc).__name__,
            },
        )
        return
    record_operation_log(
        scope="email",
        action="process_imported_email",
        title=saved.subject,
        summary=f"邮件「{saved.subject}」已完成后台 Agent 处理。",
        detail={
            "email_id": saved.id,
            "customer_email": saved.customer_email,
            "category": saved.category,
            "confidence": saved.confidence,
            "status": saved.status,
        },
    )


@app.delete("/mail/qq/corrupted")
def delete_corrupted_qq_mail(current_user: CurrentUser) -> dict[str, int]:
    """清理历史乱码邮件记录，方便重新从邮箱同步。"""
    deleted_count = store.delete_corrupted_provider_messages("qq")
    record_operation_log(
        scope="mail",
        action="delete_corrupted_qq_mail",
        title="QQ 邮箱乱码清理",
        summary=f"已清理 {deleted_count} 封已损坏编码的 QQ 邮件记录，可重新同步原邮件。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "deleted_count": deleted_count,
            "provider": "qq",
        },
    )
    return {"deleted_count": deleted_count}


@app.post("/emails/{email_id}/send", response_model=EmailRecord)
def send_email_reply(email_id: str, current_user: CurrentUser) -> EmailRecord:
    """发送经过人工确认的回复。

    只有 ``ready_to_send`` 状态的邮件允许发送，防止低置信度或未审核草稿被误发。
    """
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    ensure_user_can_access_email(current_user.role, email)
    if not email.draft_reply:
        raise HTTPException(status_code=400, detail="Draft reply is empty")
    if email.status != "ready_to_send":
        raise HTTPException(status_code=400, detail="Email must be approved before sending")
    if current_user.role == "agent" and has_escalation_history(email):
        raise HTTPException(status_code=403, detail="升级处理过的邮件需要客服主管或管理员发送")

    try:
        email.draft_reply = sanitize_customer_reply(email.draft_reply)
        provider = send_active_email(
            to_address=email.customer_email,
            subject=f"Re: {email.subject}",
            body=email.draft_reply,
        )
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    email.status = "sent"
    label = PROVIDER_LABELS[provider]
    email.review_note = f"已通过{label}发送回复。" if email.detected_language == "zh" else f"Reply sent via {label}."
    saved = store.save(email)
    record_operation_log(
        scope="mail",
        action="send_reply",
        title=email.subject,
        summary=f"邮件「{email.subject}」已通过{label}发送回复。",
        detail={
            "operator": current_user.username,
            "operator_role": current_user.role,
            "email_id": email.id,
            "to": email.customer_email,
            "subject": f"Re: {email.subject}",
            "status": saved.status,
            "provider": provider,
        },
    )
    return saved


def has_escalation_history(email: EmailRecord) -> bool:
    """判断邮件是否仍需要按升级邮件限制发送。

    manager/admin 取消升级后，最后一次升级相关动作会是
    ``undo_escalate``，邮件恢复为普通审核邮件，允许 Agent 继续处理。
    只有最后一次相关动作仍是 ``escalate``，或存在有效升级工单时，
    才继续限制 Agent 发送。
    """
    escalation_actions = [action for action in email.review_actions if action.action in {"escalate", "undo_escalate"}]
    if escalation_actions:
        return escalation_actions[-1].action == "escalate"
    return email.escalation_ticket is not None and email.escalation_ticket.status in {"open", "assigned", "resolved"}


@app.get("/knowledge/documents", response_model=list[KnowledgeDocument])
def get_knowledge_documents(_: UserProfile = Depends(require_roles(["admin", "manager"]))) -> list[KnowledgeDocument]:
    """查询知识库文档列表。"""
    return list_knowledge_documents()


@app.get("/operation-logs", response_model=list[OperationLog])
def get_operation_logs(_: UserProfile = Depends(require_roles(["admin", "manager"])), limit: int = Query(default=100, ge=1, le=300)) -> list[OperationLog]:
    """查询运行日志。"""
    return list_operation_logs(limit=limit)


@app.delete("/operation-logs/cleanup")
def cleanup_logs(
    retention_days: int = Query(default=180, ge=7, le=730),
    scope: str | None = Query(default=None),
    current_user=Depends(require_roles(["admin"])),
) -> dict[str, int]:
    """清理过期运行日志，避免日志无限增长。"""
    deleted = cleanup_operation_logs(retention_days=retention_days, scope=scope)
    record_operation_log(
        scope="system",
        action="cleanup_operation_logs",
        title="清理运行日志",
        summary=f"用户 {current_user.username} 清理了 {deleted} 条运行日志。",
        detail={"operator": current_user.username, "operator_role": current_user.role, "deleted": deleted, "retention_days": retention_days, "scope": scope},
    )
    return {"deleted": deleted}


@app.delete("/emails/workflow-steps/cleanup")
def cleanup_email_workflow_steps(retention_days: int = Query(default=30, ge=7, le=365), current_user=Depends(require_roles(["admin"]))) -> dict[str, int]:
    """清理邮件 Agent 执行轨迹。"""
    deleted = store.cleanup_workflow_steps(retention_days=retention_days)
    record_operation_log(
        scope="system",
        action="cleanup_workflow_steps",
        title="清理邮件 Agent 轨迹",
        summary=f"用户 {current_user.username} 清理了 {deleted} 条邮件 Agent 轨迹。",
        detail={"operator": current_user.username, "operator_role": current_user.role, "deleted": deleted, "retention_days": retention_days},
    )
    return {"deleted": deleted}


@app.post("/knowledge/documents", response_model=KnowledgeDocument)
def create_knowledge_base_document(payload: KnowledgeDocumentCreate, current_user=Depends(require_roles(["admin", "manager"]))) -> KnowledgeDocument:
    """手动创建知识库文档。"""
    document = create_knowledge_document(payload)
    record_operation_log("audit", "knowledge_create_by_user", document.title, f"用户 {current_user.username} 创建了知识库文档。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document.id})
    return document


@app.get("/knowledge/documents/{document_id}", response_model=KnowledgeDocumentDetail)
def get_knowledge_base_document(document_id: str, _: UserProfile = Depends(require_roles(["admin", "manager"]))) -> KnowledgeDocumentDetail:
    document = get_knowledge_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return document


@app.get("/knowledge/documents/{document_id}/versions", response_model=list[KnowledgeDocumentVersion])
def get_knowledge_base_document_versions(document_id: str, _: UserProfile = Depends(require_roles(["admin", "manager"]))) -> list[KnowledgeDocumentVersion]:
    try:
        return list_knowledge_document_versions(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/knowledge/documents/{document_id}/versions/{version_id}/restore", response_model=KnowledgeDocument)
def restore_knowledge_base_document_version(document_id: str, version_id: str, current_user=Depends(require_roles(["admin", "manager"]))) -> KnowledgeDocument:
    try:
        document = restore_knowledge_document_version(document_id, version_id)
        record_operation_log("audit", "knowledge_restore_by_user", document.title, f"用户 {current_user.username} 回退了知识库版本。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id, "version_id": version_id})
        return document
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/knowledge/documents/{document_id}/versions/{version_id}")
def delete_knowledge_base_document_version(document_id: str, version_id: str, current_user=Depends(require_roles(["admin", "manager"]))) -> dict[str, str]:
    try:
        delete_knowledge_document_version(document_id, version_id)
        record_operation_log("audit", "knowledge_version_delete_by_user", document_id, f"用户 {current_user.username} 删除了知识库历史版本。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id, "version_id": version_id})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.put("/knowledge/documents/{document_id}", response_model=KnowledgeDocument)
def update_knowledge_base_document(document_id: str, payload: KnowledgeDocumentUpdate, current_user=Depends(require_roles(["admin", "manager"]))) -> KnowledgeDocument:
    try:
        document = update_knowledge_document(document_id, payload)
        record_operation_log("audit", "knowledge_update_by_user", document.title, f"用户 {current_user.username} 修改了知识库文档。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id})
        return document
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/knowledge/documents/{document_id}/reindex", response_model=KnowledgeDocument)
def reindex_knowledge_base_document(document_id: str, current_user=Depends(require_roles(["admin", "manager"]))) -> KnowledgeDocument:
    try:
        document = reindex_knowledge_document(document_id)
        record_operation_log("audit", "knowledge_reindex_by_user", document.title, f"用户 {current_user.username} 重新索引了知识库文档。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id})
        return document
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/knowledge/documents/{document_id}")
def delete_knowledge_base_document(document_id: str, current_user=Depends(require_roles(["admin", "manager"]))) -> dict[str, str]:
    try:
        delete_knowledge_document(document_id)
        record_operation_log("audit", "knowledge_delete_by_user", document_id, f"用户 {current_user.username} 删除了知识库文档。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/knowledge/documents/upload", response_model=KnowledgeDocument)
async def upload_knowledge_base_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    force_weak_duplicate: bool = Query(default=False),
    current_user=Depends(require_roles(["admin", "manager"])),
) -> KnowledgeDocument:
    content = await file.read()
    try:
        document = create_knowledge_document_upload_job_with_duplicate_policy(
            file.filename or "knowledge.md",
            content,
            force_weak_duplicate=force_weak_duplicate,
        )
        background_tasks.add_task(index_knowledge_document, document.id)
        record_operation_log("audit", "knowledge_upload_by_user", document.title, f"用户 {current_user.username} 上传了知识库文件。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document.id, "filename": file.filename})
        return document
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"kind": "weak_duplicate", "message": str(exc)}) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("强去重命中") else 400
        kind = "strong_duplicate" if status_code == 409 else "upload_error"
        raise HTTPException(status_code=status_code, detail={"kind": kind, "message": str(exc)}) from exc


@app.put("/knowledge/documents/{document_id}/upload", response_model=KnowledgeDocument)
async def upload_knowledge_base_document_revision(
    document_id: str,
    file: UploadFile = File(...),
    force_weak_duplicate: bool = Query(default=False),
    current_user=Depends(require_roles(["admin", "manager"])),
) -> KnowledgeDocument:
    content = await file.read()
    try:
        document = update_knowledge_document_from_upload(
            document_id,
            file.filename or "knowledge.md",
            content,
            force_weak_duplicate=force_weak_duplicate,
        )
        record_operation_log("audit", "knowledge_revision_upload_by_user", document.title, f"用户 {current_user.username} 上传了知识库新版本。", {"operator": current_user.username, "operator_role": current_user.role, "document_id": document_id, "filename": file.filename})
        return document
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"kind": "weak_duplicate", "message": str(exc)}) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("强去重命中") else 400
        kind = "strong_duplicate" if status_code == 409 else "upload_error"
        raise HTTPException(status_code=status_code, detail={"kind": kind, "message": str(exc)}) from exc


@app.post("/knowledge/ingest", response_model=list[KnowledgeDocument])
def ingest_knowledge_base(current_user=Depends(require_roles(["admin", "manager"]))) -> list[KnowledgeDocument]:
    record_operation_log("audit", "knowledge_ingest_by_user", "初始化知识库", f"用户 {current_user.username} 触发了内置知识库入库。", {"operator": current_user.username, "operator_role": current_user.role})
    return ingest_knowledge_documents()


@app.post("/knowledge/search", response_model=list[KnowledgeHit])
def search_knowledge_base(payload: KnowledgeSearchRequest, _: UserProfile = Depends(require_roles(["admin", "manager"]))) -> list[KnowledgeHit]:
    return search_knowledge(payload.query, category=payload.category, limit=payload.limit)


def default_review_note(language: str, action: str) -> str:
    if language == "zh":
        return {
            "approve": "审核通过，可以发送。",
            "revise": "审核要求修改回复草稿。",
            "escalate": "已升级给人工专员处理。",
            "undo_escalate": "已撤销升级，邮件回到人工审核队列。",
        }[action]
    return {
        "approve": "Approved by reviewer.",
        "revise": "Reviewer requested changes.",
        "escalate": "Escalated to a human specialist.",
        "undo_escalate": "Escalation undone; the email is back in human review.",
    }[action]
