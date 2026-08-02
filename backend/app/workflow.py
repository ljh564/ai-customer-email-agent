"""邮件处理 Agent 的 LangGraph 编排层。

这个模块是整个系统的“决策中枢”：一封邮件进入后，会被包装成
``EmailWorkflowState``，再沿着 LangGraph 节点依次经过低成本预处理、
非客服过滤、语义分析、知识库检索、回复草稿生成和审核决策。

设计上这里刻意把“便宜的规则判断”和“昂贵的 LLM/RAG 调用”拆开：
1. 先用本地规则做语言、风险、非客服场景的快速判断，尽量避免无意义调用模型。
2. 确认为客服邮件后，再进入语义分析和 RAG 检索。
3. 只有需要回复的邮件才生成草稿，并根据风险与置信度决定是否进入人工审核。

面试表达时可以把它理解为：LangGraph 负责流程编排，每个 node 负责一个稳定、
可测试、可替换的 Agent 子能力。
"""

import re
from datetime import datetime
from typing import Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.knowledge import retrieve_knowledge
from app.llm_client import (
    LLMClientError,
    SemanticAnalysisResult,
    analyze_email_with_llm,
    generate_reply_draft_with_llm,
    is_llm_configured,
)
import os
import time

from app.models import AgentMetrics, EmailRecord, WorkflowStep
from app.risk import assess_email_risk, contains_any, merge_llm_risk


WorkflowRoute = Literal["relevant", "irrelevant"]
ProgressCallback = Callable[[EmailRecord], None]


class EmailWorkflowState(TypedDict, total=False):
    """在 LangGraph 节点之间传递的可变状态。

    每个节点都不直接返回一个全新的业务对象，而是返回它改动过的字段。
    LangGraph 会把这些 partial update 合并回当前 state。这样做的好处是：
    - 节点之间边界清晰，方便单独测试和替换。
    - 可以在流程中插入新的 Agent 节点，而不需要改动所有上下游代码。
    - 出错时可以根据 state 中的 email.steps 还原执行轨迹。
    """

    email: EmailRecord
    use_llm: bool
    semantic_is_support_request: bool
    progress_callback: ProgressCallback


def update_processing_progress(
    state: EmailWorkflowState,
    *,
    stage: str,
    progress: int,
    message: str,
    status: Literal["queued", "running", "completed", "failed"] = "running",
) -> None:
    """更新并持久化工作流进度。

    进度在耗时节点调用前就会写入数据库，因此前端展示的是实际执行阶段，
    而不是按时间递增的模拟百分比。
    """
    email = state["email"]
    email.processing_status = status
    email.processing_stage = stage
    email.processing_progress = max(0, min(progress, 100))
    email.processing_message = message
    callback = state.get("progress_callback")
    if callback:
        callback(email)


