/**
 * 前端单页应用入口。
 *
 * 这个文件负责把后端邮件 Agent 的能力组织成企业后台页面：
 * - 左侧侧边栏：收件箱、审核队列、知识库、运行日志、系统设置。
 * - 收件箱：展示客服邮件与非客服邮件复核列表，并支持同步 QQ 邮箱。
 * - 审核队列：展示需要人工确认的邮件、回复草稿、风险标记和执行流程。
 * - 知识库：支持上传/编辑/删除/版本回退/重新索引。
 * - 运行日志：展示邮件 Agent 轨迹、知识库操作和成本汇总。
 *
 * 目前前端没有引入复杂状态管理库，所有状态都集中在 App 组件中，便于毕业/实习
 * 项目演示时快速追踪数据流。
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  Inbox,
  Languages,
  ListChecks,
  Mail,
  Pencil,
  PlusCircle,
  RefreshCcw,
  Search,
  Settings,
  ShieldCheck,
  Trash2,
  UploadCloud,
  UserCheck,
  X,
  XCircle,
} from "lucide-react";
import "./styles.css";

const API_URL = "/api";
const AUTH_TOKEN_KEY = "email_agent_access_token";

type UserRole = "admin" | "manager" | "agent";

type UserProfile = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
};

type LoginResponse = {
  access_token: string;
  token_type: string;
  user: UserProfile;
};

async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const token = window.localStorage.getItem(AUTH_TOKEN_KEY);
  const headers = new Headers(init.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return window.fetch(input, { ...init, headers });
}

// 前后端共享的枚举类型。这里用 TypeScript union 限制状态值，避免 UI 写错状态字符串。
type Locale = "en" | "zh";
type ActiveView = "inbox" | "review" | "knowledge" | "runs" | "settings";
type EmailStatus = "new" | "processed" | "human_review" | "ready_to_send" | "needs_revision" | "escalated" | "sent" | "irrelevant";
type EmailProcessingStatus = "queued" | "running" | "completed" | "failed";
type KnowledgeStatus = "processing" | "indexed" | "failed" | "needs_reindex";
type KnowledgeInputMode = "upload" | "manual";
type MailProvider = "qq" | "outlook" | "gmail";
type MailAuthMode = "imap_smtp" | "api_oauth";

type MailSourceInfo = {
  provider: MailProvider;
  auth_mode: MailAuthMode;
  label: string;
  active: boolean;
  configured: boolean;
  secret_configured: boolean;
  imap_secret_configured: boolean;
  oauth_secret_configured: boolean;
  email_address: string;
  imap_host: string;
  imap_port: number | null;
  smtp_host: string;
  smtp_port: number | null;
  tenant_id: string;
  client_id: string;
  redirect_uri: string;
};

type MailSourceState = {
  active_provider: MailProvider;
  sources: MailSourceInfo[];
};

type MailSourceDraft = {
  provider: MailProvider;
  auth_mode: MailAuthMode;
  email_address: string;
  credential: string;
  imap_host: string;
  imap_port: string;
  smtp_host: string;
  smtp_port: string;
  tenant_id: string;
  client_id: string;
  client_secret: string;
  redirect_uri: string;
};

type WorkflowStep = {
  name: string;
  status: "complete" | "warning" | "blocked";
  summary: string;
  detail: string;
  confidence: number;
  timestamp: string;
};

type KnowledgeHit = {
  title: string;
  source: string;
  snippet: string;
  score: number;
  semantic_score?: number;
  keyword_score?: number;
  category_score?: number;
  category?: string;
  match_reason?: string;
  reliability?: "strong" | "medium" | "weak";
  page_number?: number | null;
  section_title?: string;
};

type EmailAttachment = {
  filename: string;
  content_type?: string;
  size_bytes?: number;
  text_preview?: string;
  parse_status?: string;
  status_message?: string;
  parse_report?: KnowledgeParseReport;
};

type KnowledgeParseReport = {
  file_type?: string;
  parser?: string;
  original_chars?: number;
  cleaned_chars?: number;
  noise_lines_removed?: number;
  page_count?: number | null;
  pages_with_text?: number | null;
  section_count?: number;
  table_count?: number;
  warnings?: string[];
};

type KnowledgeDocument = {
  id: string;
  title: string;
  source: string;
  chunk_count: number;
  current_version: number;
  status?: KnowledgeStatus;
  status_message?: string;
  parse_report?: KnowledgeParseReport;
  created_at: string;
};

type KnowledgeDocumentVersion = {
  id: string;
  document_id: string;
  version_number: number;
  title: string;
  source: string;
  content_hash: string;
  content_snapshot?: string;
  chunk_count: number;
  status?: KnowledgeStatus;
  status_message?: string;
  parse_report?: KnowledgeParseReport;
  note?: string;
  created_at: string;
};

type OperationLog = {
  id: string;
  scope: string;
  action: string;
  title: string;
  summary: string;
  detail: Record<string, string | number | boolean | null>;
  created_at: string;
};

type ReviewActionRecord = {
  action: "approve" | "revise" | "escalate" | "undo_escalate";
  note: string;
  revised_reply: string;
  created_at: string;
};

type EscalationTicket = {
  id: string;
  email_id: string;
  status: "open" | "assigned" | "resolved" | "returned";
  reason: string;
  created_by: string;
  assigned_to: string;
  resolution_note: string;
  created_at: string;
  updated_at: string;
};

type AgentMetrics = {
  llm_calls: number;
  semantic_llm_calls: number;
  draft_llm_calls: number;
  embedding_calls: number;
  input_tokens: number;
  output_tokens: number;
  embedding_tokens: number;
  rag_latency_ms: number;
  estimated_cost_cny: number;
};

type EmailRecord = {
  id: string;
  customer_name: string;
  customer_email: string;
  subject: string;
  body: string;
  attachments: EmailAttachment[];
  category: string | null;
  priority: "low" | "medium" | "high";
  status: EmailStatus;
  processing_status: EmailProcessingStatus;
  processing_stage: string;
  processing_progress: number;
  processing_message: string;
  processing_started_at?: string | null;
  processing_finished_at?: string | null;
  confidence: number;
  risk_flags: string[];
  knowledge_hits: KnowledgeHit[];
  draft_reply: string;
  agent_metrics: AgentMetrics;
  review_note: string;
  steps: WorkflowStep[];
  review_actions: ReviewActionRecord[];
  escalation_ticket?: EscalationTicket | null;
  created_at: string;
  updated_at: string;
};

const copy = {
  // 页面文案集中管理，语言切换时只需要根据 locale 取对应文案。
  en: {
    appName: "Email Agent",
    appScope: "Support operations",
    navInbox: "Inbox",
    navReview: "Review Queue",
    navKnowledge: "Knowledge Base",
    navRuns: "Run Logs",
    navSettings: "Settings",
    workbench: "Customer Email Workbench",
    subtitle: "Classify, ground, draft, and review support replies.",
    reviewTitle: "Review Queue",
    reviewSubtitle: "High-risk and low-confidence replies waiting for a human decision.",
    knowledgeTitle: "Knowledge Base",
    knowledgeSubtitle: "Policy and FAQ sources used to ground agent replies.",
    runsTitle: "Run Logs",
    runsSubtitle: "Email agent traces and knowledge-base audit events.",
    settingsTitle: "System Settings",
    settingsSubtitle: "Configure workspace behavior and operator preferences.",
    totalEmails: "Total Emails",
    inReview: "In Review",
    highPriority: "High Priority",
    processed: "Processed",
    newEmail: "New Email",
    syncQQ: "Sync QQ Mail",
    syncingQQ: "Syncing",
    refresh: "Refresh emails",
    customer: "Customer",
    email: "Email",
    subject: "Subject",
    body: "Body",
    sample: "Sample",
    processEmail: "Process Email",
    processing: "Processing",
    inboxQueue: "Inbox Queue",
    inboxQueueHint: "Live cases ordered by intake time",
    incomingMessage: "Incoming Message",
    agentTrace: "Agent Trace",
    knowledgeGrounding: "Knowledge Grounding",
    noKnowledge: "No strong knowledge match.",
    draftReply: "Draft Reply",
    approve: "Approve",
    revise: "Revise",
    escalate: "Escalate",
    undoEscalate: "Cancel escalation",
    regenerateReply: "Regenerate",
    regeneratingReply: "Generating",
    sendReply: "Send Reply",
    sendingReply: "Sending",
    riskFlags: "Risk flags",
    confidence: "confidence",
    uncategorized: "uncategorized",
    settingsPanel: "Settings",
    languageSetting: "Interface language",
    languageLabel: "中文",
    languageTitle: "Switch to Chinese",
    workspaceLabel: "Workspace",
    workspaceValue: "Acme Support",
    slaLabel: "SLA target",
    slaValue: "< 1 business hour",
    modeLabel: "Agent mode",
    modeValue: "Human-in-the-loop",
  },
  zh: {
    appName: "邮件 Agent",
    appScope: "客服运营",
    navInbox: "收件箱",
    navReview: "审核队列",
    navKnowledge: "知识库",
    navRuns: "运行日志",
    navSettings: "系统设置",
    workbench: "智能客服邮件工作台",
    subtitle: "自动分类、检索依据、生成草稿并进入人工审核。",
    reviewTitle: "审核队列",
    reviewSubtitle: "等待人工处理的高风险或低置信度回复。",
    knowledgeTitle: "知识库",
    knowledgeSubtitle: "用于支撑 Agent 回复的政策、FAQ 和操作手册。",
    runsTitle: "运行日志",
    runsSubtitle: "邮件处理轨迹与知识库操作审计记录。",
    settingsTitle: "系统设置",
    settingsSubtitle: "配置工作区行为和操作员偏好。",
    totalEmails: "邮件总量",
    inReview: "待审核",
    highPriority: "高优先级",
    processed: "已处理",
    newEmail: "新邮件",
    syncQQ: "同步 QQ 邮箱",
    syncingQQ: "同步中",
    refresh: "刷新邮件",
    customer: "客户",
    email: "邮箱",
    subject: "主题",
    body: "正文",
    sample: "样例",
    processEmail: "处理邮件",
    processing: "处理中",
    inboxQueue: "收件箱队列",
    inboxQueueHint: "按接入时间排序的实时工单",
    incomingMessage: "原始邮件",
    agentTrace: "Agent 执行轨迹",
    knowledgeGrounding: "知识库依据",
    noKnowledge: "没有匹配到强相关知识。",
    draftReply: "回复草稿",
    approve: "通过",
    revise: "修改",
    escalate: "升级处理",
    undoEscalate: "取消升级",
    regenerateReply: "换一版回复",
    regeneratingReply: "生成中",
    sendReply: "发送回复",
    sendingReply: "发送中",
    riskFlags: "风险标记",
    confidence: "置信度",
    uncategorized: "未分类",
    settingsPanel: "系统设置",
    languageSetting: "界面语言",
    languageLabel: "EN",
    languageTitle: "切换到英文",
    workspaceLabel: "工作区",
    workspaceValue: "Acme 客服",
    slaLabel: "SLA 目标",
    slaValue: "1 个工作小时内",
    modeLabel: "Agent 模式",
    modeValue: "人工审核闭环",
  },
} satisfies Record<Locale, Record<string, string>>;

const statusLabels: Record<Locale, Record<EmailStatus, string>> = {
  en: {
    new: "new",
    processed: "processed",
    human_review: "human review",
    ready_to_send: "ready to send",
    needs_revision: "needs revision",
    escalated: "escalated",
    sent: "sent",
    irrelevant: "non-support",
  },
  zh: {
    new: "新建",
    processed: "已处理",
    human_review: "待人工审核",
    ready_to_send: "可发送",
    needs_revision: "需要修改",
    escalated: "已升级",
    sent: "已发送",
    irrelevant: "非客服邮件",
  },
};

const priorityLabels: Record<Locale, Record<EmailRecord["priority"], string>> = {
  en: { low: "low", medium: "medium", high: "high" },
  zh: { low: "低", medium: "中", high: "高" },
};

const knowledgeStatusLabels: Record<Locale, Record<KnowledgeStatus, string>> = {
  en: { processing: "indexing", indexed: "indexed", failed: "failed", needs_reindex: "reindex needed" },
  zh: { processing: "处理中", indexed: "已索引", failed: "索引失败", needs_reindex: "待重新索引" },
};

const categoryLabels: Record<Locale, Record<string, string>> = {
  en: {
    refund: "refund",
    complaint: "complaint",
    technical: "technical",
    billing: "billing",
    product_question: "product question",
    other: "other",
  },
  zh: {
    refund: "退款",
    complaint: "投诉",
    technical: "技术问题",
    billing: "账单",
    product_question: "产品咨询",
    other: "其他",
  },
};

function normalizeKnowledgeStatus(status?: KnowledgeStatus): KnowledgeStatus {
  return status ?? "indexed";
}

function formatCount(value?: number | null) {
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "-";
}

function formatLatency(value: number) {
  if (!value) return "0 ms";
  if (value >= 10_000) return `${(value / 1000).toFixed(1)} s`;
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${value} ms`;
}

function formatCost(value: number) {
  if (!value) return "¥0";
  if (value < 0.000001) return "<¥0.000001";
  if (value < 0.01) return `¥${value.toFixed(6)}`;
  return `¥${value.toFixed(4)}`;
}

function normalizeUploadError(detail: unknown) {
  if (typeof detail === "string") return { kind: "upload_error", message: detail };
  if (detail && typeof detail === "object") {
    const value = detail as { kind?: unknown; message?: unknown };
    return {
      kind: typeof value.kind === "string" ? value.kind : "upload_error",
      message: typeof value.message === "string" ? value.message : "上传失败，请检查文件后重试。",
    };
  }
  return { kind: "upload_error", message: "上传失败，请检查文件后重试。" };
}

function App() {
  const [authToken, setAuthToken] = useState(() => window.localStorage.getItem(AUTH_TOKEN_KEY) || "");
  const [currentUser, setCurrentUser] = useState<UserProfile | null>(null);
  const [authLoading, setAuthLoading] = useState(Boolean(authToken));
  const [authError, setAuthError] = useState("");
  const [loginForm, setLoginForm] = useState({ username: "admin", password: "Admin123456" });
  // 邮件、知识库和日志是三个主要业务数据源。为了让页面切换时不反复丢状态，
  // 它们统一放在 App 顶层，然后向不同视图组件传递。
  const [emails, setEmails] = useState<EmailRecord[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [knowledgeDocs, setKnowledgeDocs] = useState<KnowledgeDocument[]>([]);
  const [knowledgeVersions, setKnowledgeVersions] = useState<Record<string, KnowledgeDocumentVersion[]>>({});
  const [operationLogs, setOperationLogs] = useState<OperationLog[]>([]);
  const [knowledgeFile, setKnowledgeFile] = useState<File | null>(null);
  const [knowledgeRevisionFile, setKnowledgeRevisionFile] = useState<File | null>(null);
  const [editingKnowledgeId, setEditingKnowledgeId] = useState("");
  const [deletingKnowledgeId, setDeletingKnowledgeId] = useState("");
  const [reindexingKnowledgeId, setReindexingKnowledgeId] = useState("");
  const [restoringVersionId, setRestoringVersionId] = useState("");
  const [deletingVersionId, setDeletingVersionId] = useState("");
  const [knowledgeEditForm, setKnowledgeEditForm] = useState({ title: "", content: "" });
  const [knowledgeInputMode, setKnowledgeInputMode] = useState<KnowledgeInputMode>("upload");
  const [knowledgeForm, setKnowledgeForm] = useState({
    title: "物流延迟处理规则",
    source: "shipping_delay_policy.md",
    content:
      "适用场景：客户反馈订单物流延迟、包裹长时间未更新或要求补偿。\n\n处理原则：先确认订单号、物流单号和承运商状态；如果超过承诺送达时间，应安抚客户并创建跟进工单；涉及退款或赔付时进入人工审核。",
  });
  const [syncing, setSyncing] = useState(false);
  const [sendingId, setSendingId] = useState("");
  const [regeneratingId, setRegeneratingId] = useState("");
  const [regenerateProgress, setRegenerateProgress] = useState(0);
  const [viewedReadyIds, setViewedReadyIds] = useState<string[]>([]);
  const [ingesting, setIngesting] = useState(false);
  const [savingKnowledge, setSavingKnowledge] = useState(false);
  const [uploadingKnowledge, setUploadingKnowledge] = useState(false);
  const [uploadingKnowledgeRevisionId, setUploadingKnowledgeRevisionId] = useState("");
  const [cleaningLogs, setCleaningLogs] = useState(false);
  const [mailSourceState, setMailSourceState] = useState<MailSourceState | null>(null);
  const [mailSourceModalOpen, setMailSourceModalOpen] = useState(false);
  const [mailSourceDraft, setMailSourceDraft] = useState<MailSourceDraft>(() => emptyMailSourceDraft("qq"));
  const [savingMailSource, setSavingMailSource] = useState(false);
  const [mailSourceError, setMailSourceError] = useState("");
  const [mailListScope, setMailListScope] = useState<"active" | "all">("active");
  const [locale, setLocale] = useState<Locale>("zh");
  const [activeView, setActiveView] = useState<ActiveView>("inbox");
  const syncingRef = useRef(false);
  const selectedIdRef = useRef("");

  const t = copy[locale];
  // 收件箱中把客服邮件和非客服邮件拆成两个可折叠列表：
  // 客服邮件用于处理和发送；非客服邮件只用于快速复核，避免通知类邮件干扰主流程。
  const inboxEmails = emails.filter((email) => email.status !== "irrelevant" && !(currentUser?.role === "agent" && hasActiveEscalation(email)));
  const irrelevantEmails = emails.filter((email) => email.status === "irrelevant");
  const visibleInboxEmails = useMemo(() => [...inboxEmails, ...irrelevantEmails], [inboxEmails, irrelevantEmails]);
  const selected = useMemo(() => visibleInboxEmails.find((email) => email.id === selectedId) ?? inboxEmails[0] ?? irrelevantEmails[0] ?? emails[0], [emails, inboxEmails, irrelevantEmails, visibleInboxEmails, selectedId]);
  const reviewEmails = emails.filter((email) => {
    if (["irrelevant", "sent", "processed"].includes(email.status)) return false;
    if (currentUser?.role === "agent" && hasActiveEscalation(email)) return false;
    if (currentUser?.role === "manager") return isEscalationEmail(email);
    if (email.status === "ready_to_send") return hasManualApproval(email) || hasEscalationHistory(email);
    return ["human_review", "needs_revision", "escalated", "ready_to_send"].includes(email.status) || email.priority === "high";
  });
  const selectedReview = reviewEmails.find((email) => email.id === selectedId) ?? reviewEmails[0];
  const readyToSendEmails = emails.filter(isLowRiskReadyToSend);
  const viewedReadyCount = readyToSendEmails.filter((email) => viewedReadyIds.includes(email.id)).length;
  const canBulkSendReady = readyToSendEmails.length > 0 && viewedReadyCount === readyToSendEmails.length;
  const processedCount = emails.filter((email) => email.status === "processed" || email.status === "ready_to_send").length;
  const hasProcessingEmails = emails.some(isEmailProcessing);
  const pageMeta = getPageMeta(activeView, t);
  const visibleViews = getVisibleViews(currentUser?.role);

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const result = params.get("mail_oauth");
    if (!result) return;
    const provider = mailSourceLabel((params.get("provider") || undefined) as MailProvider | undefined);
    if (result === "success") {
      window.alert(`${provider} API OAuth 授权成功，邮件源已切换。`);
    } else {
      window.alert(`${provider} API OAuth 授权失败：${params.get("message") || "授权被取消"}`);
    }
    window.history.replaceState({}, document.title, window.location.pathname);
  }, []);

  useEffect(() => {
    if (!authToken) {
      setAuthLoading(false);
      return;
    }
    apiFetch(`${API_URL}/auth/me`)
      .then(async (response) => {
        if (!response.ok) throw new Error("登录已过期");
        setCurrentUser((await response.json()) as UserProfile);
      })
      .catch(() => {
        window.localStorage.removeItem(AUTH_TOKEN_KEY);
        setAuthToken("");
        setCurrentUser(null);
      })
      .finally(() => setAuthLoading(false));
  }, [authToken]);

  async function login(event: React.FormEvent) {
    event.preventDefault();
    setAuthError("");
    setAuthLoading(true);
    try {
      const response = await apiFetch(`${API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(loginForm),
      });
      if (!response.ok) throw new Error("账号或密码错误");
      const result = (await response.json()) as LoginResponse;
      window.localStorage.setItem(AUTH_TOKEN_KEY, result.access_token);
      setAuthToken(result.access_token);
      setCurrentUser(result.user);
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "登录失败");
    } finally {
      setAuthLoading(false);
    }
  }

  function logout() {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    setAuthToken("");
    setCurrentUser(null);
    setEmails([]);
    setSelectedId("");
  }

  async function loadEmails(options: { selectFirstIfEmpty?: boolean; scope?: "active" | "all" } = {}) {
    // 保留当前选中邮件，避免后台自动同步刷新后页面突然跳回第一封。
    const scope = options.scope ?? mailListScope;
    const response = await apiFetch(`${API_URL}/emails?provider=${scope}`);
    const data = (await response.json()) as EmailRecord[];
    setEmails(data);
    if (options.selectFirstIfEmpty && !selectedIdRef.current && data.length > 0) {
      selectedIdRef.current = data[0].id;
      setSelectedId(data[0].id);
    }
    return data;
  }

  async function loadKnowledgeDocuments() {
    const response = await apiFetch(`${API_URL}/knowledge/documents`);
    setKnowledgeDocs((await response.json()) as KnowledgeDocument[]);
  }

  async function loadOperationLogs() {
    const response = await apiFetch(`${API_URL}/operation-logs`);
    setOperationLogs((await response.json()) as OperationLog[]);
  }

  async function loadMailSourceState() {
    const response = await apiFetch(`${API_URL}/mail/source`);
    if (!response.ok) return;
    setMailSourceState((await response.json()) as MailSourceState);
  }

  function openMailSourceModal(provider = mailSourceState?.active_provider || "qq") {
    const source = mailSourceState?.sources.find((item) => item.provider === provider);
    setMailSourceDraft(mailSourceDraftFromInfo(provider, source));
    setMailSourceError("");
    setMailSourceModalOpen(true);
  }

  function selectMailProvider(provider: MailProvider) {
    const source = mailSourceState?.sources.find((item) => item.provider === provider);
    setMailSourceDraft(mailSourceDraftFromInfo(provider, source));
    setMailSourceError("");
  }

  async function saveMailSource(event: React.FormEvent) {
    event.preventDefault();
    setSavingMailSource(true);
    setMailSourceError("");
    try {
      const endpoint = mailSourceDraft.auth_mode === "api_oauth" ? `${API_URL}/mail/oauth/start` : `${API_URL}/mail/source`;
      const response = await apiFetch(endpoint, {
        method: mailSourceDraft.auth_mode === "api_oauth" ? "POST" : "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...mailSourceDraft,
          imap_port: mailSourceDraft.imap_port ? Number(mailSourceDraft.imap_port) : null,
          smtp_port: mailSourceDraft.smtp_port ? Number(mailSourceDraft.smtp_port) : null,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "连接测试失败，邮件源未切换");
      if (mailSourceDraft.auth_mode === "api_oauth") {
        window.location.assign((result as { authorization_url: string }).authorization_url);
        return;
      }
      setMailSourceState((result as { state: MailSourceState }).state);
      setMailSourceModalOpen(false);
      setMailListScope("active");
      selectedIdRef.current = "";
      setSelectedId("");
      await loadEmails({ scope: "active", selectFirstIfEmpty: true });
      await syncMail();
      window.alert("连接测试通过，邮件源已切换。");
    } catch (error) {
      setMailSourceError(error instanceof Error ? error.message : "连接测试失败，邮件源未切换");
    } finally {
      setSavingMailSource(false);
    }
  }

  async function cleanupRunLogs() {
    const confirmed = window.confirm("确认清理过期运行日志吗？\n\n邮件 Agent 轨迹：清理 30 天前且已结束邮件的轨迹。\n知识库操作：清理 180 天前的审计记录。");
    if (!confirmed) return;

    setCleaningLogs(true);
    try {
      const [agentResponse, knowledgeResponse] = await Promise.all([
        apiFetch(`${API_URL}/emails/workflow-steps/cleanup?retention_days=30`, { method: "DELETE" }),
        apiFetch(`${API_URL}/operation-logs/cleanup?retention_days=180&scope=knowledge`, { method: "DELETE" }),
      ]);
      const agentResult = (await agentResponse.json()) as { deleted: number };
      const knowledgeResult = (await knowledgeResponse.json()) as { deleted: number };
      await Promise.all([loadEmails(), loadOperationLogs()]);
      window.alert(`清理完成：邮件 Agent 轨迹 ${agentResult.deleted} 条，知识库操作 ${knowledgeResult.deleted} 条。`);
    } finally {
      setCleaningLogs(false);
    }
  }

  async function ingestKnowledge() {
    setIngesting(true);
    try {
      const response = await apiFetch(`${API_URL}/knowledge/ingest`, { method: "POST" });
      setKnowledgeDocs((await response.json()) as KnowledgeDocument[]);
    } finally {
      setIngesting(false);
    }
  }

  async function createKnowledgeDocument() {
    setSavingKnowledge(true);
    try {
      const response = await apiFetch(`${API_URL}/knowledge/documents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(knowledgeForm),
      });
      await response.json();
      await loadKnowledgeDocuments();
      setKnowledgeForm({ title: "", source: "", content: "" });
    } finally {
      setSavingKnowledge(false);
    }
  }

  async function uploadKnowledgeDocument() {
    if (!knowledgeFile) return;
    setUploadingKnowledge(true);
    try {
      await uploadKnowledgeFileWithDuplicatePrompt(`${API_URL}/knowledge/documents/upload`, knowledgeFile, "POST");
      await loadKnowledgeDocuments();
      setKnowledgeFile(null);
    } finally {
      setUploadingKnowledge(false);
    }
  }

  async function startEditKnowledgeDocument(doc: KnowledgeDocument) {
    const [response, versionsResponse] = await Promise.all([
      apiFetch(`${API_URL}/knowledge/documents/${doc.id}`),
      apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions`),
    ]);
    const detail = (await response.json()) as KnowledgeDocument & { content: string };
    const versions = (await versionsResponse.json()) as KnowledgeDocumentVersion[];
    setEditingKnowledgeId(doc.id);
    setKnowledgeVersions((items) => ({ ...items, [doc.id]: versions }));
    setKnowledgeEditForm({ title: detail.title, content: detail.content });
    setKnowledgeRevisionFile(null);
  }

  async function saveKnowledgeDocument(docId: string) {
    const response = await apiFetch(`${API_URL}/knowledge/documents/${docId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(knowledgeEditForm),
    });
    await response.json();
    await loadKnowledgeDocuments();
    setEditingKnowledgeId("");
    setKnowledgeRevisionFile(null);
  }

  async function uploadKnowledgeDocumentRevision(doc: KnowledgeDocument) {
    if (!knowledgeRevisionFile) return;
    setUploadingKnowledgeRevisionId(doc.id);
    try {
      await uploadKnowledgeFileWithDuplicatePrompt(`${API_URL}/knowledge/documents/${doc.id}/upload`, knowledgeRevisionFile, "PUT");
      const versionsResponse = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions`);
      const versions = (await versionsResponse.json()) as KnowledgeDocumentVersion[];
      await loadKnowledgeDocuments();
      await loadOperationLogs();
      setKnowledgeVersions((items) => ({ ...items, [doc.id]: versions }));
      setKnowledgeRevisionFile(null);
    } finally {
      setUploadingKnowledgeRevisionId("");
    }
  }

  async function uploadKnowledgeFileWithDuplicatePrompt(url: string, file: File, method: "POST" | "PUT") {
    // 上传知识库文件时，后端可能返回“强重复”或“弱重复”。
    // 强重复直接阻止；弱重复由用户确认后带 force_weak_duplicate 再提交一次。
    const send = async (forceWeakDuplicate: boolean) => {
      const formData = new FormData();
      formData.append("file", file);
      const separator = url.includes("?") ? "&" : "?";
      return apiFetch(`${url}${separator}force_weak_duplicate=${forceWeakDuplicate}`, { method, body: formData });
    };

    let response = await send(false);
    if (response.status === 409) {
      const error = await response.json();
      const detail = normalizeUploadError(error.detail);
      if (detail.kind === "weak_duplicate") {
        const confirmed = window.confirm(`${detail.message}\n\n是否仍然继续入库？继续后会保留为独立知识文档或新版本。`);
        if (!confirmed) return;
        response = await send(true);
      } else {
        window.alert(detail.message);
        return;
      }
    }
    if (!response.ok) {
      const error = await response.json();
      throw new Error(normalizeUploadError(error.detail).message || "上传失败");
    }
    await response.json();
  }

  async function reindexKnowledgeDocument(doc: KnowledgeDocument) {
    setReindexingKnowledgeId(doc.id);
    try {
      const response = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/reindex`, { method: "POST" });
      await response.json();
      await loadKnowledgeDocuments();
    } finally {
      setReindexingKnowledgeId("");
    }
  }

  async function restoreKnowledgeVersion(doc: KnowledgeDocument, version: KnowledgeDocumentVersion) {
    const confirmed = window.confirm(
      `确认回退「${doc.title}」到 v${version.version_number} 吗？\n\n回退后会删除该版本之后的版本记录，无法再恢复到这些被删除的版本。操作会记录到运行日志。`
    );
    if (!confirmed) return;
    setRestoringVersionId(version.id);
    try {
      const response = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions/${version.id}/restore`, { method: "POST" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "版本回退失败");
      }
      await response.json();
      const versionsResponse = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions`);
      const versions = (await versionsResponse.json()) as KnowledgeDocumentVersion[];
      await loadKnowledgeDocuments();
      await loadOperationLogs();
      setKnowledgeVersions((items) => ({ ...items, [doc.id]: versions }));
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "版本回退失败");
    } finally {
      setRestoringVersionId("");
    }
  }

  async function deleteKnowledgeVersion(doc: KnowledgeDocument, version: KnowledgeDocumentVersion) {
    const confirmed = window.confirm(`确认删除「${doc.title}」的 v${version.version_number} 吗？删除记录会写入运行日志。`);
    if (!confirmed) return;
    setDeletingVersionId(version.id);
    try {
      const response = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions/${version.id}`, { method: "DELETE" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "删除版本失败");
      }
      const versionsResponse = await apiFetch(`${API_URL}/knowledge/documents/${doc.id}/versions`);
      const versions = (await versionsResponse.json()) as KnowledgeDocumentVersion[];
      await loadKnowledgeDocuments();
      await loadOperationLogs();
      setKnowledgeVersions((items) => ({ ...items, [doc.id]: versions }));
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "删除版本失败");
    } finally {
      setDeletingVersionId("");
    }
  }

  async function deleteKnowledgeDocument(doc: KnowledgeDocument) {
    const confirmed = window.confirm(`确定删除「${doc.title}」吗？`);
    if (!confirmed) return;
    setDeletingKnowledgeId(doc.id);
    try {
      await apiFetch(`${API_URL}/knowledge/documents/${doc.id}`, { method: "DELETE" });
      await loadKnowledgeDocuments();
      if (editingKnowledgeId === doc.id) setEditingKnowledgeId("");
    } finally {
      setDeletingKnowledgeId("");
    }
  }

  async function syncMail(options: { silent?: boolean } = {}) {
    // 手动/自动同步共用同一入口。syncingRef 用来避免上一次同步未结束时重复发起请求。
    if (syncingRef.current) {
      return;
    }
    syncingRef.current = true;
    if (!options.silent) setSyncing(true);
    try {
      const response = await apiFetch(`${API_URL}/mail/import?limit=5`, { method: "POST" });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || (locale === "zh" ? "邮箱刷新失败" : "Mailbox refresh failed"));
      }
      const result = (await response.json()) as { queued_count?: number; skipped_count?: number; emails?: EmailRecord[] };
      const imported = result.emails || [];
      await loadEmails();
      await loadOperationLogs();
      if (!options.silent && imported.length > 0) setSelectedId(imported[0].id);
    } catch (error) {
      if (!options.silent) {
        window.alert(error instanceof Error ? error.message : (locale === "zh" ? "邮箱刷新失败，请稍后重试。" : "Mailbox refresh failed. Please try again."));
      }
    } finally {
      syncingRef.current = false;
      if (!options.silent) setSyncing(false);
    }
  }

  async function sendReply(emailId: string) {
    const target = emails.find((email) => email.id === emailId);
    if (!target) return;
    if (target.status !== "ready_to_send") {
      window.alert("请先在审核队列中通过该回复，再发送邮件。");
      return;
    }
    if (!canUserSendReply(currentUser, target)) {
      window.alert("该邮件曾经升级处理过，需要客服主管或管理员发送回复。");
      return;
    }
    const confirmed = window.confirm(`确认通过 QQ SMTP 发送给 ${target.customer_email} 吗？\n\n主题：Re: ${target.subject}`);
    if (!confirmed) return;
    setSendingId(emailId);
    try {
      const response = await apiFetch(`${API_URL}/emails/${emailId}/send`, { method: "POST" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "发送失败");
      }
      const updated = (await response.json()) as EmailRecord;
      setEmails((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await loadOperationLogs();
      setSelectedId(updated.id);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "发送失败");
    } finally {
      setSendingId("");
    }
  }

  async function bulkSendReadyReplies() {
    // 批量发送只允许已全部查看过的低风险邮件，避免用户没看草稿就误发。
    if (readyToSendEmails.length === 0) return;
    if (currentUser?.role === "agent" && readyToSendEmails.some(hasEscalationHistory)) {
      window.alert("待发送列表中包含曾经升级处理过的邮件，请由客服主管或管理员发送。");
      return;
    }
    if (!canBulkSendReady) {
      window.alert(`请先逐封查看全部可发送邮件。当前已查看 ${viewedReadyCount}/${readyToSendEmails.length} 封。`);
      return;
    }
    const subjectList = readyToSendEmails.map((email, index) => `${index + 1}. ${email.subject}`).join("\n");
    const confirmed = window.confirm(
      `确认批量发送 ${readyToSendEmails.length} 封低风险可发送邮件吗？\n\n${subjectList}\n\n将通过 QQ SMTP 逐封发送，发送后不可自动撤回。`
    );
    if (!confirmed) return;

    setSendingId("bulk");
    const failures: string[] = [];
    try {
      for (const email of readyToSendEmails) {
        const response = await apiFetch(`${API_URL}/emails/${email.id}/send`, { method: "POST" });
        if (!response.ok) {
          const error = await response.json();
          failures.push(`${email.subject}：${error.detail || "发送失败"}`);
        }
      }
      await loadEmails();
      await loadOperationLogs();
      if (failures.length > 0) {
        window.alert(`批量发送完成，但有 ${failures.length} 封失败：\n\n${failures.join("\n")}`);
      } else {
        window.alert("全部可发送邮件已发送。");
      }
    } finally {
      setSendingId("");
    }
  }

  async function regenerateReply(emailId: string) {
    // 重新生成回复可能需要等待 LLM。这里用前端估算进度条缓解“按钮点了没反应”的等待感。
    setRegeneratingId(emailId);
    setRegenerateProgress(8);
    const startedAt = Date.now();
    const progressTimer = window.setInterval(() => {
      const elapsed = Date.now() - startedAt;
      const estimated = Math.min(90, 8 + Math.round((elapsed / 12000) * 82));
      setRegenerateProgress((current) => Math.max(current, estimated));
    }, 350);
    try {
      const response = await apiFetch(`${API_URL}/emails/${emailId}/draft/regenerate`, { method: "POST" });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "回复草稿生成失败");
      }
      const updated = (await response.json()) as EmailRecord;
      setRegenerateProgress(100);
      setEmails((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      await loadOperationLogs();
      setSelectedId(updated.id);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "回复草稿生成失败");
    } finally {
      window.clearInterval(progressTimer);
      window.setTimeout(() => {
        setRegeneratingId("");
        setRegenerateProgress(0);
      }, 450);
    }
  }

  async function review(action: "approve" | "escalate" | "revise" | "undo_escalate", note = "", revisedReply = "") {
    // 所有人工审核动作都走同一个接口，后端负责更新状态和记录审核历史。
    const target = activeView === "review" ? selectedReview : selected;
    if (!target) return;
    if (action === "escalate" && currentUser?.role === "agent") {
      const confirmed = window.confirm(
        locale === "zh"
          ? "确认升级处理这封邮件吗？\n\n确认后邮件将移交给客服主管和系统管理员，并立即从你的收件箱及审核队列中移除。你无法自行取消升级。"
          : "Escalate this email?\n\nIt will be transferred to managers and administrators and removed from your inbox and review queue. You cannot cancel the escalation yourself."
      );
      if (!confirmed) return;
    }
    if (action === "undo_escalate" && currentUser?.role === "agent") {
      window.alert(locale === "zh" ? "客服人员不能取消升级处理，请联系主管或系统管理员。" : "Agents cannot cancel escalations. Contact a manager or administrator.");
      return;
    }
    if (action === "approve" && hasActiveEscalation(target)) {
      window.alert("该邮件正在升级处理中，请先取消升级，或由客服主管处理升级工单后再通过。");
      return;
    }
    if (action === "approve" && hasEscalationHistory(target)) {
      if (currentUser?.role === "agent") {
        window.alert("该邮件已经被升级处理，客服人员不能发送回复，请由客服主管或管理员处理。");
        return;
      }
      const confirmed = window.confirm("该邮件有升级处理记录，通过后需要由客服主管或管理员负责发送。确认继续通过吗？");
      if (!confirmed) return;
    }
    if (action === "revise" && hasActiveEscalation(target)) {
      window.alert("该邮件正在升级处理中，请先取消升级，或由客服主管处理升级工单后再修改。");
      return;
    }
    const response = await apiFetch(`${API_URL}/emails/${target.id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action,
        note: note || (action === "approve" ? "Reviewed and approved for sending." : action === "escalate" ? "Escalated to senior support." : "Needs revision."),
        revised_reply: revisedReply,
      }),
    });
    if (!response.ok) {
      const error = await response.json();
      window.alert(error.detail || "审核操作失败");
      return;
    }
    const updated = (await response.json()) as EmailRecord;
    if (action === "escalate" && currentUser?.role === "agent") {
      setEmails((items) => items.filter((item) => item.id !== updated.id));
      setSelectedId("");
    } else {
      setEmails((items) => items.map((item) => (item.id === updated.id ? updated : item)));
      setSelectedId(updated.id);
    }
    await loadOperationLogs();
  }

  async function updateEscalation(emailId: string, action: "assign" | "resolve" | "return_to_review", note = "") {
    const response = await apiFetch(`${API_URL}/emails/${emailId}/escalation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, note }),
    });
    if (!response.ok) {
      const error = await response.json();
      window.alert(error.detail || "升级工单处理失败");
      return;
    }
    const updated = (await response.json()) as EmailRecord;
    setEmails((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    await loadOperationLogs();
    setSelectedId(updated.id);
  }

  useEffect(() => {
    // 页面初始化时一次性加载所有基础数据。
    if (!currentUser) return;
    loadEmails({ selectFirstIfEmpty: true });
    loadMailSourceState();
    if (canAccessView(currentUser.role, "knowledge")) loadKnowledgeDocuments();
    if (canAccessView(currentUser.role, "runs")) loadOperationLogs();
  }, [currentUser?.id]);

  useEffect(() => {
    // 用户切换“当前邮箱/全部历史”时重新查询，不修改服务端活动邮件源。
    if (!currentUser) return;
    selectedIdRef.current = "";
    setSelectedId("");
    loadEmails({ scope: mailListScope, selectFirstIfEmpty: true });
  }, [mailListScope]);

  useEffect(() => {
    // 自动同步当前邮件源只在浏览器标签可见时运行，减少后台空转请求。
    if (!currentUser) return;
    const syncWhenVisible = () => {
      if (document.visibilityState !== "visible") return;
      syncMail({ silent: true });
    };
    const timer = window.setInterval(syncWhenVisible, 20_000);
    const initialTimer = window.setTimeout(syncWhenVisible, 2_000);
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(initialTimer);
    };
  }, [currentUser?.id, mailListScope]);

  useEffect(() => {
    // 进度轮询由邮件状态驱动。刷新页面或切换视图后，只要仍有后台任务，
    // 页面就会继续刷新；全部任务结束后定时器会随状态变化自动销毁。
    if (!currentUser || !hasProcessingEmails) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      loadEmails();
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [currentUser?.id, hasProcessingEmails, mailListScope]);

  useEffect(() => {
    // 用户看过所有 ready_to_send 邮件后，才允许批量确认发送。
    if (!selected || selected.status !== "ready_to_send") return;
    setViewedReadyIds((items) => (items.includes(selected.id) ? items : [...items, selected.id]));
  }, [selected?.id, selected?.status]);

  useEffect(() => {
    const readyIds = new Set(readyToSendEmails.map((email) => email.id));
    setViewedReadyIds((items) => items.filter((id) => readyIds.has(id)));
  }, [emails]);

  useEffect(() => {
    // 如果知识库文档仍在后台索引，前端短轮询刷新状态。
    if (!knowledgeDocs.some((doc) => normalizeKnowledgeStatus(doc.status) === "processing")) return;
    const timer = window.setTimeout(() => loadKnowledgeDocuments(), 2000);
    return () => window.clearTimeout(timer);
  }, [knowledgeDocs]);

  useEffect(() => {
    if (!currentUser) return;
    if (!visibleViews.includes(activeView)) {
      setActiveView(visibleViews[0] ?? "inbox");
    }
  }, [activeView, currentUser?.role, visibleViews]);

  if (authLoading && !currentUser) {
    return <div className="authShell"><div className="authCard"><strong>正在检查登录状态...</strong></div></div>;
  }

  if (!currentUser) {
    return (
      <main className="authShell">
        <form className="authCard" onSubmit={login}>
          <div className="brand authBrand">
            <div className="brandMark"><Mail size={20} /></div>
            <div><strong>邮件 Agent</strong><span>企业后台登录</span></div>
          </div>
          <label>
            <span>账号</span>
            <input value={loginForm.username} onChange={(event) => setLoginForm((value) => ({ ...value, username: event.target.value }))} />
          </label>
          <label>
            <span>密码</span>
            <input type="password" value={loginForm.password} onChange={(event) => setLoginForm((value) => ({ ...value, password: event.target.value }))} />
          </label>
          {authError && <p className="authError">{authError}</p>}
          <button className="primaryButton" type="submit" disabled={authLoading}>{authLoading ? "登录中" : "登录"}</button>
          <p className="authHint">演示账号：admin / manager / agent，默认密码 Admin123456。</p>
        </form>
      </main>
    );
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brandMark"><Mail size={20} /></div>
          <div><strong>{t.appName}</strong><span>{t.appScope}</span></div>
        </div>
        <nav className="sideNav">
          {visibleViews.includes("inbox") && <NavButton active={activeView === "inbox"} icon={<Inbox size={16} />} label={t.navInbox} onClick={() => setActiveView("inbox")} />}
          {visibleViews.includes("review") && <NavButton active={activeView === "review"} icon={<ListChecks size={16} />} label={t.navReview} onClick={() => setActiveView("review")} />}
          {visibleViews.includes("knowledge") && <NavButton active={activeView === "knowledge"} icon={<Database size={16} />} label={t.navKnowledge} onClick={() => setActiveView("knowledge")} />}
          {visibleViews.includes("runs") && <NavButton active={activeView === "runs"} icon={<Activity size={16} />} label={t.navRuns} onClick={() => setActiveView("runs")} />}
          {visibleViews.includes("settings") && <NavButton active={activeView === "settings"} icon={<Settings size={16} />} label={t.navSettings} onClick={() => setActiveView("settings")} />}
        </nav>
        <div className="userPanel">
          <strong>{currentUser.display_name}</strong>
          <span>{roleLabels[currentUser.role]}</span>
          <button type="button" onClick={logout}>退出登录</button>
        </div>
      </aside>

      <section className="content">
        <Topbar title={pageMeta.title} subtitle={pageMeta.subtitle} t={t} emails={emails} processedCount={processedCount} />

        {activeView === "inbox" && (
          <>
            <ReadySendBar
              readyCount={readyToSendEmails.length}
              viewedCount={viewedReadyCount}
              canSend={canBulkSendReady}
              sending={sendingId === "bulk"}
              locale={locale}
              onSend={bulkSendReadyReplies}
            />
            <div className="layout inboxLayout">
              <div className="moduleStack inboxStack">
                <EmailQueue
                  title={t.inboxQueue}
                  hint={t.inboxQueueHint}
                  emails={inboxEmails}
                  selectedId={selected?.id}
                  locale={locale}
                  setSelectedId={setSelectedId}
                  onRefresh={syncMail}
                  refreshing={syncing}
                  refreshTitle={`${t.refresh}（${mailSourceLabel(mailSourceState?.active_provider)}）`}
                  listScope={mailListScope}
                  activeProvider={mailSourceState?.active_provider}
                  onListScopeChange={setMailListScope}
                  primaryTitle={locale === "zh" ? "客服邮件" : "Support emails"}
                  primaryHint={locale === "zh" ? "需要进入客服处理流程的邮件。" : "Emails that should enter the support workflow."}
                  secondaryTitle={locale === "zh" ? "非客服邮件复核" : "Non-support review"}
                  secondaryHint={locale === "zh" ? "系统已过滤，可快速确认是否误判。" : "Filtered by the gate. Check for false negatives."}
                  secondaryEmails={irrelevantEmails}
                />
              </div>
              {selected && <EmailDetail selected={selected} currentUser={currentUser} locale={locale} t={t} review={review} updateEscalation={updateEscalation} sendReply={sendReply} sendingId={sendingId} regenerateReply={regenerateReply} regeneratingId={regeneratingId} regenerateProgress={regenerateProgress} />}
            </div>
          </>
        )}

        {activeView === "review" && (
          <div className="reviewLayout">
            <EmailQueue title={t.navReview} hint={t.reviewSubtitle} emails={reviewEmails} selectedId={selectedReview?.id} locale={locale} setSelectedId={setSelectedId} />
            {selectedReview ? <EmailDetail selected={selectedReview} currentUser={currentUser} locale={locale} t={t} review={review} updateEscalation={updateEscalation} sendReply={sendReply} sendingId={sendingId} regenerateReply={regenerateReply} regeneratingId={regeneratingId} regenerateProgress={regenerateProgress} /> : <EmptyModule title={t.navReview} text="当前没有需要审核的邮件。" />}
          </div>
        )}

        {activeView === "knowledge" && (
          <KnowledgePage
            docs={knowledgeDocs}
            versions={knowledgeVersions}
            file={knowledgeFile}
            revisionFile={knowledgeRevisionFile}
            editingId={editingKnowledgeId}
            editForm={knowledgeEditForm}
            form={knowledgeForm}
            inputMode={knowledgeInputMode}
            locale={locale}
            ingesting={ingesting}
            saving={savingKnowledge}
            uploading={uploadingKnowledge}
            uploadingRevisionId={uploadingKnowledgeRevisionId}
            deletingId={deletingKnowledgeId}
            reindexingId={reindexingKnowledgeId}
            restoringVersionId={restoringVersionId}
            deletingVersionId={deletingVersionId}
            setFile={setKnowledgeFile}
            setRevisionFile={setKnowledgeRevisionFile}
            setEditForm={setKnowledgeEditForm}
            setForm={setKnowledgeForm}
            setInputMode={setKnowledgeInputMode}
            onIngest={ingestKnowledge}
            onCreate={createKnowledgeDocument}
            onUpload={uploadKnowledgeDocument}
            onEdit={startEditKnowledgeDocument}
            onCancelEdit={() => {
              setEditingKnowledgeId("");
              setKnowledgeRevisionFile(null);
            }}
            onSave={saveKnowledgeDocument}
            onUploadRevision={uploadKnowledgeDocumentRevision}
            onReindex={reindexKnowledgeDocument}
            onDelete={deleteKnowledgeDocument}
            onRestore={restoreKnowledgeVersion}
            onDeleteVersion={deleteKnowledgeVersion}
          />
        )}

        {activeView === "runs" && <RunLogPage operationLogs={operationLogs} emails={emails} cleaningLogs={cleaningLogs} onCleanup={cleanupRunLogs} locale={locale} />}

        {activeView === "settings" && (
          <div className="settingsPage">
            <section className="settingsPanel">
              <div className="sectionTitle"><span>{t.settingsPanel}</span><Settings size={16} /></div>
              <div className="settingRow">
                <span>{t.languageSetting}</span>
                <button className="languageToggle" onClick={() => setLocale(locale === "en" ? "zh" : "en")} title={t.languageTitle}>
                  <Languages size={16} />{t.languageLabel}
                </button>
              </div>
              <div className="settingRow mailSourceRow">
                <div className="mailSourceSummary">
                  <span>邮件源</span>
                  <strong>{mailSourceLabel(mailSourceState?.active_provider)}</strong>
                  <small>切换前会验证收信与发信权限，失败时继续使用当前邮件源。</small>
                </div>
                <button className="mailSourceButton" type="button" onClick={() => openMailSourceModal()} title="配置或切换邮件源">
                  <PlusCircle size={16} />配置邮件源
                </button>
              </div>
            </section>
            <section className="opsPanel">
              <div><span>{t.workspaceLabel}</span><strong>{t.workspaceValue}</strong></div>
              <div><span>{t.slaLabel}</span><strong>{t.slaValue}</strong></div>
              <div><span>{t.modeLabel}</span><strong>{t.modeValue}</strong></div>
            </section>
          </div>
        )}

        {mailSourceModalOpen && (
          <MailSourceModal
            draft={mailSourceDraft}
            state={mailSourceState}
            saving={savingMailSource}
            error={mailSourceError}
            onSelectProvider={selectMailProvider}
            onChange={(field, value) => setMailSourceDraft((current) => ({ ...current, [field]: value }))}
            onClose={() => !savingMailSource && setMailSourceModalOpen(false)}
            onSubmit={saveMailSource}
          />
        )}
      </section>
    </main>
  );
}

function mailSourceLabel(provider?: MailProvider) {
  return provider === "outlook" ? "Outlook" : provider === "gmail" ? "Gmail" : "QQ 邮箱";
}

function emptyMailSourceDraft(provider: MailProvider): MailSourceDraft {
  const imapDefaults = provider === "gmail"
    ? { imap_host: "imap.gmail.com", imap_port: "993", smtp_host: "smtp.gmail.com", smtp_port: "465" }
    : provider === "outlook"
      ? { imap_host: "outlook.office365.com", imap_port: "993", smtp_host: "smtp-mail.outlook.com", smtp_port: "587" }
      : { imap_host: "imap.qq.com", imap_port: "993", smtp_host: "smtp.qq.com", smtp_port: "465" };
  return {
    provider,
    auth_mode: "imap_smtp",
    email_address: "",
    credential: "",
    tenant_id: "",
    client_id: "",
    client_secret: "",
    redirect_uri: provider === "gmail" || provider === "outlook" ? `${window.location.origin}/api/auth/${provider}/callback` : "",
    ...imapDefaults,
  };
}

function mailSourceDraftFromInfo(provider: MailProvider, source?: MailSourceInfo): MailSourceDraft {
  const defaults = emptyMailSourceDraft(provider);
  if (!source) return defaults;
  return {
    ...defaults,
    auth_mode: source.auth_mode || defaults.auth_mode,
    email_address: source.email_address || "",
    imap_host: source.imap_host || defaults.imap_host,
    imap_port: source.imap_port ? String(source.imap_port) : defaults.imap_port,
    smtp_host: source.smtp_host || defaults.smtp_host,
    smtp_port: source.smtp_port ? String(source.smtp_port) : defaults.smtp_port,
    tenant_id: source.tenant_id || "",
    client_id: source.client_id || "",
    redirect_uri: source.redirect_uri || "",
  };
}

function MailSourceModal({ draft, state, saving, error, onSelectProvider, onChange, onClose, onSubmit }: {
  draft: MailSourceDraft;
  state: MailSourceState | null;
  saving: boolean;
  error: string;
  onSelectProvider: (provider: MailProvider) => void;
  onChange: (field: keyof MailSourceDraft, value: string) => void;
  onClose: () => void;
  onSubmit: (event: React.FormEvent) => void;
}) {
  const source = state?.sources.find((item) => item.provider === draft.provider);
  const modeSecretConfigured = draft.auth_mode === "api_oauth" ? source?.oauth_secret_configured : source?.imap_secret_configured;
  const secretHint = modeSecretConfigured ? "留空则沿用已保存的认证信息" : "请输入认证信息";
  return (
    <div className="modalBackdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="mailSourceModal" role="dialog" aria-modal="true" aria-labelledby="mail-source-title">
        <header className="mailSourceModalHeader">
          <div><h2 id="mail-source-title">配置邮件源</h2><p>系统会先验证收信与发信权限，通过后才切换。</p></div>
          <button type="button" className="iconButton" onClick={onClose} title="关闭" aria-label="关闭"><X size={18} /></button>
        </header>
        <div className="providerTabs" role="tablist" aria-label="邮件服务商">
          {(["qq", "outlook", "gmail"] as MailProvider[]).map((provider) => (
            <button key={provider} type="button" role="tab" aria-selected={draft.provider === provider} className={`providerTab ${draft.provider === provider ? "active" : ""}`} onClick={() => onSelectProvider(provider)}>
              {mailSourceLabel(provider)}
              {state?.active_provider === provider && <span>当前</span>}
            </button>
          ))}
        </div>
        <form className="mailSourceForm" onSubmit={onSubmit}>
          <fieldset className="authModeFieldset">
            <legend>接入方式</legend>
            <div className="authModeSelector">
              <button type="button" aria-pressed={draft.auth_mode === "imap_smtp"} className={draft.auth_mode === "imap_smtp" ? "active" : ""} onClick={() => onChange("auth_mode", "imap_smtp")}>IMAP + SMTP</button>
              <button type="button" aria-pressed={draft.auth_mode === "api_oauth"} className={draft.auth_mode === "api_oauth" ? "active" : ""} disabled={draft.provider === "qq"} title={draft.provider === "qq" ? "QQ 邮箱暂不提供此 API OAuth 接入" : "通过官方 API 授权接入"} onClick={() => onChange("auth_mode", "api_oauth")}>API OAuth</button>
            </div>
            <small>{draft.provider === "qq" ? "QQ 邮箱使用授权码连接 IMAP/SMTP。" : draft.auth_mode === "api_oauth" ? "将跳转到服务商授权页，系统不会保存邮箱登录密码。" : "使用应用专用密码或邮箱授权码连接。"}</small>
          </fieldset>
          <label><span>邮箱地址</span><input type="email" required value={draft.email_address} onChange={(event) => onChange("email_address", event.target.value)} placeholder={draft.provider === "outlook" ? "name@outlook.com" : "name@example.com"} /></label>
          {draft.auth_mode === "api_oauth" ? (
            <div className="mailSourceFieldGrid">
              {draft.provider === "outlook" && <label><span>授权租户</span><input required value={draft.tenant_id || "consumers"} onChange={(event) => onChange("tenant_id", event.target.value)} placeholder="个人 Outlook 使用 consumers" /></label>}
              <label><span>Client ID</span><input required value={draft.client_id} onChange={(event) => onChange("client_id", event.target.value)} /></label>
              <label className="wideField"><span>Client Secret</span><input type="password" value={draft.client_secret} onChange={(event) => onChange("client_secret", event.target.value)} placeholder={secretHint} required={!modeSecretConfigured} /></label>
              <label className="wideField"><span>Redirect URI</span><input required value={draft.redirect_uri} onChange={(event) => onChange("redirect_uri", event.target.value)} /></label>
            </div>
          ) : (
            <div className="mailSourceFieldGrid">
              <label className="wideField"><span>{draft.provider === "qq" ? "QQ 邮箱授权码" : `${mailSourceLabel(draft.provider)} 应用专用密码`}</span><input type="password" value={draft.credential} onChange={(event) => onChange("credential", event.target.value)} placeholder={secretHint} required={!modeSecretConfigured} /></label>
              <label><span>IMAP 主机</span><input required value={draft.imap_host} onChange={(event) => onChange("imap_host", event.target.value)} /></label>
              <label><span>IMAP 端口</span><input required type="number" value={draft.imap_port} onChange={(event) => onChange("imap_port", event.target.value)} /></label>
              <label><span>SMTP 主机</span><input required value={draft.smtp_host} onChange={(event) => onChange("smtp_host", event.target.value)} /></label>
              <label><span>SMTP 端口</span><input required type="number" value={draft.smtp_port} onChange={(event) => onChange("smtp_port", event.target.value)} /></label>
            </div>
          )}
          {error && <div className="mailSourceError" role="alert">{error}</div>}
          <footer className="mailSourceActions">
            <button type="button" onClick={onClose} disabled={saving}>取消</button>
            <button type="submit" className="primary" disabled={saving}>{saving ? (draft.auth_mode === "api_oauth" ? "正在创建授权..." : "正在验证连接...") : (draft.auth_mode === "api_oauth" ? "前往授权" : "测试并切换")}</button>
          </footer>
        </form>
      </section>
    </div>
  );
}

function NavButton({ active, icon, label, onClick }: { active: boolean; icon: React.ReactNode; label: string; onClick: () => void }) {
  return <button className={`navItem ${active ? "active" : ""}`} onClick={onClick}>{icon}{label}</button>;
}

function ReadySendBar({
  readyCount,
  viewedCount,
  canSend,
  sending,
  locale,
  onSend,
}: {
  readyCount: number;
  viewedCount: number;
  canSend: boolean;
  sending: boolean;
  locale: Locale;
  onSend: () => void;
}) {
  if (readyCount === 0) return null;
  return (
    <section className="readySendBar">
      <div>
        <strong>{locale === "zh" ? "低风险可发送邮件" : "Low-risk ready replies"}</strong>
        <span>
          {locale === "zh"
            ? `已查看 ${viewedCount}/${readyCount} 封。全部查看后可批量确认发送。`
            : `Viewed ${viewedCount}/${readyCount}. Bulk send is enabled after every ready reply has been reviewed.`}
        </span>
      </div>
      <button className="primaryButton compact" onClick={onSend} disabled={!canSend || sending}>
        <Mail size={15} />{sending ? (locale === "zh" ? "发送中" : "Sending") : (locale === "zh" ? "全部确认发送" : "Send all reviewed")}
      </button>
    </section>
  );
}

function Topbar({ title, subtitle, t, emails, processedCount }: { title: string; subtitle: string; t: Record<string, string>; emails: EmailRecord[]; processedCount: number }) {
  return (
    <div className="topbar">
      <div><h1>{title}</h1><p>{subtitle}</p></div>
      <div className="topbarActions">
        <div className="metricRow">
          <Metric label={t.totalEmails} value={emails.length} />
          <Metric label={t.inReview} value={emails.filter((email) => email.status === "human_review").length} />
          <Metric label={t.highPriority} value={emails.filter((email) => email.priority === "high").length} />
          <Metric label={t.processed} value={processedCount} />
        </div>
      </div>
    </div>
  );
}

function hasEscalationHistory(email: EmailRecord) {
  // 仍处于有效升级链路的邮件需要更高权限发送。
  // manager/admin 取消升级后，最后一次升级相关动作会是 undo_escalate，
  // 此时恢复为普通审核邮件，Agent 审核通过后可以发送。
  // 前端用于禁用 agent 发送入口；后端也有同样校验，避免绕过页面限制。
  const escalationActions = (email.review_actions || []).filter((action) => action.action === "escalate" || action.action === "undo_escalate");
  if (escalationActions.length > 0) {
    return escalationActions[escalationActions.length - 1].action === "escalate";
  }
  return Boolean(email.escalation_ticket && ["open", "assigned", "resolved"].includes(email.escalation_ticket.status));
}

function hasManualApproval(email: EmailRecord) {
  return (email.review_actions || []).some((action) => action.action === "approve");
}

function hasActiveEscalation(email: EmailRecord) {
  return email.status === "escalated" || Boolean(email.escalation_ticket && ["open", "assigned"].includes(email.escalation_ticket.status));
}

function isEscalationEmail(email: EmailRecord) {
  return hasActiveEscalation(email) || hasEscalationHistory(email);
}

function isLowRiskReadyToSend(email: EmailRecord) {
  return (
    email.status === "ready_to_send" &&
    email.priority === "low" &&
    email.confidence >= 0.78 &&
    hasStrongKnowledgeGrounding(email) &&
    !hasManualApproval(email) &&
    !hasEscalationHistory(email)
  );
}

function hasStrongKnowledgeGrounding(email: EmailRecord) {
  return email.knowledge_hits.some((hit) => (hit.reliability ?? "weak") === "strong");
}

function canUserSendReply(user: UserProfile | null, email: EmailRecord) {
  if (email.status !== "ready_to_send") return false;
  if (user?.role === "agent" && hasEscalationHistory(email)) return false;
  return true;
}

function KnowledgePage(props: {
  // 知识库页面集中处理文档上传、手动录入、编辑、版本和重建索引。
  docs: KnowledgeDocument[];
  versions: Record<string, KnowledgeDocumentVersion[]>;
  file: File | null;
  revisionFile: File | null;
  editingId: string;
  editForm: { title: string; content: string };
  form: { title: string; source: string; content: string };
  inputMode: KnowledgeInputMode;
  locale: Locale;
  ingesting: boolean;
  saving: boolean;
  uploading: boolean;
  uploadingRevisionId: string;
  deletingId: string;
  reindexingId: string;
  restoringVersionId: string;
  deletingVersionId: string;
  setFile: (file: File | null) => void;
  setRevisionFile: (file: File | null) => void;
  setEditForm: (form: { title: string; content: string }) => void;
  setForm: (form: { title: string; source: string; content: string }) => void;
  setInputMode: (mode: KnowledgeInputMode) => void;
  onIngest: () => void;
  onCreate: () => void;
  onUpload: () => void;
  onEdit: (doc: KnowledgeDocument) => void;
  onCancelEdit: () => void;
  onSave: (docId: string) => void;
  onUploadRevision: (doc: KnowledgeDocument) => void;
  onReindex: (doc: KnowledgeDocument) => void;
  onDelete: (doc: KnowledgeDocument) => void;
  onRestore: (doc: KnowledgeDocument, version: KnowledgeDocumentVersion) => void;
  onDeleteVersion: (doc: KnowledgeDocument, version: KnowledgeDocumentVersion) => void;
}) {
  return (
    <div className="knowledgeLayout">
      <section className="knowledgePanel">
        <div className="sectionTitle">
          <span>知识文档</span>
          <button className="secondaryButton compact" onClick={props.onIngest} disabled={props.ingesting}>
            <RefreshCcw size={16} />{props.ingesting ? "入库中" : "重新入库"}
          </button>
        </div>
        <div className="tabSwitch">
          <button className={props.inputMode === "upload" ? "active" : ""} onClick={() => props.setInputMode("upload")}>上传文件</button>
          <button className={props.inputMode === "manual" ? "active" : ""} onClick={() => props.setInputMode("manual")}>手动录入</button>
        </div>
        <div className={`knowledgeUpload ${props.inputMode === "upload" ? "" : "hidden"}`}>
          <label>上传知识文件
            <input type="file" accept=".md,.txt,.pdf,.docx,.doc,text/markdown,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword" onChange={(event) => props.setFile(event.target.files?.[0] ?? null)} />
          </label>
          <button className="secondaryButton" onClick={props.onUpload} disabled={!props.file || props.uploading}>
            <UploadCloud size={16} />{props.uploading ? "上传中" : "上传并入库"}
          </button>
          {props.file && <span className="fileHint">{props.file.name}</span>}
        </div>
        <div className={`knowledgeCreate ${props.inputMode === "manual" ? "" : "hidden"}`}>
          <label>新知识标题<input value={props.form.title} onChange={(event) => props.setForm({ ...props.form, title: event.target.value })} /></label>
          <label>文件名<input value={props.form.source} onChange={(event) => props.setForm({ ...props.form, source: event.target.value })} /></label>
          <label>知识内容<textarea rows={6} value={props.form.content} onChange={(event) => props.setForm({ ...props.form, content: event.target.value })} /></label>
          <button className="primaryButton" onClick={props.onCreate} disabled={props.saving || !props.form.title.trim() || props.form.content.trim().length < 20}>
            <PlusCircle size={16} />{props.saving ? "添加中" : "添加知识"}
          </button>
        </div>
        <div className="knowledgeTable">
          {props.docs.map((doc) => (
            <React.Fragment key={doc.id}>
              <div className="knowledgeRow">
                <Database size={16} />
                <div><strong>{doc.title}</strong><span>{doc.source}</span><KnowledgeReportSummary doc={doc} /></div>
                <small className={`indexBadge ${normalizeKnowledgeStatus(doc.status)}`} title={doc.status_message || "已完成索引"}>
                  {knowledgeStatusLabels[props.locale][normalizeKnowledgeStatus(doc.status)]}
                </small>
                <div className="knowledgeActions">
                  {(normalizeKnowledgeStatus(doc.status) === "failed" || normalizeKnowledgeStatus(doc.status) === "needs_reindex") && (
                    <button onClick={() => props.onReindex(doc)} disabled={props.reindexingId === doc.id} title="重新索引"><RefreshCcw size={15} /></button>
                  )}
                  <button onClick={() => props.onEdit(doc)} title="编辑知识"><Pencil size={15} /></button>
                  <button onClick={() => props.onDelete(doc)} disabled={props.deletingId === doc.id} title="删除知识"><Trash2 size={15} /></button>
                </div>
              </div>
              {props.editingId === doc.id && (
                <div className="knowledgeEditPanel">
                  <KnowledgeParseReportPanel report={doc.parse_report} chunkCount={doc.chunk_count} />
                  <KnowledgeVersionHistory
                    versions={props.versions[doc.id] || []}
                    restoringVersionId={props.restoringVersionId}
                    deletingVersionId={props.deletingVersionId}
                    onRestore={(version) => props.onRestore(doc, version)}
                    onDelete={(version) => props.onDeleteVersion(doc, version)}
                  />
                  <div className="revisionUpload">
                    <label>上传文件生成新版本
                      <input type="file" accept=".md,.txt,.pdf,.docx,.doc,text/markdown,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword" onChange={(event) => props.setRevisionFile(event.target.files?.[0] ?? null)} />
                    </label>
                    <button className="secondaryButton compact" onClick={() => props.onUploadRevision(doc)} disabled={!props.revisionFile || props.uploadingRevisionId === doc.id}>
                      <UploadCloud size={15} />{props.uploadingRevisionId === doc.id ? "上传中" : "上传更新版本"}
                    </button>
                    {props.revisionFile && <span className="fileHint">{props.revisionFile.name}</span>}
                  </div>
                  <label>文档标题<input value={props.editForm.title} onChange={(event) => props.setEditForm({ ...props.editForm, title: event.target.value })} /></label>
                  <label>文档内容<textarea rows={8} value={props.editForm.content} onChange={(event) => props.setEditForm({ ...props.editForm, content: event.target.value })} /></label>
                  <div className="editActions">
                    <button className="secondaryButton compact" onClick={props.onCancelEdit}><XCircle size={15} />取消</button>
                    <button className="primaryButton compact" onClick={() => props.onSave(doc.id)} disabled={!props.editForm.title.trim() || props.editForm.content.trim().length < 20}>保存修改</button>
                  </div>
                </div>
              )}
            </React.Fragment>
          ))}
          {props.docs.length === 0 && <div className="empty"><Search size={18} /><span>暂无知识文档，请先入库。</span></div>}
        </div>
      </section>
    </div>
  );
}

function KnowledgeReportSummary({ doc }: { doc: KnowledgeDocument }) {
  const report = doc.parse_report || {};
  const fileType = report.file_type || doc.source.split(".").pop() || "file";
  return (
    <div className="docMeta">
      <span>{fileType.toUpperCase()}</span>
      <span>{report.parser || "未生成报告"}</span>
      <span>v{doc.current_version || 0}</span>
      <span>{doc.chunk_count} 个片段</span>
      {typeof report.cleaned_chars === "number" && <span>{formatCount(report.cleaned_chars)} 字符</span>}
    </div>
  );
}

function KnowledgeParseReportPanel({ report, chunkCount }: { report?: KnowledgeParseReport; chunkCount: number }) {
  // 解析报告用于解释文档是如何被清洗和切分的，方便用户发现 OCR/表格/图片问题。
  if (!report || Object.keys(report).length === 0) {
    return <div className="parseReport"><strong>解析报告</strong><span>暂无解析报告，重新索引后会生成。</span></div>;
  }
  const warnings = report.warnings || [];
  return (
    <div className="parseReport">
      <div className="parseReportHeader"><strong>解析报告</strong><span>{report.file_type?.toUpperCase() || "FILE"} · {report.parser || "unknown parser"}</span></div>
      <div className="reportGrid">
        <span>原始字符 <strong>{formatCount(report.original_chars)}</strong></span>
        <span>清洗后字符 <strong>{formatCount(report.cleaned_chars)}</strong></span>
        <span>片段数 <strong>{formatCount(chunkCount)}</strong></span>
        <span>噪声行 <strong>{formatCount(report.noise_lines_removed)}</strong></span>
        <span>页数 <strong>{formatCount(report.page_count)}</strong></span>
        <span>有文本页 <strong>{formatCount(report.pages_with_text)}</strong></span>
        <span>章节数 <strong>{formatCount(report.section_count)}</strong></span>
        <span>表格数 <strong>{formatCount(report.table_count)}</strong></span>
      </div>
      {warnings.length > 0 && <div className="reportWarnings">{warnings.map((warning) => <span key={warning}>{warning}</span>)}</div>}
    </div>
  );
}

function KnowledgeVersionHistory({
  versions,
  restoringVersionId,
  deletingVersionId,
  onRestore,
  onDelete,
}: {
  versions: KnowledgeDocumentVersion[];
  restoringVersionId: string;
  deletingVersionId: string;
  onRestore: (version: KnowledgeDocumentVersion) => void;
  onDelete: (version: KnowledgeDocumentVersion) => void;
}) {
  return (
    <div className="versionHistory">
      <div className="parseReportHeader"><strong>可回退版本</strong><span>{versions.length > 0 ? `${versions.length} 个版本` : "暂无版本记录"}</span></div>
      {versions.length === 0 && <span className="versionEmpty">更新文档后会生成可回退版本。</span>}
      {versions.map((version, index) => {
        const isCurrent = index === 0;
        return (
          <div className="versionItem" key={version.id}>
            <div>
              <strong>v{version.version_number}{isCurrent ? " 当前版本" : ""}</strong>
              <span>{new Date(version.created_at).toLocaleString("zh-CN")}</span>
              {!isCurrent && (
                <div className="versionActions">
                  <button className="versionRestoreButton" onClick={() => onRestore(version)} disabled={restoringVersionId === version.id}>
                    <RefreshCcw size={14} />{restoringVersionId === version.id ? "回退中" : "回退到此版本"}
                  </button>
                  <button className="versionDeleteButton" onClick={() => onDelete(version)} disabled={deletingVersionId === version.id}>
                    <Trash2 size={14} />{deletingVersionId === version.id ? "删除中" : "删除版本"}
                  </button>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AgentCostPanel({ metrics, locale }: { metrics: AgentMetrics; locale: Locale }) {
  // 成本面板只做估算展示，真实账单仍以模型平台为准。
  const totalTokens = metrics.input_tokens + metrics.output_tokens + metrics.embedding_tokens;
  const items = locale === "zh"
    ? [
        ["Token", `${totalTokens}`],
        ["LLM 总数", `${metrics.llm_calls}`],
        ["RAG 耗时", formatLatency(metrics.rag_latency_ms)],
        ["单次成本", formatCost(metrics.estimated_cost_cny)],
      ]
    : [
        ["Tokens", `${totalTokens}`],
        ["LLM total", `${metrics.llm_calls}`],
        ["RAG latency", formatLatency(metrics.rag_latency_ms)],
        ["Run cost", formatCost(metrics.estimated_cost_cny)],
      ];
  return (
    <section className="costPanel">
      <div>
        <strong>{locale === "zh" ? "Agent 成本与 token 监控" : "Agent Cost and Token Monitor"}</strong>
        <span>{locale === "zh" ? "按当前邮件估算，实际账单以模型平台为准。" : "Estimated for this email; provider billing is authoritative."}</span>
      </div>
      <div className="costMetrics">
        {items.map(([label, value]) => (
          <div className="costMetric" key={label}>
            <span>{label}</span>
            <strong className={label.includes("成本") || label.includes("cost") || label.includes("RAG") ? "costValue" : ""} title={value}>{value}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

const processingPhaseIds = ["preprocess", "relevance_gate", "semantic_analysis", "retrieve", "draft", "review"] as const;

function isEmailProcessing(email: EmailRecord) {
  return email.processing_status === "queued" || email.processing_status === "running";
}

function processingStageIndex(stage: string) {
  const aliases: Record<string, number> = {
    queued: -1,
    preprocess: 0,
    relevance_gate: 1,
    semantic_analysis: 2,
    semantic_relevance_gate: 2,
    retrieve: 3,
    draft: 4,
    review: 5,
    completed: 6,
  };
  return aliases[stage] ?? -1;
}

function AgentProcessingPanel({ email, locale }: { email: EmailRecord; locale: Locale }) {
  const failed = email.processing_status === "failed";
  const currentIndex = processingStageIndex(email.processing_stage);
  const labels = locale === "zh"
    ? ["低成本预处理", "客服场景判断", "语义分析", "检索知识库", "生成回复草稿", "审核门控"]
    : ["Low-cost preprocessing", "Support relevance", "Semantic analysis", "Knowledge retrieval", "Draft generation", "Review gate"];
  const progress = Math.max(0, Math.min(email.processing_progress || 0, 100));

  return (
    <section className={`agentProcessingPanel ${failed ? "failed" : ""}`} aria-live="polite">
      <div className="agentProcessingHeader">
        <div>
          {failed ? <XCircle size={18} /> : <Activity size={18} />}
          <strong>{failed ? (locale === "zh" ? "Agent 处理失败" : "Agent processing failed") : (locale === "zh" ? "Agent 正在处理" : "Agent is processing")}</strong>
        </div>
        <strong>{progress}%</strong>
      </div>
      <div className="agentProgressTrack" aria-label={`${progress}%`}>
        <div style={{ width: `${progress}%` }} />
      </div>
      <p>{email.processing_message || (locale === "zh" ? "正在处理邮件" : "Processing email")}</p>
      <div className="agentPhaseList">
        {processingPhaseIds.map((phase, index) => {
          const complete = email.processing_status === "completed" || index < currentIndex;
          const active = !complete && index === currentIndex;
          const phaseFailed = failed && active;
          return (
            <div key={phase} className={`agentPhase ${complete ? "complete" : active ? "active" : "waiting"} ${phaseFailed ? "failed" : ""}`}>
              {complete ? <CheckCircle2 size={16} /> : phaseFailed ? <XCircle size={16} /> : active ? <Activity size={16} /> : <Clock3 size={16} />}
              <span>{labels[index]}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function RunLogPage({ operationLogs, emails, cleaningLogs, onCleanup, locale }: { operationLogs: OperationLog[]; emails: EmailRecord[]; cleaningLogs: boolean; onCleanup: () => void; locale: Locale }) {
  // 运行日志把“邮件 Agent 轨迹”和“知识库操作”拆开展示，避免审核历史挤在邮件详情里。
  const knowledgeLogs = operationLogs.filter((log) => log.scope === "knowledge");
  const agentTraces = emails.flatMap((email) => email.steps.map((step) => ({ email, step })));
  const todayEmails = emails.filter((email) => isTodayOrRecent(email.updated_at));
  const todayCost = emails
    .filter((email) => todayEmails.includes(email))
    .reduce((sum, email) => sum + normalizeAgentMetrics(email.agent_metrics).estimated_cost_cny, 0);
  const todayTokens = emails
    .filter((email) => todayEmails.includes(email))
    .reduce((sum, email) => {
      const metrics = normalizeAgentMetrics(email.agent_metrics);
      return sum + metrics.input_tokens + metrics.output_tokens + metrics.embedding_tokens;
    }, 0);
  return (
    <div className="runLog">
      <section className="costPanel runCostPanel">
        <div>
          <strong>今日成本汇总</strong>
          <span>统计今日处理或更新过的邮件，作为开发期成本观察。</span>
        </div>
        <div className="costMetrics">
          <div className="costMetric"><span>今日 Token</span><strong>{todayTokens}</strong></div>
          <div className="costMetric"><span>今日总成本</span><strong>¥{todayCost.toFixed(6)}</strong></div>
        </div>
      </section>
      <section className="runPolicy">
        <div>
          <strong>日志保留策略</strong>
          <span>邮件 Agent 轨迹保留 30 天；知识库操作保留 180 天。清理只处理过期记录。</span>
        </div>
        <button className="secondaryButton compact" onClick={onCleanup} disabled={cleaningLogs}>
          <Trash2 size={14} />{cleaningLogs ? "清理中" : "清理过期日志"}
        </button>
      </section>
      <section className="runGroup">
        <div className="sectionTitle"><span>知识库操作</span><Database size={16} /></div>
        <div className="runScrollArea">
          {knowledgeLogs.map((log) => <OperationLogRow key={log.id} log={log} />)}
          {knowledgeLogs.length === 0 && <div className="empty"><Activity size={18} /><span>暂无知识库操作记录。</span></div>}
        </div>
      </section>
      <section className="runGroup">
        <div className="sectionTitle"><span>邮件 Agent 轨迹</span><Activity size={16} /></div>
        <div className="runScrollArea agentTraceList">
        {agentTraces.map(({ email, step }) => {
          const display = formatWorkflowStep(step, locale);
          return (
            <section className="runRow" key={`${email.id}-${step.name}-${step.timestamp}`}>
              <Activity size={16} />
              <div><strong>{display.name}</strong><span>{formatEmailSubject(email.subject, locale)}</span><p>{display.summary}</p></div>
              <small>{Math.round(step.confidence * 100)}%</small>
            </section>
          );
        })}
        {agentTraces.length === 0 && <div className="empty"><Activity size={18} /><span>暂无邮件 Agent 轨迹。</span></div>}
        </div>
      </section>
    </div>
  );
}

function OperationLogRow({ log }: { log: OperationLog }) {
  const restoredFrom = log.detail?.restored_from_version;
  const newVersion = log.detail?.new_version;
  const currentVersion = log.detail?.current_version;
  const deletedVersion = log.detail?.deleted_version;
  return (
    <section className="runRow operationRunRow">
      <Database size={16} />
      <div>
        <strong>{log.title}</strong>
        <span>{log.summary}</span>
        {restoredFrom && newVersion && <p>来源版本 v{restoredFrom}，当前生成 v{newVersion}</p>}
        {restoredFrom && currentVersion && <p>来源版本 v{restoredFrom}，当前版本 v{currentVersion}</p>}
        {deletedVersion && <p>已删除历史版本 v{deletedVersion}</p>}
      </div>
      <small>{new Date(log.created_at).toLocaleString("zh-CN")}</small>
    </section>
  );
}

function normalizeAgentMetrics(metrics?: Partial<AgentMetrics>): AgentMetrics {
  const llmCalls = metrics?.llm_calls ?? 0;
  const draftCalls = metrics?.draft_llm_calls ?? 0;
  return {
    llm_calls: llmCalls,
    semantic_llm_calls: metrics?.semantic_llm_calls ?? Math.max(llmCalls - draftCalls, 0),
    draft_llm_calls: draftCalls,
    embedding_calls: metrics?.embedding_calls ?? 0,
    input_tokens: metrics?.input_tokens ?? 0,
    output_tokens: metrics?.output_tokens ?? 0,
    embedding_tokens: metrics?.embedding_tokens ?? 0,
    rag_latency_ms: metrics?.rag_latency_ms ?? 0,
    estimated_cost_cny: metrics?.estimated_cost_cny ?? 0,
  };
}

function isTodayOrRecent(value?: string) {
  if (!value) return false;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return false;
  const now = new Date();
  const sameLocalDay = date.toDateString() === now.toDateString();
  const within24Hours = now.getTime() - date.getTime() >= 0 && now.getTime() - date.getTime() <= 24 * 60 * 60 * 1000;
  return sameLocalDay || within24Hours;
}

function normalizedHitScores(hit: KnowledgeHit) {
  if (hit.semantic_score || hit.keyword_score || hit.category_score) {
    return {
      semantic: hit.semantic_score ?? 0,
      keyword: hit.keyword_score ?? 0,
      category: hit.category_score ?? 0,
    };
  }
  return {
    semantic: Math.min(0.99, hit.score * 0.65),
    keyword: Math.min(0.99, hit.score * 0.25),
    category: hit.category && hit.category !== "other" ? 0.1 : 0,
  };
}

function KnowledgeHitCard({ hit, locale }: { hit: KnowledgeHit; locale: Locale }) {
  const categoryLabel = hit.category ? categoryLabels[locale][hit.category] || hit.category : categoryLabels[locale].other;
  const scores = normalizedHitScores(hit);
  const reliability = hit.reliability ?? "weak";
  const reliabilityLabels = {
    zh: { strong: "强依据", medium: "参考依据", weak: "弱相关" },
    en: { strong: "Strong", medium: "Reference", weak: "Weak" },
  } as const;
  return (
    <div className={`source ${reliability}`}>
      <div><strong>{hit.title}</strong><span>{hit.source}</span></div>
      <small className={`reliabilityBadge ${reliability}`}>{reliabilityLabels[locale][reliability]} · {Math.round(hit.score * 100)}%</small>
      <p>{hit.snippet}</p>
      <div className="hitReason">{hit.match_reason || "综合语义、关键词和分类信号命中"}</div>
      <div className="hitMetrics">
        <span>语义 {Math.round(scores.semantic * 100)}%</span>
        <span>关键词 {Math.round(scores.keyword * 100)}%</span>
        <span>分类 {Math.round(scores.category * 100)}%</span>
        <span>{categoryLabel}</span>
        {hit.page_number && <span>第 {hit.page_number} 页</span>}
        {hit.section_title && <span>{hit.section_title}</span>}
      </div>
    </div>
  );
}

function EmailQueue({
  // 收件箱队列支持两个折叠列表：客服邮件和非客服邮件复核。
  // 每个列表内部独立滚动，避免两个列表同时展开时互相遮挡。
  title,
  hint,
  emails,
  selectedId,
  locale,
  setSelectedId,
  primaryTitle,
  primaryHint,
  secondaryTitle,
  secondaryHint,
  secondaryEmails = [],
  onRefresh,
  refreshing = false,
  refreshTitle,
  listScope,
  activeProvider,
  onListScopeChange,
}: {
  title: string;
  hint: string;
  emails: EmailRecord[];
  selectedId?: string;
  locale: Locale;
  setSelectedId: (id: string) => void;
  onRefresh?: () => void;
  refreshing?: boolean;
  refreshTitle?: string;
  listScope?: "active" | "all";
  activeProvider?: MailProvider;
  onListScopeChange?: (scope: "active" | "all") => void;
  primaryTitle?: string;
  primaryHint?: string;
  secondaryTitle?: string;
  secondaryHint?: string;
  secondaryEmails?: EmailRecord[];
}) {
  // 两个邮件分组使用独立展开状态，允许同时展开或同时收起。
  const [openQueueSections, setOpenQueueSections] = useState({ primary: true, secondary: false });
  const renderEmailItem = (email: EmailRecord) => (
    <button key={email.id} className={`emailItem ${selectedId === email.id ? "active" : ""}`} onClick={() => setSelectedId(email.id)}>
      <span className={`priority ${email.priority}`}>{priorityLabels[locale][email.priority]}</span>
      <strong>{formatEmailSubject(email.subject, locale)}</strong>
      <span>{email.customer_name}</span>
      <small>{isEmailProcessing(email) ? email.processing_message : statusLabels[locale][email.status]}</small>
      {isEmailProcessing(email) && (
        <div className="queueProcessingProgress" aria-label={`${email.processing_progress}%`}>
          <div><span>{locale === "zh" ? "处理中" : "Processing"}</span><strong>{email.processing_progress}%</strong></div>
          <div className="queueProgressTrack"><span style={{ width: `${email.processing_progress}%` }} /></div>
        </div>
      )}
    </button>
  );

  return (
    <div className="queuePane">
      <div className="queueHeader">
        <div><h3>{title}</h3><span>{hint}</span></div>
        <div className="queueHeaderActions">
          {onListScopeChange && (
            <select
              className="mailScopeSelect"
              value={listScope || "active"}
              aria-label={locale === "zh" ? "邮件来源范围" : "Mail source scope"}
              onChange={(event) => onListScopeChange(event.target.value as "active" | "all")}
            >
              <option value="active">
                {locale === "zh" ? `当前：${mailSourceLabel(activeProvider)}` : `Current: ${mailSourceLabel(activeProvider)}`}
              </option>
              <option value="all">{locale === "zh" ? "全部历史" : "All history"}</option>
            </select>
          )}
          {onRefresh && (
            <button
              className={`iconButton tooltipButton refreshMailButton ${refreshing ? "refreshing" : ""}`}
              onClick={() => onRefresh()}
              disabled={refreshing}
              aria-busy={refreshing}
              aria-label={refreshTitle || (locale === "zh" ? "刷新邮件" : "Refresh emails")}
              data-tooltip={refreshTitle || (locale === "zh" ? "刷新邮件" : "Refresh emails")}
            >
              <RefreshCcw size={16} />
            </button>
          )}
        </div>
      </div>
      <section className={`queueSection ${openQueueSections.primary ? "open" : ""}`}>
        <button
          className="queueSectionHeader"
          type="button"
          aria-expanded={openQueueSections.primary}
          onClick={() => setOpenQueueSections((current) => ({ ...current, primary: !current.primary }))}
        >
          <div><strong>{primaryTitle || title}</strong>{primaryHint && <span>{primaryHint}</span>}</div>
          <small>{emails.length}</small>
        </button>
        {openQueueSections.primary && (
          <div className="emailList queueSectionList">
            {emails.map(renderEmailItem)}
            {emails.length === 0 && <div className="queueEmpty">{locale === "zh" ? "暂无客服邮件" : "No support emails"}</div>}
          </div>
        )}
      </section>
      {secondaryTitle && (
        <section className={`queueSection secondaryQueueSection ${openQueueSections.secondary ? "open" : ""}`}>
          <button
            className="queueSectionHeader"
            type="button"
            aria-expanded={openQueueSections.secondary}
            onClick={() => setOpenQueueSections((current) => ({ ...current, secondary: !current.secondary }))}
          >
            <div><strong>{secondaryTitle}</strong>{secondaryHint && <span>{secondaryHint}</span>}</div>
            <small>{secondaryEmails.length}</small>
          </button>
          {openQueueSections.secondary && (
            <div className="emailList queueSectionList secondaryEmailList">
              {secondaryEmails.map(renderEmailItem)}
              {secondaryEmails.length === 0 && <div className="queueEmpty">{locale === "zh" ? "暂无非客服邮件" : "No non-support emails"}</div>}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function EmailDetail({ selected, currentUser, locale, t, review, updateEscalation, sendReply, sendingId, regenerateReply, regeneratingId, regenerateProgress }: {
  // 邮件详情是人工审核的主工作台：展示原文、成本指标、草稿、风险、历史和执行流程。
  selected: EmailRecord;
  currentUser: UserProfile | null;
  locale: Locale;
  t: Record<string, string>;
  review: (action: "approve" | "escalate" | "revise" | "undo_escalate", note?: string, revisedReply?: string) => void;
  updateEscalation: (emailId: string, action: "assign" | "resolve" | "return_to_review", note?: string) => void;
  sendReply: (emailId: string) => void;
  sendingId: string;
  regenerateReply: (emailId: string) => void;
  regeneratingId: string;
  regenerateProgress: number;
}) {
  const [reviewNote, setReviewNote] = useState(safeEditableText(selected.review_note));
  const [revisedReply, setRevisedReply] = useState(safeEditableText(selected.draft_reply));
  const [showExecutionDetails, setShowExecutionDetails] = useState(false);
  const [showAllReviewHistory, setShowAllReviewHistory] = useState(false);
  const agentEscalationSendBlocked = currentUser?.role === "agent" && hasEscalationHistory(selected);
  const canSend = canUserSendReply(currentUser, selected);
  const sendDisabledTitle = selected.status !== "ready_to_send"
    ? "请先审核通过后再发送"
    : agentEscalationSendBlocked
      ? "升级处理过的邮件需要客服主管或管理员发送"
      : "";
  const attachments = selected.attachments || [];
  const reviewActions = selected.review_actions || [];
  const visibleReviewActions = showAllReviewHistory ? reviewActions : reviewActions.slice(-3);
  const isIrrelevant = selected.status === "irrelevant";
  const isProcessing = isEmailProcessing(selected);
  const metrics = normalizeAgentMetrics(selected.agent_metrics);
  const isGeneratingReply = regeneratingId === selected.id;
  const ticket = selected.escalation_ticket;
  const activeEscalation = hasActiveEscalation(selected);
  const canHandleEscalation = currentUser?.role === "manager" || currentUser?.role === "admin";

  useEffect(() => {
    setReviewNote(safeEditableText(selected.review_note));
    setRevisedReply(safeEditableText(selected.draft_reply));
    setShowExecutionDetails(false);
    setShowAllReviewHistory(false);
  }, [selected.id, selected.review_note, selected.draft_reply]);

  useEffect(() => {
    if (selected.status === "irrelevant") return;
    if (selected.status === "new" || !selected.category || selected.category === "other") return;
    if (safeEditableText(selected.draft_reply).trim() || isGeneratingReply) return;
    regenerateReply(selected.id);
  }, [selected.id, selected.status, selected.draft_reply, isGeneratingReply, regenerateReply]);

  return (
    <article className={`detail ${isProcessing ? "processing" : ""}`}>
      <header className="detailHeader">
        <div>
          <div className="statusLine"><StatusIcon status={selected.status} /><span>{statusLabels[locale][selected.status]}</span></div>
          <h2>{formatEmailSubject(selected.subject, locale)}</h2>
          <p>{selected.customer_name} · {selected.customer_email}</p>
        </div>
        <div className="chipGroup"><span className="chip">{isIrrelevant ? statusLabels[locale].irrelevant : formatCategory(selected.category, locale)}</span><span className="chip">{t.confidence} {Math.round(selected.confidence * 100)}%</span></div>
      </header>
      <section className="messageBox">
        <h3>{t.incomingMessage}</h3>
        <p>{formatEmailBody(selected.body, locale)}</p>
        {attachments.length > 0 && (
          <div className="attachmentList">
            <strong>附件</strong>
            {attachments.map((attachment) => (
              <div className="attachmentItem" key={`${selected.id}-${attachment.filename}`}>
                <div>
                  <span>{attachment.filename}</span>
                  <small>{attachment.content_type || "unknown"} · {formatBytes(attachment.size_bytes || 0)}</small>
                </div>
                <span className={`attachmentStatus ${normalizeAttachmentStatus(attachment.parse_status)}`}>
                  {attachmentStatusLabels[normalizeAttachmentStatus(attachment.parse_status)]}
                </span>
                {attachment.status_message && <small className="attachmentMessage">{attachment.status_message}</small>}
                {attachment.text_preview && <p>{attachment.text_preview}</p>}
              </div>
            ))}
          </div>
        )}
      </section>
      {(isProcessing || selected.processing_status === "failed") && <AgentProcessingPanel email={selected} locale={locale} />}
      <AgentCostPanel metrics={metrics} locale={locale} />
      <section className="replyPanel">
        <div className="replyHeader">
          <h3>{t.draftReply}</h3>
          {!isIrrelevant && <div className="reviewActions">
            <button onClick={() => review("approve", reviewNote, revisedReply)} title={activeEscalation ? "请先取消升级，或由客服主管处理升级工单后再通过" : ""}><ShieldCheck size={16} />{t.approve}</button>
            {(!activeEscalation || canHandleEscalation) && (
              <button onClick={() => review(activeEscalation ? "undo_escalate" : "escalate", reviewNote, revisedReply)}>
                <UserCheck size={16} />{activeEscalation ? t.undoEscalate : t.escalate}
              </button>
            )}
            <button onClick={() => regenerateReply(selected.id)} disabled={regeneratingId === selected.id}>
              <RefreshCcw size={16} />{regeneratingId === selected.id ? t.regeneratingReply : t.regenerateReply}
            </button>
            <button onClick={() => sendReply(selected.id)} disabled={sendingId === selected.id || !canSend} title={sendDisabledTitle}><Mail size={16} />{sendingId === selected.id ? t.sendingReply : t.sendReply}</button>
          </div>}
        </div>
        {isIrrelevant ? (
          <div className="irrelevantNotice">
            <strong>{locale === "zh" ? "无需生成客服回复" : "No customer reply needed"}</strong>
            <span>{locale === "zh" ? "该邮件被识别为平台通知、安全提醒或营销邮件，不进入 RAG 回复链路。" : "This email was identified as a notification, security alert, or marketing email and was excluded from the RAG reply flow."}</span>
          </div>
        ) : <div className="reviewEditor">
          <label>
            <span>审核备注</span>
            <input value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="记录审核结论、风险点或交接说明" />
          </label>
          <label>
            <span>修改后的回复草稿</span>
            {isGeneratingReply && (
              <div className="draftProgress" aria-live="polite">
                <div>
                  <span>{locale === "zh" ? "正在生成回复草稿" : "Generating reply draft"}</span>
                  <strong>{regenerateProgress}%</strong>
                </div>
                <div className="draftProgressTrack">
                  <div style={{ width: `${regenerateProgress}%` }} />
                </div>
              </div>
            )}
            <textarea value={revisedReply} onChange={(event) => setRevisedReply(event.target.value)} rows={8} wrap="soft" />
          </label>
        </div>}
        {!isIrrelevant && selected.risk_flags.length > 0 && <div className="riskBox"><strong>{t.riskFlags}</strong>{selected.risk_flags.map((flag) => <span key={flag}>{formatRiskFlag(flag, locale)}</span>)}</div>}
        {!isIrrelevant && safeDisplayText(selected.review_note) && <p className="reviewNote">{safeDisplayText(selected.review_note)}</p>}
        {reviewActions.length > 0 && (
          <div className="reviewHistory">
            <div className="reviewHistoryHeader">
              <strong>{locale === "zh" ? "审核历史" : "Review history"}</strong>
              {reviewActions.length > 3 && (
                <button type="button" onClick={() => setShowAllReviewHistory((value) => !value)}>
                  {showAllReviewHistory ? (locale === "zh" ? "收起" : "Collapse") : (locale === "zh" ? `查看全部 ${reviewActions.length} 条` : `View all ${reviewActions.length}`)}
                </button>
              )}
            </div>
            <div className="reviewHistoryList">
            {visibleReviewActions.map((action, index) => (
              <div className="reviewHistoryItem" key={`${selected.id}-${action.created_at}-${index}`}>
                <div>
                  <span>{reviewActionLabels[locale][action.action]}</span>
                  <small>{new Date(action.created_at).toLocaleString()}</small>
                </div>
                {safeDisplayText(action.note, locale === "zh" ? "备注内容编码异常，已隐藏。" : "Note hidden because the text encoding is invalid.") && (
                  <p>{safeDisplayText(action.note, locale === "zh" ? "备注内容编码异常，已隐藏。" : "Note hidden because the text encoding is invalid.")}</p>
                )}
              </div>
            ))}
            </div>
          </div>
        )}
      </section>
      <section className="executionPanel">
        <button className="executionToggle" onClick={() => setShowExecutionDetails((value) => !value)}>
          <ListChecks size={16} />
          <span>{locale === "zh" ? "执行流程" : "Execution flow"}</span>
          <small>{showExecutionDetails ? (locale === "zh" ? "收起详情" : "Hide details") : (locale === "zh" ? "查看 Agent 轨迹和知识库依据" : "View trace and grounding")}</small>
        </button>
        {showExecutionDetails && (
          <div className="gridTwo">
            <section><h3>{t.agentTrace}</h3><div className="timeline">{selected.steps.map((step) => <WorkflowStepRow key={`${selected.id}-${step.name}`} step={step} locale={locale} />)}</div></section>
            <section>
              <h3>{t.knowledgeGrounding}</h3>
              <div className="sourceList">
                {selected.knowledge_hits.map((hit) => <KnowledgeHitCard key={hit.source} hit={hit} locale={locale} />)}
                {selected.knowledge_hits.length === 0 && <div className="empty"><Search size={18} /><span>{t.noKnowledge}</span></div>}
              </div>
            </section>
          </div>
        )}
      </section>
    </article>
  );
}

const reviewActionLabels: Record<Locale, Record<ReviewActionRecord["action"], string>> = {
  en: {
    approve: "Approved",
    revise: "Revision requested",
    escalate: "Escalated",
    undo_escalate: "Escalation undone",
  },
  zh: {
    approve: "审核通过",
    revise: "要求修改",
    escalate: "升级处理",
    undo_escalate: "取消升级",
  },
};

function formatEscalationStatus(status: EscalationTicket["status"], locale: Locale) {
  const labels: Record<Locale, Record<EscalationTicket["status"], string>> = {
    zh: {
      open: "待主管接单",
      assigned: "处理中",
      resolved: "已处理完成",
      returned: "已退回审核",
    },
    en: {
      open: "Open",
      assigned: "Assigned",
      resolved: "Resolved",
      returned: "Returned",
    },
  };
  return labels[locale][status];
}

const subjectZhMap: Record<string, string> = {
  "Cannot log in after password reset": "重置密码后无法登录",
  "Need help checking invoice policy": "需要协助确认发票政策",
  "Need manual audit record test": "人工审核记录测试",
  "Invoice question with attachment": "带附件的发票问题",
  "Refund request for duplicate subscription charge": "重复订阅扣费退款请求",
  "Very unhappy with support response time": "对客服响应时间非常不满意",
  "[GitHub] A third-party OAuth application has been added to your account": "[GitHub] 第三方 OAuth 应用已添加到账户",
};

function formatEmailSubject(subject: string, locale: Locale) {
  if (isCorruptedText(subject)) return locale === "zh" ? "平台通知（内容编码异常）" : "Platform notification (encoding issue)";
  if (locale === "en") return subject;
  return subjectZhMap[subject] || subject;
}

function formatEmailBody(body: string, locale: Locale) {
  if (isCorruptedText(body)) {
    return locale === "zh" ? "邮件正文编码异常，已隐藏损坏内容。" : "Email body encoding is invalid, so the corrupted content is hidden.";
  }
  return stripCssPreamble(body);
}

function stripCssPreamble(body: string) {
  const text = body.trim();
  const head = text.slice(0, 3000);
  const cssSignals = [
    /\{[^}]{0,120}box-sizing\s*:/i,
    /@media\s*\(/i,
    /!important/i,
    /\bmso-/i,
    /font-size\s*:/i,
    /line-height\s*:/i,
    /#MessageViewBody/i,
    /\.desktop_hide/i,
  ].filter((pattern) => pattern.test(head)).length;
  if (cssSignals < 3) return body;

  const primaryAnchors = [
    /\bHi\s+[^,\n]{0,40},/i,
    /\bHello\s+[^,\n]{0,40},/i,
    /\bDear\s+[^,\n]{0,40},/i,
    /你好[，,]/,
  ];
  const secondaryAnchors = [
    /\bSign In\b/i,
    /\bView in browser\b/i,
  ];
  for (const anchors of [primaryAnchors, secondaryAnchors]) {
    const indexes = anchors.map((pattern) => {
      const match = text.match(pattern);
      return match?.index ?? -1;
    }).filter((index) => index > 40);
    if (indexes.length > 0) return text.slice(Math.min(...indexes)).trim();
  }
  return body;
}

function formatRiskFlag(flag: string, locale: Locale) {
  if (locale === "en") return flag;
  const normalized = flag.toLowerCase();
  if (normalized.includes("verification test") && normalized.includes("no user impact")) return "验证测试，无用户影响";
  const knownFlags: Array<[string, string]> = [
    ["verification test", "验证测试"],
    ["no user impact", "无用户影响"],
    ["unauthorized oauth access possible", "可能存在未授权 OAuth 访问"],
    ["third-party application with sensitive scopes", "第三方应用包含敏感权限范围"],
    ["legal", "涉及法务风险"],
    ["cancel", "客户存在取消合同风险"],
    ["blocked", "业务流程被阻塞"],
    ["third email", "客户多次联系"],
    ["duplicate", "疑似重复扣费"],
    ["refund", "涉及退款请求"],
    ["sensitive", "涉及敏感权限或敏感信息"],
  ];
  const translated = knownFlags.filter(([keyword]) => normalized.includes(keyword)).map(([, label]) => label);
  return translated.length > 0 ? Array.from(new Set(translated)).join("，") : flag;
}

const attachmentStatusLabels: Record<string, string> = {
  parsed: "已解析",
  failed: "解析失败",
  metadata_only: "仅记录文件信息",
};

function normalizeAttachmentStatus(status?: string) {
  if (status === "parsed" || status === "failed") return status;
  return "metadata_only";
}

function formatBytes(value: number) {
  if (!value) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function WorkflowStepRow({ step, locale }: { step: WorkflowStep; locale: Locale }) {
  const display = formatWorkflowStep(step, locale);
  return (
    <div className={`step ${step.status}`}>
      <div className="stepIcon">{step.status === "complete" ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}</div>
      <div><strong>{display.name}</strong><span>{display.summary}</span><p>{display.detail}</p></div>
    </div>
  );
}

function formatWorkflowStep(step: WorkflowStep, locale: Locale) {
  if (locale === "en") {
    return {
      name: safeDisplayText(step.name, step.name),
      summary: safeDisplayText(step.summary, step.summary),
      detail: safeDisplayText(step.detail, step.detail),
    };
  }

  const zhSteps: Record<string, { name: string; summary: string; detail: string }> = {
    "Preprocess email": {
      name: "预处理邮件",
      summary: "识别语言、附件和高风险线索",
      detail: "在调用 LLM 前先进行低成本预处理，提取语言、附件、重复联系、退款、法律和业务阻塞等信号。",
    },
    "Semantic analysis": {
      name: "语义分析",
      summary: "完成邮件分类、置信度和风险等级判断",
      detail: "结合 LLM 语义分析和规则兜底，判断邮件类别、处理优先级、风险标记和是否需要人工介入。",
    },
    "Retrieve knowledge": {
      name: "检索知识库",
      summary: "召回与邮件意图相关的知识库依据",
      detail: "根据邮件正文和附件内容检索知识库片段，并按语义相关度、关键词和分类匹配进行排序。",
    },
    "Draft reply": {
      name: "生成回复草稿",
      summary: "生成可审核的客服回复草稿",
      detail: "根据邮件分类、风险等级和知识库依据生成回复草稿，等待人工审核后再发送。",
    },
    "Regenerate draft reply": {
      name: "重新生成回复草稿",
      summary: "根据当前邮件上下文生成另一版回复",
      detail: "保留原有分类、风险判断和知识库依据，重新组织回复措辞，供审核人员选择或继续修改。",
    },
    "Review decision": {
      name: "审核门控",
      summary: step.status === "blocked" ? "需要人工审核" : "已生成可发送草稿",
      detail: "低风险、高置信且有知识库依据的回复会进入可发送状态，由人工一键确认发送；中高风险、低置信或依据不足的回复进入人工审核队列。",
    },
    "Relevance gate": {
      name: "相关性过滤",
      summary: step.status === "blocked" ? "非客服邮件，已拦截" : "客服请求，继续处理",
      detail: "平台通知、安全提醒、营销邮件等非客服请求不会进入 RAG 检索和回复生成链路。",
    },
    "Semantic relevance gate": {
      name: "语义相关性复核",
      summary: step.status === "blocked" ? "语义判断为非客服邮件" : "客服请求，继续处理",
      detail: "在语义分析后再次检查是否属于平台通知、安全提醒、营销邮件或无明确客户诉求的邮件，避免无关邮件进入 RAG 和回复生成链路。",
    },
  };

  const translated = zhSteps[step.name];
  if (translated) return translated;

  return {
    name: translateWorkflowText(step.name, "未知步骤"),
    summary: translateWorkflowText(step.summary, "暂无步骤摘要"),
    detail: translateWorkflowText(step.detail, "暂无步骤说明"),
  };
}

function translateWorkflowText(value?: string, fallback = "") {
  const text = safeDisplayText(value, fallback);
  const dictionary: Record<string, string> = {
    "Customer support intent retained": "已保留客服请求",
    "Human review required": "需要人工审核",
    "Ready for one-click sending": "已进入一键确认发送",
    "Found 0 relevant source(s)": "未召回相关知识库依据",
    "Generated customer-ready draft": "已生成可审核回复草稿",
    "Generated reply draft variant 1": "已生成第 1 版替代回复",
    "Generated reply draft variant 2": "已生成第 2 版替代回复",
    "Generated reply draft variant 3": "已生成第 3 版替代回复",
    "Semantic analysis did not find a strong non-support signal, so the email can continue to RAG retrieval.": "语义分析未发现明确的非客服信号，邮件可以继续进入 RAG 检索。",
    "Semantic analysis explicitly identified this email as non-support, marketing, notification, or without a customer issue.": "语义分析判断该邮件属于非客服、营销、通知或没有明确客户问题的邮件。",
    "Semantic analysis identified this as a platform notification, marketing email, security alert, or message without a customer support request.": "语义分析判断该邮件属于平台通知、营销邮件、安全提醒或无客服诉求的邮件。",
    "Draft generation used the customer email, classification result, and retrieved knowledge snippets as LLM context.": "回复生成使用了客户邮件、分类结果和已召回的可靠知识库依据作为上下文。",
    "The operator requested another LLM-generated draft while keeping the same category, risk level, and knowledge grounding.": "操作人员请求重新生成回复，系统保留原有分类、风险等级和知识库依据。",
  };
  return dictionary[text] ?? text;
}

function safeEditableText(value?: string) {
  if (isCorruptedText(value)) return "";
  return stripInternalReferenceLines(value || "");
}

function safeDisplayText(value?: string, fallback = "") {
  if (!value) return "";
  return isCorruptedText(value) ? fallback : value;
}

function isCorruptedText(value?: string) {
  const text = (value || "").trim();
  if (!text) return false;
  const questionCount = (text.match(/\?/g) || []).length;
  const repeatedQuestionRuns = (text.match(/\?{4,}/g) || []).length;
  return (
    text === "�"
    || /^[?\s]+$/.test(text)
    || repeatedQuestionRuns > 0
    || questionCount / text.length > 0.25
    || text.includes("�")
    || isMojibakeText(text)
  );
}

function isMojibakeText(text: string) {
  const markers = ["鏈", "鎬", "浣", "鏂", "鐪", "嬶", "細", "鏉", "閫", "氱", "煡"];
  const hits = markers.filter((marker) => text.includes(marker)).length;
  return hits >= 3;
}

function stripInternalReferenceLines(value: string) {
  return value
    .split(/\r?\n/)
    .filter((line) => {
      const trimmed = line.trim();
      return !trimmed.startsWith("Reference used:") && !trimmed.startsWith("参考依据：");
    })
    .join("\n")
    .trim();
}

function EmptyModule({ title, text }: { title: string; text: string }) {
  return <section className="emptyModule"><CheckCircle2 size={22} /><strong>{title}</strong><p>{text}</p></section>;
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return <div className="metric"><strong>{value}</strong><span>{label}</span></div>;
}

function StatusIcon({ status }: { status: EmailStatus }) {
  if (status === "human_review" || status === "escalated") return <AlertTriangle size={17} />;
  if (status === "ready_to_send" || status === "processed" || status === "irrelevant") return <CheckCircle2 size={17} />;
  return <Clock3 size={17} />;
}

function formatCategory(category: string | null, locale: Locale) {
  if (!category) return copy[locale].uncategorized;
  return categoryLabels[locale][category] ?? category.replace(/_/g, " ");
}

function getPageMeta(activeView: ActiveView, t: Record<string, string>) {
  const meta = {
    inbox: { title: t.workbench, subtitle: t.subtitle },
    review: { title: t.reviewTitle, subtitle: t.reviewSubtitle },
    knowledge: { title: t.knowledgeTitle, subtitle: t.knowledgeSubtitle },
    runs: { title: t.runsTitle, subtitle: t.runsSubtitle },
    settings: { title: t.settingsTitle, subtitle: t.settingsSubtitle },
  };
  return meta[activeView];
}

const roleLabels: Record<UserRole, string> = {
  admin: "系统管理员",
  manager: "客服主管",
  agent: "客服人员",
};

function getVisibleViews(role?: UserRole): ActiveView[] {
  if (role === "admin") return ["inbox", "review", "knowledge", "runs", "settings"];
  if (role === "manager") return ["inbox", "review", "knowledge", "runs"];
  if (role === "agent") return ["inbox", "review"];
  return ["inbox"];
}

function canAccessView(role: UserRole, view: ActiveView) {
  return getVisibleViews(role).includes(view);
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
