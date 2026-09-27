"""从环境变量读取配置。支持项目根目录下的 .env 文件（简单解析，不依赖 python-dotenv）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Settings:
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_stream: bool = False  # 只支持流式的中转服务设为 true
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    embed_dimensions: int | None = None  # 只对支持 dimensions 参数的模型填写
    retriever: str = "memory"  # memory | pgvector
    storage: str = "memory"  # memory | postgres：LangGraph 状态和运行记录存哪
    database_url: str = ""
    max_review_retries: int = 2
    cover_font: str = ""  # 封面用的中文字体文件；留空自动在系统字体里找
    max_format_retries: int = 1  # 模型输出不是合法 JSON / 结构不对时，带着错误重新请求的次数
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "outputs")
    log_file: Path = field(default_factory=lambda: PROJECT_ROOT / "logs" / "runs.jsonl")
    events_file: Path = field(default_factory=lambda: PROJECT_ROOT / "logs" / "events.jsonl")  # STORAGE=memory 时的反馈
    shops_file: Path = field(default_factory=lambda: PROJECT_ROOT / "logs" / "shops.json")  # STORAGE=memory 时的试用码
    require_shop_code: bool = True  # Web 接口是否要求试用码；本地调试可关
    sample_notes: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "sample_notes.jsonl")
    sensitive_words: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "sensitive_words.txt")
    merchant_words: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "ad_words_food.txt")

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(PROJECT_ROOT / ".env")
        s = cls()
        env = os.environ
        s.llm_base_url = env.get("LLM_BASE_URL", s.llm_base_url)
        s.llm_api_key = env.get("LLM_API_KEY", s.llm_api_key)
        s.llm_model = env.get("LLM_MODEL", s.llm_model)
        s.llm_stream = env.get("LLM_STREAM", str(s.llm_stream)).strip().lower() in ("1", "true", "yes")
        s.embed_base_url = env.get("EMBED_BASE_URL", s.embed_base_url)
        s.embed_api_key = env.get("EMBED_API_KEY", s.embed_api_key)
        s.embed_model = env.get("EMBED_MODEL", s.embed_model)
        if env.get("EMBED_DIMENSIONS"):
            s.embed_dimensions = int(env["EMBED_DIMENSIONS"])
        s.retriever = env.get("RETRIEVER", s.retriever)
        s.storage = env.get("STORAGE", s.storage)
        s.database_url = env.get("DATABASE_URL", s.database_url)
        s.max_review_retries = int(env.get("MAX_REVIEW_RETRIES", s.max_review_retries))
        s.max_format_retries = int(env.get("MAX_FORMAT_RETRIES", s.max_format_retries))
        s.cover_font = env.get("COVER_FONT", s.cover_font)
        if "OUTPUT_DIR" in env:
            s.output_dir = PROJECT_ROOT / env["OUTPUT_DIR"]
        if "LOG_FILE" in env:
            s.log_file = PROJECT_ROOT / env["LOG_FILE"]
        if "EVENTS_FILE" in env:
            s.events_file = PROJECT_ROOT / env["EVENTS_FILE"]
        if "SHOPS_FILE" in env:
            s.shops_file = PROJECT_ROOT / env["SHOPS_FILE"]
        if "REQUIRE_SHOP_CODE" in env:
            s.require_shop_code = env["REQUIRE_SHOP_CODE"].strip().lower() in ("1", "true", "yes")
        return s
