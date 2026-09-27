"""应用配置：从项目根 .env 读取环境变量。

设计要点：
- 所有配置项集中在此，禁止在业务代码中直接读 os.environ
- 项目根的 .env 是唯一凭据来源（见 docs/architecture.md §15.1）
- 提供派生属性（如 db_url）避免各处重复拼接
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：backend/app/core/config.py -> 上溯 3 层
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """全局配置。字段名与 .env 中的变量名大小写不敏感对应。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # 允许 .env 存在未被使用的变量
    )

    # ---------------- 应用 ----------------
    app_env: str = Field(default="dev", description="运行环境：dev / prod")
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000)
    log_level: str = Field(default="INFO")

    # ---------------- MySQL ----------------
    mysql_host: str = Field(default="127.0.0.1")
    mysql_port: int = Field(default=13306)
    mysql_database: str = Field(default="contract_review")
    mysql_user: str = Field(default="contract_review")
    mysql_password: str = Field(default="")

    # ---------------- Redis ----------------
    redis_host: str = Field(default="127.0.0.1")
    redis_port: int = Field(default=16379)
    redis_db: int = Field(default=0)
    redis_password: str = Field(default="")

    # ---------------- MinIO ----------------
    minio_endpoint: str = Field(default="127.0.0.1:19000")
    minio_access_key: str = Field(default="")
    minio_secret_key: str = Field(default="")
    minio_secure: bool = Field(default=False)
    minio_bucket_contracts: str = Field(default="contracts")
    minio_bucket_reports: str = Field(default="reports")

    # ---------------- LLM ----------------
    llm_provider: str = Field(default="mock", description="mock / openai_compat")
    llm_base_url: str = Field(default="")
    llm_api_key: str = Field(default="")
    llm_model: str = Field(default="")
    llm_timeout: int = Field(default=120)
    llm_max_retries: int = Field(default=3)

    # ---------------- 宿主能力（进程内库调用，无服务地址）----------------
    docx_converter: str = Field(default="wps_com", description="wps_com / libreoffice / passthrough")
    wps_com_timeout: int = Field(default=60)

    # ---------------- 解析参数 ----------------
    ocr_dpi: int = Field(default=200)
    ocr_engine: str = Field(default="rapidocr")
    parse_base_timeout: int = Field(default=60)
    parse_per_page_timeout: int = Field(default=30)

    # ---------------- mock 审批服务 ----------------
    mock_approval_host: str = Field(default="127.0.0.1")
    mock_approval_port: int = Field(default=8010)

    # ==================== 派生属性 ====================

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """SQLAlchemy 连接串。

        注意：charset 必须显式指定 utf8mb4，否则中文与 emoji 可能损坏。
        见 docs/data-model.md §10.3。
        """
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            f"?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        """Redis 连接串。无密码时省略密码段。"""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mock_approval_base_url(self) -> str:
        """mock 审批服务的 base url。"""
        return f"http://{self.mock_approval_host}:{self.mock_approval_port}"

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def use_real_llm(self) -> bool:
        """是否使用真实 LLM。三者齐备才算配置完整，否则回落到 mock。"""
        return (
            self.llm_provider == "openai_compat"
            and bool(self.llm_base_url)
            and bool(self.llm_api_key)
            and bool(self.llm_model)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。用 lru_cache 避免重复解析 .env。"""
    return Settings()


settings = get_settings()
