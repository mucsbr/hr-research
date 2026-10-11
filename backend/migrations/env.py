from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.db import Base
from app import models  # noqa: F401
from app import sentiment_models  # noqa: F401
from app import notification_models  # noqa: F401
from app import news_models  # noqa: F401
from app import recruitment_management_models  # noqa: F401
from app import maintenance_models  # noqa: F401
from app import auto_maintenance_models  # noqa: F401
from app import checkpoint_models  # noqa: F401
from app import adapter_code_models  # noqa: F401
from app import adapter_runtime_models  # noqa: F401
from app import retention_models  # noqa: F401
from app import probe_models  # noqa: F401
from app import model_settings  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
url = get_settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
