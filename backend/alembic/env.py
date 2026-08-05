import os
import sys
from logging.config import fileConfig
from pathlib import Path


from alembic import context
from sqlalchemy import engine_from_config, pool
from pgvector.sqlalchemy import Vector

# Make the `db` package importable. The src layout is `src/ai-customer-assistant/db/...`
# and "ai-customer-assistant" (hyphenated) can never appear as a dotted segment in
# an import statement — but adding that directory straight to sys.path makes its
# direct children, like `db`, ordinary top-level-importable packages.


# SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "ai-customer-assistant"
# sys.path.insert(0, str(SRC_ROOT))

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ai_customer_assistant"))
from db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Schema is driven by the SQLAlchemy models now — Base.metadata is what
# `alembic revision --autogenerate` diffs the live database against.
target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@{os.environ.get('POSTGRES_HOST', 'localhost')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ['POSTGRES_DB']}"
    )
    
def render_item(type_, obj, autogen_context):
    """Teach autogenerate how to correctly import/render pgvector's Vector type."""
    if type_ == "type" and isinstance(obj, Vector):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")
        return f"Vector({obj.dim!r})"
    return False

def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item,)
        with context.begin_transaction():
            context.run_migrations()


(run_migrations_offline if context.is_offline_mode() else run_migrations_online)()