def process_email(
    email: EmailRecord,
    use_llm: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> EmailRecord:
    """执行完整邮件处理流程。

    ``use_llm`` 主要用于测试或降级场景：当模型平台不可用时，系统仍然可以依靠
    规则和模板完成基础分类，保证服务不会因为外部模型失败而整体不可用。
    """
    email.steps = []
    email.agent_metrics = AgentMetrics()
    email.processing_status = "running"
    email.processing_stage = "preprocess"
    email.processing_progress = 8
    email.processing_message = "正在识别语言、附件和规则信号"
    email.processing_started_at = datetime.utcnow()
    email.processing_finished_at = None
    state: EmailWorkflowState = {
        "email": email,
        "use_llm": use_llm,
    }
    if progress_callback:
        state["progress_callback"] = progress_callback
        progress_callback(email)

    try:
        result = email_workflow.invoke(state)
        processed = result["email"]
        processed.processing_status = "completed"
        processed.processing_stage = "completed"
        processed.processing_progress = 100
        processed.processing_message = "Agent 处理完成"
        processed.processing_finished_at = datetime.utcnow()
        if progress_callback:
            progress_callback(processed)
        return processed
    except Exception as exc:
        email.processing_status = "failed"
        email.processing_progress = max(email.processing_progress, 5)
        email.processing_message = f"{email.processing_message}失败：{type(exc).__name__}"
        email.processing_finished_at = datetime.utcnow()
        if progress_callback:
            progress_callback(email)
        raise


def build_email_context(email: EmailRecord) -> str:
    """把邮件主题、正文和附件预览拼成统一上下文。

    后续的分类、RAG 查询和 LLM 生成都使用同一个上下文，避免“正文看到了，
    附件没看到”导致判断不一致。附件只放入解析后的预览文本，不直接塞原文件，
    这是为了控制 token 成本和模型输入长度。
    """
    attachment_lines: list[str] = []
    for attachment in email.attachments:
        meta = f"{attachment.filename} ({attachment.content_type or 'unknown'}, {attachment.size_bytes} bytes)"
        if attachment.text_preview:
            attachment_lines.append(f"{meta}: {attachment.text_preview}")
        else:
            attachment_lines.append(meta)

    attachment_context = "\n".join(attachment_lines)
    if not attachment_context:
        return f"{email.subject}\n\n{email.body}"
    return f"{email.subject}\n\n{email.body}\n\nAttachments:\n{attachment_context}"


def preprocess_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """低成本预处理节点。

    这里不调用 LLM，只做三件事：
    1. 判断邮件语言，决定后续回复和提示词使用中文还是英文。
    2. 命中一些强信号，例如退款、取消、法律风险、重复联系等。
    3. 标记是否有附件，为后续风险等级和人工审核提供依据。

    这个节点是成本控制的第一道闸门，适合放稳定、可解释、召回优先的规则。
    """
    email = state["email"]
    text = build_email_context(email).lower()
    is_chinese = contains_chinese(text)
    email.detected_language = "zh" if is_chinese else "en"

    preprocess_rules = [
        ({"refund", "退款", "退费"}, "payment_change"),
        ({"cancel", "取消"}, "churn_risk"),
        ({"legal action", "legal threat", "lawyer", "sue", "法律行动", "律师", "起诉"}, "legal_risk"),
        ({"third email", "第三次", "多次"}, "repeated_contact"),
        ({"blocked", "阻塞", "无法使用"}, "business_blocked"),
    ]
    email.preprocessing_flags = [
        flag for terms, flag in preprocess_rules if contains_any(text, terms)
    ]
    if email.attachments:
        email.preprocessing_flags.append("has_attachment")

    email.steps.append(
        WorkflowStep(
            name="Preprocess email",
            status="complete",
            summary=f"Detected language: {email.detected_language}, flags: {len(email.preprocessing_flags)}",
            detail="Low-cost preprocessing detects language, strong policy signals, repeated contact, and business-blocking hints before any LLM call.",
            confidence=0.95,
        )
    )
    update_processing_progress(
        state,
        stage="relevance_gate",
        progress=15,
        message="预处理完成，正在判断是否属于客服场景",
    )
    return {"email": email}


def semantic_analysis_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """语义分析节点：识别邮件类别、置信度和风险等级。

    该节点优先调用 LLM 做更灵活的语义判断；如果 LLM 不可用或调用失败，
    会回退到本地规则分类。这样既能在真实环境中获得较好的理解能力，
    又能在模型欠费、网络失败或本地演示时保持系统可运行。

    这里统计的 token 和调用次数会写入 ``email.agent_metrics``，
    前端的“Agent 成本与 token 监控”就是基于这些指标展示的。
    """
    email = state["email"]
    email_context = build_email_context(email)
    text = email_context.lower()
    llm_result = None
    llm_error = ""
    if state.get("use_llm", True):
        llm_api_configured = bool(os.getenv("LLM_API_KEY") or os.getenv("SILICONFLOW_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
        if llm_api_configured:
            email.agent_metrics.llm_calls += 1
            email.agent_metrics.semantic_llm_calls += 1
            email.agent_metrics.input_tokens += estimate_tokens(email_context) + 260
        try:
            llm_result = analyze_email_with_llm(
                subject=email.subject,
                body=email_context,
                detected_language=email.detected_language,
                preprocessing_flags=email.preprocessing_flags,
            )
        except LLMClientError as exc:
            llm_error = str(exc)

    if llm_result:
        apply_llm_analysis(email, llm_result)
        rule_risk = assess_email_risk(
            email_context,
            email.category,
            email.preprocessing_flags,
            email.detected_language,
        )
        llm_risk_level = email.risk_level
        email.risk_level, email.risk_flags = merge_llm_risk(
            email.risk_level,
            email.risk_flags,
            rule_risk,
        )
        email.should_escalate = email.risk_level == "high"
        email.priority = email.risk_level
        if email.risk_level != llm_risk_level:
            guardrail_note = (
                "确定性风险护栏检测到不可降级的强风险信号，已提高最终风险等级。"
                if email.detected_language == "zh"
                else "The deterministic risk guardrail found a non-downgradable signal and raised the final risk level."
            )
            email.analysis_reason = f"{email.analysis_reason} {guardrail_note}".strip()
        email.agent_metrics.output_tokens += estimate_tokens(email.analysis_reason) + sum(estimate_tokens(flag) for flag in email.risk_flags) + 30
        update_estimated_cost(email)
        detail = email.analysis_reason
        if email.preprocessing_flags:
            detail = f"{detail} Preprocessing flags: {', '.join(email.preprocessing_flags)}."
        email.steps.append(
            WorkflowStep(
                name="Semantic analysis",
                status="warning" if email.should_escalate else "complete",
                summary=f"{email.category.replace('_', ' ')} / {email.risk_level} risk / {email.confidence:.0%} confidence",
                detail=detail,
                confidence=email.confidence,
            )
        )
        update_processing_progress(
            state,
            stage="semantic_relevance_gate",
            progress=45,
            message="语义分析完成，正在确认客服意图",
        )
        return {"email": email, "semantic_is_support_request": llm_result.is_support_request}

    category, confidence, matched = classify_semantically(text)
    risk = assess_email_risk(
        email_context,
        category,
        email.preprocessing_flags,
        email.detected_language,
    )

    email.category = category
    email.confidence = confidence
    email.risk_level = risk.level
    email.risk_flags = list(risk.flags)
    email.should_escalate = risk.level == "high"
    email.priority = risk.level
    email.analysis_reason = build_analysis_reason(email, matched)
    if llm_error:
        email.analysis_reason = f"{email.analysis_reason} LLM fallback reason: {llm_error}"

    email.steps.append(
        WorkflowStep(
            name="Semantic analysis",
            status="warning" if email.should_escalate else "complete",
            summary=f"{category.replace('_', ' ')} / {risk.level} risk / {confidence:.0%} confidence",
            detail=email.analysis_reason,
            confidence=confidence,
        )
    )
    update_processing_progress(
        state,
        stage="semantic_relevance_gate",
        progress=45,
        message="语义分析完成，正在确认客服意图",
    )
    return {"email": email}


def semantic_relevance_gate_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """语义分析后的二次非客服门控。

    第一层 ``relevance_gate`` 发生在 LLM 之前，用于省成本；但有些平台营销、
    安全通知或系统提醒会包含 ``support``、``access``、``login`` 等泛化词，
    仅靠前置规则可能被误放行。语义分析后如果已经判断出 category=other，
    且 reason/risk_flags 指向“营销、通知、无客户问题”，这里会再次拦截，
    防止继续进入 RAG 和回复生成。
    """
    email = state["email"]
    semantic_decision = state.get("semantic_is_support_request")
    if semantic_decision is False:
        mark_irrelevant_email(
            email,
            "Semantic analysis explicitly classified this message as not being a customer support request.",
            step_name="Semantic relevance gate",
            confidence=max(0.9, email.confidence),
        )
        update_processing_progress(
            state,
            stage="completed",
            progress=100,
            message="已识别为非客服邮件，无需生成回复",
            status="completed",
        )
        return {"email": email}

    should_filter, reason = should_filter_after_semantic_analysis(email)
    if not should_filter:
        email.steps.append(
            WorkflowStep(
                name="Semantic relevance gate",
                status="complete",
                summary="Customer support intent retained",
                detail="Semantic analysis did not find a strong non-support signal, so the email can continue to RAG retrieval.",
                confidence=0.84,
            )
        )
        update_processing_progress(
            state,
            stage="retrieve",
            progress=50,
            message="客服意图确认完成，正在检索知识库",
        )
        return {"email": email}

    mark_irrelevant_email(email, reason, step_name="Semantic relevance gate", confidence=0.9)
    update_processing_progress(
        state,
        stage="completed",
        progress=100,
        message="已识别为非客服邮件，无需生成回复",
        status="completed",
    )
    return {"email": email}


def relevance_gate_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """非客服邮件过滤节点。

    这个节点在 RAG 和回复生成之前执行。如果邮件只是平台通知、安全提醒、
    验证码、营销订阅等非客服场景，就直接标记为 ``irrelevant``。
    被过滤的邮件仍会保留在系统里，方便人工复核，但不会进入知识库检索和
    回复草稿生成，从而避免浪费 LLM token。
    """
    email = state["email"]
    relevant, reason = is_customer_support_request(email)
    if relevant:
        email.steps.append(
            WorkflowStep(
                name="Relevance gate",
                status="complete",
                summary="Customer support request",
                detail="The email contains a support intent and can continue through RAG retrieval and draft generation.",
                confidence=0.86,
            )
        )
        update_processing_progress(
            state,
            stage="semantic_analysis",
            progress=25,
            message="客服场景初筛完成，正在进行语义分析",
        )
        return {"email": email}

    mark_irrelevant_email(email, reason, step_name="Relevance gate", confidence=0.88)
    update_processing_progress(
        state,
        stage="completed",
        progress=100,
        message="已识别为非客服邮件，无需生成回复",
        status="completed",
    )
    return {"email": email}


def route_after_relevance(state: EmailWorkflowState) -> WorkflowRoute:
    """根据非客服过滤结果决定 LangGraph 后续路径。"""
    return "irrelevant" if state["email"].status == "irrelevant" else "relevant"


def mark_irrelevant_email(email: EmailRecord, reason: str, *, step_name: str, confidence: float) -> None:
    """统一把邮件标记为非客服邮件，并清空后续回复相关结果。"""
    email.status = "irrelevant"
    email.category = "other"
    email.priority = "low"
    email.risk_level = "low"
    email.risk_flags = []
    email.should_escalate = False
    email.knowledge_hits = []
    email.draft_reply = ""
    is_chinese_email = contains_chinese(build_email_context(email))
    email.review_note = "非客服业务邮件，已拦截，不生成回复。" if is_chinese_email else "Non-support email filtered out. No reply draft generated."
    email.steps.append(
        WorkflowStep(
            name=step_name,
            status="blocked",
            summary="Non-support email filtered out",
            detail=reason,
            confidence=confidence,
        )
    )


def is_customer_support_request(email: EmailRecord) -> tuple[bool, str]:
    """判断邮件是否属于客服处理范围。

    这里采用“黑名单通知信号 + 白名单客户诉求信号”的组合策略：
    - 如果发件人或正文明显来自平台通知、验证码、安全提醒，则倾向于过滤。
    - 如果正文明确出现“退款、无法登录、账单、套餐咨询”等客户诉求，则放行。
    - 当二者冲突时，优先保留人工可复核的客服邮件，避免误杀真实客户请求。
    """
    text = build_email_context(email).lower()
    sender = f"{email.customer_name} {email.customer_email}".lower()
    platform_senders = {
        "facebook", "facebookmail.com", "meta", "github", "google", "microsoft",
        "openai", "chatgpt", "email.openai.com", "nvidia", "ngc", "aws", "amazon",
        "stripe", "paypal", "apple", "icloud", "adobe", "atlassian", "slack",
        "notification", "notifications", "no-reply", "noreply", "donotreply",
        "newsletter", "updates", "qq邮箱团队",
    }
    platform_notification_terms = {
        "unsubscribe", "no-reply", "noreply", "notification", "notifications", "unread",
        "new notification", "security alert", "login alert", "new login", "verification code",
        "sudo email verification code", "email_forward_notice", "privacy", "newsletter",
        "marketing", "promotion", "weekly update", "product update", "release notes",
        "recommendation", "digest", "community update", "retiring", "deprecation",
        "terms of service", "service terms", "privacy settings", "recommendation available",
        "new recommendation", "default directory",
        "welcome", "welcome to", "get started", "getting started", "onboarding",
        "activate your account", "complete your profile", "explore features", "product tour",
        "oauth application", "third-party oauth", "first-party github oauth",
        "authorized to access your account", "security events", "security-log",
        "settings/security-log", "do not forward this email", "facebook.com",
        "github.com/settings", "github.com/contact", "meta platforms",
        "退订", "动态更新", "新通知", "条新通知", "查看通知", "通知中心",
        "服务条款", "隐私设置", "更新后的", "推荐", "营销", "推广", "周报", "月报",
        "产品更新", "版本更新", "功能更新", "摘要", "即将停用", "退役", "下线",
        "登录提醒", "安全提醒", "验证码", "请勿转发", "如果不希望再收到", "了解详情",
    }
    direct_customer_request_terms = {
        "please help", "can you help", "i need help", "we need help", "need help",
        "please refund", "refund request", "invoice question", "billing issue",
        "cannot log in", "can't log in", "unable to access", "failed to access",
        "duplicate charge", "charged twice", "pro plan", "workspace issue",
        "请问", "请帮", "帮我", "协助", "需要帮助", "无法", "不能", "失败",
        "退款", "退费", "发票", "账单", "重复扣费", "重复收费", "套餐", "工作区",
    }
    strong_business_request_terms = {
        "please refund", "refund request", "invoice question", "billing issue",
        "cannot log in", "can't log in", "unable to access", "failed to access",
        "duplicate charge", "charged twice", "pro plan", "workspace issue",
        "退款", "退费", "发票", "账单", "重复扣费", "重复收费", "套餐", "工作区",
        "无法登录", "不能登录", "访问失败", "权限管理", "审计日志",
    }
    security_notification_terms = {
        "oauth application", "third-party oauth", "first-party github oauth",
        "authorized to access your account", "security events", "security-log",
        "settings/security-log", "verification code", "sudo email verification code",
        "new login", "login alert", "security alert", "do not forward this email",
    }

    from_platform = any(term in sender for term in platform_senders)
    has_notification_signal = any(term in text or term in sender for term in platform_notification_terms)
    has_direct_customer_request = any(term in text for term in direct_customer_request_terms)
    has_strong_business_request = any(term in text for term in strong_business_request_terms)
    has_platform_security_signal = from_platform and any(term in text for term in security_notification_terms)
    has_lifecycle_marketing_signal = any(
        term in text
        for term in {
            "welcome", "welcome to", "get started", "getting started", "onboarding",
            "activate your account", "complete your profile", "explore features", "product tour",
        }
    )
    has_unsubscribe_signal = any(
        term in text
        for term in {"unsubscribe", "manage preferences", "email preferences", "opt out"}
    )
    has_policy_update_signal = any(
        term in text
        for term in {"服务条款", "隐私设置", "terms of service", "privacy settings", "new recommendation", "recommendation available"}
    )

    if has_platform_security_signal:
        return False, "Detected platform security notification without a customer support request."
    if has_lifecycle_marketing_signal and has_unsubscribe_signal and not has_strong_business_request:
        return False, "Detected welcome or onboarding email with marketing unsubscribe signals and no concrete customer issue."
    if from_platform and has_policy_update_signal:
        return False, "Detected platform policy, privacy, or recommendation notification without a customer support request."
    if from_platform and not has_strong_business_request:
        return False, "Detected platform sender without a strong customer business request."
    if (from_platform or has_notification_signal) and not has_direct_customer_request:
        return False, "Detected platform notification, security alert, newsletter, or unsubscribe-style email without a direct customer support request."

    support_terms = {
        "refund", "invoice", "billing", "charged", "duplicate", "login", "password", "access",
        "workspace", "cannot", "can't", "failed", "issue", "problem",
        "退款", "退费", "发票", "账单", "扣费", "重复扣费", "登录", "密码", "无法", "不能",
        "帮", "帮助", "问题", "故障", "打不开", "开票", "保修", "订单", "物流",
    }
    notification_terms = {
        "unsubscribe", "no-reply", "noreply", "notification", "security alert", "login alert",
        "new login", "verification code", "email_forward_notice", "privacy", "newsletter",
        "oauth application", "third-party oauth", "authorized to access your account",
        "security events", "security-log", "settings/security-log", "do not forward this email",
        "退订", "动态更新", "新通知", "登录提醒", "安全提醒", "验证码", "请勿转发", "如果不希望再收到",
        "了解详情", "meta platforms", "facebook.com", "github", "google", "microsoft",
    }
    has_support_intent = any(term in text for term in support_terms)
    has_notification_signal = any(term in text or term in sender for term in notification_terms | platform_senders)

    if has_platform_security_signal:
        return False, "Detected platform security notification without a customer support request."
    if has_notification_signal and not has_support_intent:
        return False, "Detected platform notification, security alert, newsletter, or unsubscribe-style email without a customer support request."
    if from_platform and not has_support_intent:
        return False, "Detected platform sender without a customer support intent."
    if email.category == "other" and email.confidence < 0.7 and not has_support_intent:
        return False, "The email does not contain a clear customer support intent."
    return True, "The email contains customer support intent."


def should_filter_after_semantic_analysis(email: EmailRecord) -> tuple[bool, str]:
    """根据语义分析结果判断是否应二次拦截非客服邮件。"""
    email_context = build_email_context(email).lower()
    semantic_text = f"{email.analysis_reason}\n{' '.join(email.risk_flags)}".lower()
    text = f"{email_context}\n{semantic_text}"
    sender = f"{email.customer_name} {email.customer_email}".lower()
    non_support_signals = {
        "marketing", "promotion", "newsletter", "notification", "platform notification",
        "welcome email", "welcome message", "onboarding email", "getting started email",
        "security alert", "verification code", "oauth application", "policy update",
        "privacy settings", "terms of service", "recommendation", "digest",
        "no customer issue", "without a customer support request", "no customer support",
        "no direct customer request", "openai marketing", "product update",
        "system notification", "does not involve customer support", "not a customer support request",
        "not a support request", "not a support issue", "not a customer request",
        "营销", "推广", "通知", "平台通知", "安全提醒", "验证码", "隐私设置",
        "服务条款", "推荐", "无客户问题", "没有客户问题", "无客服问题",
        "无客户诉求", "没有客户诉求", "非客服", "营销邮件", "系统通知",
        "不涉及客户支持", "不涉及任何客户支持问题", "不涉及客服",
    }
    platform_sender_signals = {
        "noreply", "no-reply", "notification", "openai", "chatgpt", "github", "facebook",
        "google", "microsoft", "nvidia", "ngc", "qq邮箱团队", "newsletter", "updates",
    }
    direct_customer_request_terms = {
        "please refund", "refund request", "charged twice", "duplicate charge", "invoice question",
        "billing issue", "cannot log in", "can't log in", "unable to access", "please help",
        "i need help", "we need help", "请帮", "帮我", "需要帮助", "无法", "不能",
        "退款", "退费", "发票", "账单", "重复扣费", "重复收费", "套餐", "工作区",
    }

    has_non_support_signal = any(signal in text or signal in sender for signal in non_support_signals)
    has_semantic_non_support_signal = any(signal in semantic_text for signal in non_support_signals)
    from_platform = any(signal in sender for signal in platform_sender_signals)
    has_direct_request = any(term in email_context for term in direct_customer_request_terms)
    if email.category == "other" and has_semantic_non_support_signal:
        return True, "Semantic analysis explicitly identified this email as non-support, marketing, notification, or without a customer issue."
    if email.category == "other" and (has_non_support_signal or from_platform) and not has_direct_request:
        return True, "Semantic analysis identified this as a platform notification, marketing email, security alert, or message without a customer support request."
    return False, ""


def retrieve_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """知识库检索节点。

    只有通过非客服过滤的邮件才会到这里。节点会记录 RAG 耗时、embedding 调用次数
    和 token 估算，用于前端成本监控。
    """
    email = state["email"]
    started_at = time.perf_counter()
    email.knowledge_hits = retrieve_knowledge(build_email_context(email), category=email.category)
    email.agent_metrics.rag_latency_ms = round((time.perf_counter() - started_at) * 1000)
    email.agent_metrics.embedding_calls += 1
    email.agent_metrics.embedding_tokens += estimate_tokens(build_email_context(email))
    update_estimated_cost(email)
    confidence = email.knowledge_hits[0].score if email.knowledge_hits else 0.32
    email.steps.append(
        WorkflowStep(
            name="Retrieve knowledge",
            status="complete" if email.knowledge_hits else "warning",
            summary=f"Found {len(email.knowledge_hits)} relevant source(s)",
            detail="RAG retrieval embeds the email intent, filters by category when possible, and ranks knowledge chunks by semantic and keyword relevance.",
            confidence=confidence,
        )
    )
    update_processing_progress(
        state,
        stage="draft",
        progress=65,
        message="知识库检索完成，正在生成回复草稿",
    )
    return {"email": email}


def reliable_knowledge_hits(email: EmailRecord, *, minimum: str = "medium") -> list:
    levels = {"weak": 0, "medium": 1, "strong": 2}
    threshold = levels[minimum]
    return [hit for hit in email.knowledge_hits if levels.get(getattr(hit, "reliability", "weak"), 0) >= threshold]


def draft_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """回复草稿生成节点。

    优先使用 LLM 基于邮件上下文和 Top-K 知识库依据生成回复；如果 LLM 不可用，
    使用结构化模板兜底，保证演示环境仍有可读草稿。
    """
    email = state["email"]
    email.draft_reply, used_llm, draft_error = generate_draft_reply(email)
    grounded_hits = reliable_knowledge_hits(email, minimum="medium")
    if used_llm:
        email.agent_metrics.llm_calls += 1
        email.agent_metrics.draft_llm_calls += 1
        email.agent_metrics.input_tokens += estimate_draft_input_tokens(email)
    email.agent_metrics.output_tokens += estimate_tokens(email.draft_reply)
    update_estimated_cost(email)
    detail = "Draft generation used the customer email, classification result, and retrieved knowledge snippets as LLM context."
    status = "complete"
    confidence = 0.86 if grounded_hits else 0.68
    if not used_llm:
        detail = "LLM draft generation was unavailable, so the system used the structured fallback template."
        if draft_error:
            detail = f"{detail} Fallback reason: {draft_error}"
        status = "warning"
        confidence = 0.72 if grounded_hits else 0.52
    email.steps.append(
        WorkflowStep(
            name="Draft reply",
            status=status,
            summary="Generated customer-ready draft",
            detail=detail,
            confidence=confidence,
        )
    )
    update_processing_progress(
        state,
        stage="review",
        progress=90,
        message="回复草稿已生成，正在执行审核门控",
    )
    return {"email": email}


def regenerate_draft_reply(email: EmailRecord) -> EmailRecord:
    """根据同一封邮件重新生成一版回复。

    重新生成不会重新分类，也不会重新改变风险等级；它只切换回复风格 variant，
    复用已有的 RAG 命中结果，减少重复检索成本。
    """
    variant = next_reply_variant(email)
    email.draft_reply, used_llm, draft_error = generate_draft_reply(email, variant=variant)
    grounded_hits = reliable_knowledge_hits(email, minimum="medium")
    if used_llm:
        email.agent_metrics.llm_calls += 1
        email.agent_metrics.draft_llm_calls += 1
        email.agent_metrics.input_tokens += estimate_draft_input_tokens(email)
    email.agent_metrics.output_tokens += estimate_tokens(email.draft_reply)
    update_estimated_cost(email)
    detail = "The operator requested another LLM-generated draft while keeping the same category, risk level, and knowledge grounding."
    status = "complete"
    confidence = 0.84 if grounded_hits else 0.64
    if not used_llm:
        detail = "The operator requested another draft, but LLM generation was unavailable, so the fallback template was used."
        if draft_error:
            detail = f"{detail} Fallback reason: {draft_error}"
        status = "warning"
        confidence = 0.7 if grounded_hits else 0.5
    email.steps.append(
        WorkflowStep(
            name="Regenerate draft reply",
            status=status,
            summary=f"Generated reply draft variant {variant}",
            detail=detail,
            confidence=confidence,
        )
    )
    return email


def next_reply_variant(email: EmailRecord) -> str:
    """根据历史重新生成次数选择下一种回复风格。"""
    if not email.draft_reply.strip():
        return "default"
    generated_count = sum(1 for step in email.steps if step.name == "Regenerate draft reply")
    return f"alternative-{(generated_count % 3) + 1}"


def build_llm_knowledge_hits(email: EmailRecord) -> list[dict]:
    """把 KnowledgeHit 转成 LLM prompt 所需的紧凑结构。"""
    return [
        {
            "title": hit.title,
            "source": hit.source,
            "snippet": hit.snippet,
            "score": hit.score,
            "semantic_score": hit.semantic_score,
            "keyword_score": hit.keyword_score,
            "category_score": hit.category_score,
            "category": hit.category,
        }
        for hit in reliable_knowledge_hits(email, minimum="medium")[:3]
    ]


def estimate_draft_input_tokens(email: EmailRecord) -> int:
    """估算草稿生成 prompt 的输入 token。"""
    knowledge_context = "\n\n".join(
        f"{hit.title} ({hit.source})\n{hit.snippet}" for hit in reliable_knowledge_hits(email, minimum="medium")[:3]
    )
    prompt_overhead = 520
    return estimate_tokens(build_email_context(email)) + estimate_tokens(knowledge_context) + prompt_overhead


def build_draft_reply(email: EmailRecord, variant: str = "default") -> str:
    reply, _, _ = generate_draft_reply(email, variant=variant)
    return reply


def generate_draft_reply(email: EmailRecord, variant: str = "default") -> tuple[str, bool, str]:
    """生成回复草稿，并返回是否实际调用了 LLM。

    返回三元组：``(草稿内容, 是否使用 LLM, 错误原因)``。
    调用方据此记录成本指标和执行轨迹。
    """
    is_chinese = contains_chinese(build_email_context(email))
    if is_llm_configured():
        try:
            draft = generate_reply_draft_with_llm(
                customer_name=email.customer_name,
                subject=email.subject,
                body=build_email_context(email),
                category=email.category,
                risk_level=email.risk_level,
                detected_language="zh" if is_chinese else "en",
                knowledge_hits=build_llm_knowledge_hits(email),
                variant=variant,
            )
            if draft:
                return sanitize_customer_reply(draft), True, ""
        except LLMClientError as exc:
            return build_structured_draft_reply(email, variant, is_chinese), False, str(exc)

    return build_structured_draft_reply(email, variant, is_chinese), False, "LLM API key is not configured."

    if is_chinese and variant.startswith("alternative"):
        if email.category == "refund":
            bodies = [
                "我已经看到你反馈的重复扣费问题。下一步会先核对订阅和支付记录，确认重复扣款后按退款流程处理，并同步处理进度。",
                "关于这笔疑似重复扣费，我们会先核验付款流水和订阅状态。如果确认重复收费，会按退款流程为你处理。",
                "收到你的退款请求。我们将优先检查本月账单和扣款记录，并在确认后给出退款安排和预计处理时间。",
            ]
        elif email.category == "technical":
            bodies = [
                "这类登录问题通常需要先排查重置链接、验证码或登录会话是否失效。建议重新发起重置并清理缓存；如果仍无法进入，我们会继续升级排查。",
                "建议先重新获取重置链接，并使用无痕窗口或清理缓存后再试一次。如果问题复现，我们会继续检查账号访问状态。",
                "我们会从重置链接有效性、登录会话和账号权限三个方向排查。你可以先尝试重新发送链接，仍失败的话我们会升级处理。",
            ]
        elif email.category == "complaint":
            bodies = [
                "抱歉这个问题已经影响到你的团队。我们会将该工单交给更高优先级的支持人员跟进，优先解决当前阻塞。",
                "很抱歉之前的响应没有达到预期。我们会提高该工单优先级，并安排人工专员继续跟进。",
                "理解这个问题已经造成影响。我们会尽快接手并同步后续处理进度，优先帮助你的团队恢复正常使用。",
            ]
        elif email.category == "billing":
            bodies = [
                "关于发票或账单问题，我会先按账号和账期定位记录。请补充需要开具或核对的账单月份，便于我们尽快处理。",
                "我可以协助核对发票政策和账单记录。请确认对应工作区、账单周期以及需要处理的发票类型。",
                "收到你的发票问题。我们会先检查账单月份和付款记录，再确认是否满足开票或调整条件。",
            ]
        elif email.category == "product_question":
            bodies = [
                "Pro 版本主要面向团队协作、审计管理和更高等级支持。如果你能补充团队规模和使用场景，我可以给出更贴近需求的说明。",
                "Pro 方案适合需要团队空间、权限控制和优先支持的客户。你可以告诉我当前使用场景，我会补充对应能力说明。",
                "我可以根据你的团队规模和集成需求，整理 Pro 版本与当前方案的差异，方便你判断是否升级。",
            ]
        else:
            bodies = [
                "感谢你的来信。我会根据邮件内容确认问题类型，并将其转入合适的客服处理流程。",
                "收到你的反馈。我们会先核对问题背景，再安排对应支持流程继续处理。",
                "谢谢你提供的信息。我会先整理关键问题，并交由合适的支持路径跟进。",
            ]
        body = pick_variant_body(bodies, variant)
        return f"{email.customer_name} 你好，\n\n{body}\n\n客服团队"

    if not is_chinese and variant.startswith("alternative"):
        if email.category == "refund":
            bodies = [
                "I see the duplicate billing concern. I will check the subscription and payment records first, then move the confirmed duplicate charge into the refund flow.",
                "Thanks for flagging the possible duplicate charge. I will verify the billing record and confirm the refund path if the extra payment is found.",
                "I will review the June invoice and payment history, then follow up with the refund status if the duplicate charge is confirmed.",
            ]
        elif email.category == "technical":
            bodies = [
                "This looks like an access issue that may involve an expired reset link, invalid token, or browser session state. Please request a new reset link and clear cache first; we can escalate if the issue continues.",
                "Please try a fresh reset link in a private browser window first. If access is still blocked, I will route the case for deeper account checks.",
                "I will help narrow this down across reset-link validity, browser state, and account permissions. Start with a new reset link, and we can escalate if it still fails.",
            ]
        elif email.category == "complaint":
            bodies = [
                "I am sorry this has disrupted your team. I will route this case to a higher-priority support path so we can focus on unblocking you quickly.",
                "I understand the delay has been frustrating. I will escalate this to a support specialist and keep the focus on getting your team unblocked.",
                "Thank you for the context. We will prioritize this case and follow up with the next action as soon as a specialist reviews it.",
            ]
        elif email.category == "billing":
            bodies = [
                "I can help locate the invoice or billing record. Please confirm the workspace and billing period so we can verify the correct document.",
                "Please share the billing month and workspace name, and I will check the invoice policy and matching payment record.",
                "I will review the invoice request against the account and billing period. Once confirmed, we can advise on the next billing action.",
            ]
        elif email.category == "product_question":
            bodies = [
                "The Pro plan is designed for team workflows, audit visibility, priority support, and advanced integrations. Share your use case and I can tailor the comparison.",
                "Pro is best for teams that need shared workspaces, admin controls, and faster support. I can map the differences to your workflow if you share more context.",
                "I can send a focused Pro comparison based on your team size, collaboration needs, and integration requirements.",
            ]
        else:
            bodies = [
                "Thanks for the details. I will review the request and route it to the right support workflow.",
                "I appreciate the context. I will check the request and make sure it reaches the right support path.",
                "Thanks for reaching out. I will summarize the issue and route it for the appropriate next step.",
            ]
        body = pick_variant_body(bodies, variant)
        return f"Hi {email.customer_name},\n\n{body}\n\nBest,\nCustomer Support Team"

    if is_chinese:
        if email.category == "refund":
            body = "我们识别到这封邮件可能涉及重复扣费或退款请求。我会先核验账单记录，并为重复付款准备退款处理。"
        elif email.category == "technical":
            body = "这个问题可能与重置链接过期、令牌失效或登录状态有关。建议先重新申请重置链接并清理浏览器缓存；如果仍无法访问，我们会升级处理。"
        elif email.category == "complaint":
            body = "很抱歉这个问题已经多次影响到你。该情况需要升级给人工客服专员，以便尽快跟进并解除阻塞。"
        elif email.category == "billing":
            body = "我可以协助处理账单或发票问题。请确认对应账号、账单月份和需要的开票信息。"
        elif email.category == "product_question":
            body = "Pro 版本包含团队空间、审计日志、优先支持和高级集成能力。我可以根据你的使用场景补充更具体的功能说明。"
        else:
            body = "感谢你的反馈。我会先核对邮件内容，并将问题转到合适的客服处理路径。"
        return f"{email.customer_name} 你好，\n\n{body}\n\n客服团队"
    else:
        if email.category == "refund":
            body = "We found signs of a duplicate subscription charge. I will verify the billing record and prepare the refund request for the duplicate payment."
        elif email.category == "technical":
            body = "The reset token may have expired or been invalidated. Please request a fresh reset link, clear your browser cache, and try again. I can also escalate this if access is still blocked."
        elif email.category == "complaint":
            body = "I am sorry this has taken multiple attempts. Your case should be escalated to a support specialist so we can unblock your team quickly."
        elif email.category == "billing":
            body = "I can help with the billing document request. Please confirm the account workspace and billing month so we can locate the correct record."
        elif email.category == "product_question":
            body = "The Pro plan includes advanced team capabilities and priority support. I can share the exact feature comparison for your workspace needs."
        else:
            body = "Thanks for reaching out. I will review the details and route this to the right support path."
        return f"Hi {email.customer_name},\n\n{body}\n\nBest,\nCustomer Support Team"


def strip_internal_reference_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith(("Reference used:", "参考依据："))
    ).strip()


def sanitize_customer_reply(text: str) -> str:
    cleaned = strip_internal_reference_lines(text)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    return cleaned.strip()


def build_structured_draft_reply(email: EmailRecord, variant: str, is_chinese: bool) -> str:
    hit = email.knowledge_hits[0] if email.knowledge_hits else None
    evidence = f"{hit.title}（{hit.source}）" if hit else ""
    evidence_text = f"{hit.title}\n{hit.snippet}" if hit else ""
    tone = select_reply_tone(variant)
    if is_chinese:
        opener = f"{email.customer_name} 你好，"
        category_intro = {
            "refund": "我已收到你关于重复扣费/退款的反馈。",
            "technical": "我已收到你关于登录或访问异常的反馈。",
            "complaint": "我已收到你的反馈，也理解这个问题已经影响到你的使用体验。",
            "billing": "我已收到你关于账单或发票的咨询。",
            "product_question": "我已收到你关于产品功能或套餐能力的咨询。",
        }.get(email.category or "other", "我已收到你的来信。")
        next_step = {
            "refund": "我会先核对订阅记录、扣费流水和账单周期；如果确认存在重复扣费，会按退款流程继续处理并同步结果。",
            "technical": "建议先重新获取一次重置链接，并在无痕窗口或清理缓存后重试；如果仍无法访问，我会把该工单升级给人工支持继续排查账号状态。",
            "complaint": "这类情况需要提高处理优先级，我会将问题交给人工支持专员跟进，并优先确认当前阻塞点和下一步处理时间。",
            "billing": "我会根据工作区、账单月份和开票信息核对记录；如果缺少必要信息，会再向你确认。",
            "product_question": build_product_answer(evidence_text, is_chinese=True),
        }.get(email.category or "other", "我会先确认问题类型，并把邮件转入合适的客服处理流程。")
        info_needed = {
            "refund": "如方便，请补充付款时间、账单月份和相关订单号。",
            "technical": "如方便，请补充报错截图、登录邮箱和问题发生时间。",
            "complaint": "如方便，请补充受影响的团队/工作区以及希望优先解决的事项。",
            "billing": "如方便，请补充工作区名称、账单月份和抬头/税号等开票信息。",
            "product_question": "如方便，请补充团队规模、核心使用场景和当前套餐。",
        }.get(email.category or "other", "如方便，请补充相关账号、时间和截图信息。")
        return (
            f"{opener}\n\n"
            f"{category_intro}\n\n"
            f"{next_step}\n\n"
            f"{info_needed}\n\n"
            f"如果你愿意，我可以继续根据你的团队场景整理一版更具体的套餐适配建议。"
            f"\n\n"
            f"客服团队"
        )

    greeting = f"Hi {email.customer_name},"
    category_intro = {
        "refund": "I received your message about the possible duplicate charge or refund request.",
        "technical": "I received your message about the login or access issue.",
        "complaint": "I received your feedback and understand this has affected your team's experience.",
        "billing": "I received your billing or invoice question.",
        "product_question": "I received your question about product capabilities or plan fit.",
    }.get(email.category or "other", "Thanks for reaching out.")
    next_step = {
        "refund": "I will first verify the subscription record, payment history, and billing period. If the duplicate charge is confirmed, I will move it through the refund process and share the status.",
        "technical": "Please request a fresh reset link and retry in a private window or after clearing your browser cache. If access is still blocked, I will escalate the ticket for account-level checks.",
        "complaint": "This should be handled with higher priority. I will route it to a support specialist so we can identify the blocker and the next action quickly.",
        "billing": "I will check the record using the workspace, billing month, and invoice details. If anything is missing, I will follow up with the exact information needed.",
        "product_question": build_product_answer(evidence_text, is_chinese=False),
    }.get(email.category or "other", "I will review the details and route this to the right support path.")
    info_needed = {
        "refund": "If available, please share the payment date, billing month, and order or invoice ID.",
        "technical": "If available, please share the error screenshot, login email, and when the issue started.",
        "complaint": "If available, please share the affected workspace or team and the most urgent blocker.",
        "billing": "If available, please share the workspace name, billing month, and invoice information.",
        "product_question": "If available, please share your team size, main workflow, and current plan.",
    }.get(email.category or "other", "If available, please share the related account, timing, and screenshot.")
    return (
        f"{greeting}\n\n"
        f"{category_intro}\n\n"
        f"{next_step}\n\n"
        f"{info_needed}\n\n"
        f"I can also tailor the plan recommendation if you share more about your team scenario."
        f"\n\n"
        f"Best,\nCustomer Support Team"
    )


def build_product_answer(evidence_text: str, is_chinese: bool) -> str:
    text = evidence_text.lower()
    if is_chinese:
        supported: list[str] = []
        if "团队工作区" in evidence_text or "team workspace" in text:
            supported.append("团队协作/团队工作区")
        if "权限" in evidence_text or "admin control" in text or "permission" in text:
            supported.append("统一权限管理")
        if "审计日志" in evidence_text or "audit" in text:
            supported.append("审计日志")
        if "高级集成" in evidence_text or "sso" in text or "webhook" in text or "api" in text:
            supported.append("高级集成（如 SSO、Webhook、API 对接和数据同步）")
        if supported:
            return (
                "根据当前知识库，Pro 套餐支持你提到的这些能力："
                + "、".join(supported)
                + "。其中团队工作区适合多人协作和统一管理，审计日志可用于追踪关键操作并支持合规场景，高级集成通常覆盖 SSO、Webhook、API 对接和数据同步。"
            )
        return "根据当前知识库，Pro 套餐主要面向团队协作、权限管理、审计追踪和高级集成等场景，整体上适合有团队协作和管理需求的客户。"

    supported_en: list[str] = []
    if "team workspace" in text or "团队工作区" in evidence_text:
        supported_en.append("team workspaces")
    if "permission" in text or "权限" in evidence_text:
        supported_en.append("permission management")
    if "audit" in text or "审计日志" in evidence_text:
        supported_en.append("audit logs")
    if "advanced integration" in text or "sso" in text or "webhook" in text or "api" in text:
        supported_en.append("advanced integrations such as SSO, Webhooks, API access, and data sync")
    if supported_en:
        return (
            "Based on the current knowledge base, the Pro plan supports "
            + ", ".join(supported_en)
            + ". It is designed for team collaboration, operational visibility, and integration-heavy workflows."
        )
    return "Based on the current knowledge base, the Pro plan is intended for team collaboration, permissions, audit visibility, and advanced integration scenarios."


def select_reply_tone(variant: str) -> str:
    return "balanced" if variant == "default" else variant


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars / 1.6))


