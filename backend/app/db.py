"""数据库连接与运行期 schema 兼容层。

项目默认使用 ``DATABASE_URL`` 指向 PostgreSQL；如果没有配置，会退回 SQLite，
方便本地快速演示。知识库向量检索需要 PostgreSQL + pgvector，SQLite 只能作为
基础功能降级环境。

``ensure_runtime_columns`` 用于给旧数据库补新增字段，避免开发过程中频繁手动迁移。
"""

import os
import warnings
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./customer_email_agent.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类。"""

    pass


def init_db() -> None:
    """初始化所有 ORM 表，并补齐运行期新增字段。"""
    from app import db_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_runtime_columns()


def ensure_runtime_columns() -> None:
    """为旧表补齐新增字段。

    这个项目迭代过程中新增过附件、成本指标、解析报告、pgvector 字段等。
    这里用轻量 ALTER TABLE 保证旧环境启动时也能继续运行。
    """
    inspector = inspect(engine)
    if "emails" in inspector.get_table_names():
        email_columns = get_column_names(inspector, "emails")
        email_statements: list[str] = []
        if "attachments" not in email_columns:
            if engine.dialect.name == "postgresql":
                email_statements.append("ALTER TABLE emails ADD COLUMN attachments JSONB DEFAULT '[]'::jsonb NOT NULL")
            else:
                email_statements.append("ALTER TABLE emails ADD COLUMN attachments JSON DEFAULT '[]' NOT NULL")
        if "agent_metrics" not in email_columns:
            if engine.dialect.name == "postgresql":
                email_statements.append("ALTER TABLE emails ADD COLUMN agent_metrics JSONB DEFAULT '{}'::jsonb NOT NULL")
            else:
                email_statements.append("ALTER TABLE emails ADD COLUMN agent_metrics JSON DEFAULT '{}' NOT NULL")
        if "processing_status" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_status VARCHAR(32) DEFAULT 'completed' NOT NULL")
        if "processing_stage" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_stage VARCHAR(64) DEFAULT 'completed' NOT NULL")
        if "processing_progress" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_progress INTEGER DEFAULT 100 NOT NULL")
        if "processing_message" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_message VARCHAR(500) DEFAULT '' NOT NULL")
        if "processing_started_at" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_started_at TIMESTAMP")
        if "processing_finished_at" not in email_columns:
            email_statements.append("ALTER TABLE emails ADD COLUMN processing_finished_at TIMESTAMP")
        if email_statements:
            with engine.begin() as connection:
                for statement in email_statements:
                    connection.execute(text(statement))

    if "knowledge_documents" not in inspector.get_table_names():
        return

    existing_columns = get_column_names(inspector, "knowledge_documents")
    statements: list[str] = []
    if "status" not in existing_columns:
        statements.append("ALTER TABLE knowledge_documents ADD COLUMN status VARCHAR(32) DEFAULT 'indexed' NOT NULL")
    if "status_message" not in existing_columns:
        statements.append("ALTER TABLE knowledge_documents ADD COLUMN status_message TEXT DEFAULT '' NOT NULL")
    if "parse_report" not in existing_columns:
        if engine.dialect.name == "postgresql":
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN parse_report JSONB DEFAULT '{}'::jsonb NOT NULL")
        else:
            statements.append("ALTER TABLE knowledge_documents ADD COLUMN parse_report JSON DEFAULT '{}' NOT NULL")

    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    if "knowledge_chunks" not in inspector.get_table_names():
        return

    chunk_columns = get_column_names(inspector, "knowledge_chunks")
    chunk_statements: list[str] = []
    if "page_number" not in chunk_columns:
        chunk_statements.append("ALTER TABLE knowledge_chunks ADD COLUMN page_number INTEGER")
    if "section_title" not in chunk_columns:
        chunk_statements.append("ALTER TABLE knowledge_chunks ADD COLUMN section_title VARCHAR(255) DEFAULT '' NOT NULL")

    if not chunk_statements:
        pass
    else:
        with engine.begin() as connection:
            for statement in chunk_statements:
                connection.execute(text(statement))

    ensure_pgvector_columns(chunk_columns)

    if "knowledge_document_versions" not in inspector.get_table_names():
        return

    version_columns = get_column_names(inspector, "knowledge_document_versions")
    version_statements: list[str] = []
    if "content_snapshot" not in version_columns:
        version_statements.append("ALTER TABLE knowledge_document_versions ADD COLUMN content_snapshot TEXT DEFAULT '' NOT NULL")

    if not version_statements:
        return

    with engine.begin() as connection:
        for statement in version_statements:
            connection.execute(text(statement))


def get_column_names(inspector, table_name: str) -> set[str]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Did not recognize type 'vector'.*", category=SAWarning)
        return {column["name"] for column in inspector.get_columns(table_name)}


def ensure_pgvector_columns(chunk_columns: set[str]) -> None:
    """确保 PostgreSQL 中存在 pgvector 扩展和向量字段。

    ``embedding`` JSON 字段用于兼容和调试；``embedding_vector`` 字段用于数据库侧
    向量近邻召回。
    """
    if engine.dialect.name != "postgresql":
        return

    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            if "embedding_vector" not in chunk_columns:
                connection.execute(text("ALTER TABLE knowledge_chunks ADD COLUMN embedding_vector vector"))
    except Exception:
        # pgvector is an optional acceleration path. The application can still
        # use JSON embeddings and Python cosine similarity when the extension is
        # unavailable in a local PostgreSQL installation.
        return

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_vector_hnsw "
                    "ON knowledge_chunks USING hnsw (embedding_vector vector_cosine_ops)"
                )
            )
    except Exception:
        # Some PostgreSQL/pgvector combinations cannot build an ANN index for a
        # dimensionless vector column. Exact pgvector ORDER BY still works.
        return
