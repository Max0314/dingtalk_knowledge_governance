from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", env_prefix="KG_", extra="ignore")
    database_url: str = "sqlite:///./runtime/knowledge_governance.db"
    redis_url: str = "redis://localhost:6379/0"
    demo_mode: bool = False
    default_actor: str = "knowledge-governance-admin"
    public_base_url: str = "http://localhost:39057"
    dingtalk_app_key: str = Field(default="", validation_alias="DINGTALK_APP_KEY")
    dingtalk_app_secret: str = Field(default="", validation_alias="DINGTALK_APP_SECRET")
    dingtalk_sync_operator_id: str = Field(default="", validation_alias="DINGTALK_SYNC_OPERATOR_ID")
    dingtalk_doc_content_url_template: str = Field(default="", validation_alias="DINGTALK_DOC_CONTENT_URL_TEMPLATE")
    bi_center_base_url: str = Field(default="", validation_alias="BI_CENTER_BASE_URL")
    bi_center_internal_token: str = Field(default="", validation_alias="BI_CENTER_INTERNAL_TOKEN")
    model_api_key: str = ""
    model_allow_content_transfer: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