def update_estimated_cost(email: EmailRecord) -> None:
    llm_input_price = float(os.getenv("LLM_INPUT_CNY_PER_MTOK", "1.0"))
    llm_output_price = float(os.getenv("LLM_OUTPUT_CNY_PER_MTOK", "2.0"))
    embedding_price = float(os.getenv("EMBEDDING_CNY_PER_MTOK", "0.08"))
    metrics = email.agent_metrics
    cost = (
        metrics.input_tokens / 1_000_000 * llm_input_price
        + metrics.output_tokens / 1_000_000 * llm_output_price
        + metrics.embedding_tokens / 1_000_000 * embedding_price
    )
    metrics.estimated_cost_cny = round(cost, 6)


def pick_variant_body(bodies: list[str], variant: str) -> str:
    try:
        index = int(variant.rsplit("-", 1)[1]) - 1
    except (IndexError, ValueError):
        index = 0
    return bodies[index % len(bodies)]


def review_node(state: EmailWorkflowState) -> EmailWorkflowState:
    """人工审核路由节点。

    低风险、高置信度且有知识库依据的邮件进入 ``ready_to_send``，
    仍然需要用户一键确认后才真实发送；其他情况进入人工审核队列。
    """
    email = state["email"]
    strong_grounding = bool(reliable_knowledge_hits(email, minimum="strong"))
    can_prepare_to_send = (
        email.risk_level == "low"
        and not email.should_escalate
        and email.category not in {None, "other"}
        and email.confidence >= 0.7
        and strong_grounding
    )
    email.status = "ready_to_send" if can_prepare_to_send else "human_review"
    email.steps.append(
        WorkflowStep(
            name="Review decision",
            status="complete" if can_prepare_to_send else "blocked",
            summary="Ready for one-click sending" if can_prepare_to_send else "Human review required",
            detail=(
                "Low-risk, high-confidence replies with knowledge grounding are prepared for manual one-click sending. "
                "High-risk, low-confidence, medium-risk, or weakly grounded replies remain in human review."
            ),
            confidence=0.88,
        )
    )
    update_processing_progress(
        state,
        stage="review",
        progress=98,
        message="审核门控完成，正在保存处理结果",
    )
    return {"email": email}


