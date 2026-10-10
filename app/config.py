"""全局配置。所有路径、预算默认值都从这里读，环境变量可覆盖。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TOPICDESK_DATA", ROOT / "data"))
DB_PATH = Path(os.environ.get("TOPICDESK_DB", DATA_DIR / "topicdesk.db"))
IMAGE_DIR = DATA_DIR / "images"
CACHE_DIR = DATA_DIR / "http_cache"
SKILL_DIR = ROOT / "skills"

# 任务默认预算：步数、工具调用次数、墙钟秒数、成本（美元，LLM 调用累计）
DEFAULT_BUDGET = {
    "max_steps": 60,
    "max_tool_calls": 120,
    "max_seconds": 900,
    "max_cost": 1.0,
}

# LLM：OpenAI 兼容接口（OpenAI / DeepSeek / 通义 / Moonshot 等都可）。
# 不配置时使用离线规则后备，保证流程可跑通。
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

for d in (DATA_DIR, IMAGE_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
