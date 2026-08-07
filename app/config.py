from functools import lru_cache
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", env_prefix="KG_", extra="ignore")
    database_url: str = "sqlite:///./runtime/knowledge_governance.db"
    demo_mode: bool = False
    default_actor: str = "knowledge-governance-admin"
    public_base_url: str = "http://localhost:39021"
    dingtalk_app_key: str = Field(default="", validation_alias="DINGTALK_APP_KEY")
    dingtalk_app_secret: str = Field(default="", validation_alias="DINGTALK_APP_SECRET")
    dingtalk_sync_operator_id: str = Field(default="", validation_alias="DINGTALK_SYNC_OPERATOR_ID")
    dingtalk_doc_content_url_template: str = Field(default="", validation_alias="DINGTALK_DOC_CONTENT_URL_TEMPLATE")
    bi_center_base_url: str = Field(default="", validation_alias="BI_CENTER_BASE_URL")
    bi_center_internal_token: str = Field(default="", validation_alias="BI_CENTER_INTERNAL_TOKEN")
    model_api_key: str = ""
    model_allow_content_transfer: bool = False
    # Push stays off until the robot permission is granted and a test send passes.
    notify_enabled: bool = False
    robot_code: str = ""  # defaults to the app key at call time when empty
    # Stream-mode event consumer (file-change events land in stream_events).
    # 2026-08-07: storage events verifiably do NOT fire for knowledge-base
    # uploads, so this stays a diagnostic channel only — detection lives in the
    # targeted watcher below.
    stream_enabled: bool = False
    # Targeted workspace watcher: comma-separated workspace ids, exact names or
    # name fragments (resolved against the operator's workspace list). The
    # first complete walk of a workspace only seeds the mirror; later walks
    # enqueue reviews for new/changed files and soft-delete missing ones.
    watch_workspaces: str = ""
    watch_interval_seconds: int = 300
    # A document is soft-deleted after this many consecutive complete walks
    # without seeing it (recycle-bin restores clear the flag again).
    watch_delete_misses: int = 2
    # Machine accounts (comma-separated userIds) excluded from person rankings.
    # bi_center currently classifies the digital employee as an official
    # employee, so the service marks its own operator account explicitly.
    robot_user_ids: str = ""
    # DingTalk login guard for /api/*. Off by default so local dev and tests
    # run open; the server .env turns it on with a real secret.
    auth_enabled: bool = False
    auth_secret: str = "dev-only-not-secret"
    dingtalk_corp_id: str = Field(default="", validation_alias="DINGTALK_CORP_ID")


@lru_cache
def get_settings() -> Settings:
    return Settings()
