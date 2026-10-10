"""固定样例回放：不联网，用 tests/fixtures.py 里的模拟网站复现完整流程。

用途：现场网络不稳时演示；或者演示“第二天资料被更正/出现新进展”的长期记忆效果。
启动：python run.py --replay（使用独立数据库 data/replay.db，不影响真实数据）
在页面“设置”里可以切换“第 2 天”：IT之家更正数字、出现一篇新进展。
"""
from __future__ import annotations

from . import config

STATE = {"enabled": False, "web": None}


def enable():
    from tests import fixtures
    from .collect import browser, fetch
    config.DB_PATH = config.DATA_DIR / "replay.db"
    config.CACHE_DIR = config.DATA_DIR / "replay_cache"
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    web = fixtures.FakeWeb()
    fetch.set_transport(web)
    fetch.fetcher.min_interval = 0
    fetch.fetcher.host_interval.clear()
    browser.render, browser.fetch_raw = fixtures.fake_render, fixtures.fake_fetch_raw
    STATE.update(enabled=True, web=web)


def set_day(day: int):
    """第 1 天：原始资料；第 2 天：IT之家更正 128K→64K，新增 256K 新进展，HN 不可用。"""
    from .collect import fetch
    web = STATE["web"]
    web.state.update(corrected=day >= 2, extra=day >= 2, hn_down=day >= 2)
    fetch.fetcher._robots.clear()
    for p in config.CACHE_DIR.glob("*"):
        p.unlink()
    STATE["day"] = day


def info():
    return {"enabled": STATE["enabled"], "day": STATE.get("day", 1)}
