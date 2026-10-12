from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, file_secret_settings

    p115_cookies: str | None = Field(default=None, alias="P115_COOKIES")
    p115_cookies_path: str | None = Field(default=None, alias="P115_COOKIES_PATH")
    p115_check_for_relogin: bool = Field(default=True, alias="P115_CHECK_FOR_RELOGIN")
    p115_allow_qrcode_login: bool = Field(default=False, alias="P115_ALLOW_QRCODE_LOGIN")
    p115_console_qrcode: bool = Field(default=False, alias="P115_CONSOLE_QRCODE")

    fastmcp_transport: str = Field(default="stdio", alias="FASTMCP_TRANSPORT")
    fastmcp_host: str = Field(default="127.0.0.1", alias="FASTMCP_HOST")
    fastmcp_port: int = Field(default=8000, alias="FASTMCP_PORT")
    fastmcp_path: str = Field(default="/mcp", alias="FASTMCP_PATH")
    fastmcp_log_level: str = Field(default="info", alias="FASTMCP_LOG_LEVEL")
    p115_debug_logging: bool = Field(default=True, alias="P115_DEBUG_LOGGING")
    p115_debug_log_file: str | None = Field(default="./logs/115-mcp-debug.log", alias="P115_DEBUG_LOG_FILE")

    # ---- 批量上传（batch upload）----
    # 这些参数只影响「批量上传」能力；单文件 upload_local_file 不受影响。
    upload_root: str | None = Field(default=None, alias="UPLOAD_ROOT")
    upload_parent_cid: str = Field(default="0", alias="UPLOAD_PARENT_CID")
    upload_conc: int = Field(default=4, alias="UP_CONC")
    upload_workdir: str | None = Field(default=None, alias="UPLOAD_WORKDIR")
    upload_chunk_mb: int = Field(default=8, alias="UP_CHUNK_MB")
    upload_connect_timeout: float = Field(default=30.0, alias="UP_CONNECT_TIMEOUT")
    upload_read_timeout: float = Field(default=180.0, alias="UP_READ_TIMEOUT")
    # 看门狗：progress 与网卡「双双」零进展多久判卡死。
    # ⚠ 若源在本机而数据不经过本机网卡（NAS 直连场景），网卡指标恒 0，
    #   应调大此值（如 900）以免误判。
    upload_watchdog_sec: float = Field(default=300.0, alias="UP_WATCHDOG_SEC")
    upload_max_retry: int = Field(default=5, alias="UP_MAX_RETRY")

    @property
    def upload_root_path(self) -> Path | None:
        if not self.upload_root:
            return None
        return Path(self.upload_root).expanduser()

    @property
    def upload_workdir_path(self) -> Path:
        if self.upload_workdir:
            return Path(self.upload_workdir).expanduser()
        return Path.cwd()

    @property
    def cookies_path(self) -> Path | None:
        if not self.p115_cookies_path:
            return None
        return Path(self.p115_cookies_path).expanduser()

    @property
    def has_auth_configuration(self) -> bool:
        return bool(self.p115_cookies or self.p115_cookies_path or self.p115_allow_qrcode_login)

    @property
    def cookies_source(self) -> str:
        if self.p115_cookies:
            return "env"
        if self.p115_cookies_path:
            return "file"
        if self.p115_allow_qrcode_login:
            return "qrcode"
        return "missing"

    @property
    def debug_log_file_path(self) -> Path | None:
        if not self.p115_debug_log_file:
            return None
        return Path(self.p115_debug_log_file).expanduser()