def build_email_workflow():
    """构建邮件 Agent 的 LangGraph 状态机。

    图结构是线性的主流程加一个条件分支：
    ``relevance_gate`` 判断为非客服邮件时直接结束，否则继续语义分析、RAG 和草稿生成。
    """
    graph = StateGraph(EmailWorkflowState)
    graph.add_node("preprocess", preprocess_node)
    graph.add_node("semantic_analysis", semantic_analysis_node)
    graph.add_node("relevance_gate", relevance_gate_node)
    graph.add_node("semantic_relevance_gate", semantic_relevance_gate_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("draft", draft_node)
    graph.add_node("review", review_node)

    graph.add_edge(START, "preprocess")
    graph.add_edge("preprocess", "relevance_gate")
    graph.add_conditional_edges("relevance_gate", route_after_relevance, {"irrelevant": END, "relevant": "semantic_analysis"})
    graph.add_edge("semantic_analysis", "semantic_relevance_gate")
    graph.add_conditional_edges("semantic_relevance_gate", route_after_relevance, {"irrelevant": END, "relevant": "retrieve"})
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", "review")
    graph.add_edge("review", END)

    return graph.compile()


email_workflow = build_email_workflow()


def get_workflow_architecture() -> dict:
    """返回轻量级架构描述，方便文档、调试或面试讲解使用。"""
    return {
        "engine": "LangGraph StateGraph",
        "state": ["email", "use_llm"],
        "nodes": [
            {"id": "preprocess", "role": "low-cost language, attachment, and risk-signal preprocessing"},
            {"id": "relevance_gate", "role": "filter platform notifications and non-support emails before LLM/RAG"},
            {"id": "semantic_analysis", "role": "LLM-first category, confidence, and risk analysis with rule fallback"},
            {"id": "semantic_relevance_gate", "role": "second-pass non-support filter after semantic analysis"},
            {"id": "retrieve", "role": "hybrid RAG retrieval over the knowledge base"},
            {"id": "draft", "role": "LLM-first customer reply drafting with structured fallback"},
            {"id": "review", "role": "human-in-the-loop routing and one-click-send decision"},
        ],
        "edges": [
            ("START", "preprocess"),
            ("preprocess", "relevance_gate"),
            ("relevance_gate", "END", "irrelevant"),
            ("relevance_gate", "semantic_analysis", "relevant"),
            ("semantic_analysis", "semantic_relevance_gate"),
            ("semantic_relevance_gate", "END", "irrelevant"),
            ("semantic_relevance_gate", "retrieve", "relevant"),
            ("retrieve", "draft"),
            ("draft", "review"),
            ("review", "END"),
        ],
    }


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def classify_semantically(text: str) -> tuple[str, float, list[str]]:
    """规则兜底分类器。

    当 LLM 不可用时，系统会用关键词集合粗略判断邮件类别和置信度。
    这不是最终理想分类能力，而是保障系统降级可用。
    """
    rules = [
        ("refund", {"refund", "charged", "duplicate", "payment", "退款", "退费", "扣费", "重复扣费", "重复收费", "付款"}),
        ("complaint", {"unhappy", "cancel", "third email", "nobody", "blocked", "投诉", "不满意", "取消", "没人处理", "第三次", "阻塞"}),
        ("technical", {"login", "password", "token", "invalid", "access", "登录", "密码", "验证码", "令牌", "无效", "无法访问", "进不去"}),
        ("billing", {"invoice", "billing", "receipt", "tax", "发票", "账单", "收据", "税务", "开票"}),
        ("product_question", {"feature", "plan", "integration", "workspace", "产品", "功能", "套餐", "版本", "集成", "工作区"}),
    ]
    best_category = "other"
    best_matches: list[str] = []

    for category, keywords in rules:
        matches = [keyword for keyword in keywords if keyword in text]
        if len(matches) > len(best_matches):
            best_category = category
            best_matches = matches

    if not best_matches:
        return "other", 0.58, []

    confidence = min(0.94, 0.72 + len(best_matches) * 0.07)
    return best_category, round(confidence, 2), best_matches


def build_analysis_reason(email: EmailRecord, matched: list[str]) -> str:
    """根据规则分类命中结果生成分析说明。"""
    if email.detected_language == "zh":
        matched_text = "、".join(matched) if matched else "无明显关键词"
        flags_text = "、".join(email.preprocessing_flags) if email.preprocessing_flags else "无强规则标记"
        return f"语义分析识别到分类线索：{matched_text}；预处理标记：{flags_text}；综合风险等级为 {email.risk_level}。"

    matched_text = ", ".join(matched) if matched else "no strong category signals"
    flags_text = ", ".join(email.preprocessing_flags) if email.preprocessing_flags else "no strong rule flags"
    return f"Semantic analysis found category signals: {matched_text}; preprocessing flags: {flags_text}; final risk level is {email.risk_level}."


def apply_llm_analysis(email: EmailRecord, result: SemanticAnalysisResult) -> None:
    """把 LLM 语义分析结果写回 EmailRecord。"""
    email.category = result.category
    email.confidence = round(result.confidence, 2)
    email.risk_level = result.risk_level
    email.risk_flags = result.risk_flags
    email.should_escalate = result.should_escalate
    email.priority = result.risk_level
    email.analysis_reason = result.reason
