from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str = Field(..., alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")

    wechat_bot_token: str = Field("", alias="WECHAT_BOT_TOKEN")
    wechat_baseurl: str = Field("https://ilinkai.weixin.qq.com", alias="WECHAT_BASEURL")

    db_url: str = Field("sqlite:///./planagent.db", alias="PLANAGENT_DB_URL")
    host: str = Field("0.0.0.0", alias="PLANAGENT_HOST")
    port: int = Field(8000, alias="PLANAGENT_PORT")


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
