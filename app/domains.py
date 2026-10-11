"""领域标签：内置 + 用户自建。

领域决定一个主题用哪些信息源：每个来源带若干领域标签，主题判定出领域后，选用标签相交的来源。
用户可以新建标签（名称 + 判断用的关键词 + 一句说明），再给信息源打上这个标签；
大模型规划和关键词规则判断领域时，会把自建标签一并考虑。
"""
from __future__ import annotations

import re

from . import db

BUILTIN = [
    {"id": "tech", "name": "科技/AI", "desc": "科技 / AI / 软件", "hints": [
        "大模型", "人工智能", "AI", "模型", "算法", "芯片", "编程", "软件", "开源", "互联网", "手机", "机器人",
        "云", "数据库", "LLM", "GPU", "agent", "software", "chip", "robot", "代码", "框架"]},
    {"id": "finance", "name": "金融", "desc": "金融 / 经济 / 公司", "hints": [
        "股票", "股市", "基金", "证券", "金融", "经济", "财报", "上市", "利率", "汇率", "通胀", "央行", "债券", "期货",
        "投资", "量化", "A股", "港股", "美股", "估值", "营收", "stock", "market", "finance", "econom", "inflation",
        "bond", "fund", "trading", "earnings"]},
    {"id": "science", "name": "学术/医学", "desc": "学术 / 医学 / 科研", "hints": [
        "医学", "药", "疾病", "临床", "基因", "蛋白", "细胞", "疫苗", "癌", "物理", "化学", "生物", "气候", "材料", "论文",
        "研究", "clinical", "disease", "drug", "gene", "protein", "climate", "physics", "biology", "medical"]},
    {"id": "general", "name": "综合/政策", "desc": "综合新闻 / 政策 / 社会", "hints": [
        "政策", "法规", "社会", "国际", "外交", "城市", "就业", "文化", "体育", "policy"]},
    {"id": "community", "name": "社区/小众", "desc": "社区 / 小众话题 / 亚文化 / 个人经验 / 具体博主或团队", "hints": [
        "工作室", "博主", "up主", "UP主", "网红", "主播", "粉丝", "圈", "二次元", "cos", "男娘", "伪娘", "女装", "饭圈",
        "八卦", "吐槽", "攻略", "避坑", "探店", "成员", "小红书", "抖音", "b站", "B站", "贴吧", "知乎", "公众号"]},
    # ---- 新增的常用领域
    {"id": "auto", "name": "汽车/新能源", "desc": "汽车 / 新能源 / 电池 / 出行", "hints": [
        "汽车", "电动车", "新能源", "电池", "固态电池", "锂电", "充电", "自动驾驶", "智驾", "车企", "车型", "续航",
        "EV", "battery", "electric vehicle", "储能", "光伏"]},
    {"id": "health", "name": "健康/医疗", "desc": "健康 / 医疗 / 药物 / 健身饮食", "hints": [
        "健康", "医疗", "医院", "医保", "减重", "减肥", "GLP-1", "司美格鲁肽", "副作用", "健身", "饮食", "营养", "睡眠",
        "心理", "体检", "health", "obesity", "weight loss"]},
    {"id": "game", "name": "游戏/动漫", "desc": "游戏 / 动漫 / 电竞 / 二次元", "hints": [
        "游戏", "手游", "主机", "Steam", "Switch", "PS5", "电竞", "动漫", "番剧", "漫画", "原神", "米哈游", "game", "anime"]},
    {"id": "consumer", "name": "消费/生活", "desc": "消费 / 零售 / 品牌 / 生活方式", "hints": [
        "消费", "零售", "品牌", "电商", "美妆", "餐饮", "咖啡", "茶饮", "旅游", "家电", "购物", "价格战", "双十一", "直播带货"]},
]
BUILTIN_IDS = {d["id"] for d in BUILTIN}

SCHEMA = """CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, descr TEXT, hints TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL)"""


def _ensure():
    db.execute(SCHEMA)


def custom() -> list[dict]:
    _ensure()
    out = []
    for r in db.query("SELECT * FROM domains ORDER BY created_at"):
        import json
        out.append({"id": r["id"], "name": r["name"], "desc": r["descr"] or r["name"],
                    "hints": json.loads(r["hints"] or "[]"), "custom": True})
    return out


def all_domains() -> list[dict]:
    return [dict(d, custom=False) for d in BUILTIN] + custom()


def names() -> dict:
    return {d["id"]: d["name"] for d in all_domains()}


def descs() -> dict:
    return {d["id"]: d["desc"] for d in all_domains()}


def ids() -> set:
    return {d["id"] for d in all_domains()}


def add(name: str, hints: list[str] | str | None = None, desc: str | None = None) -> dict:
    import json
    name = (name or "").strip()
    if not 2 <= len(name) <= 12:
        raise ValueError("标签名称 2–12 个字，例如：宠物、留学、户外运动")
    if name in {d["name"] for d in all_domains()} or name in {d["desc"] for d in all_domains()}:
        raise ValueError("已经有同名标签")
    hs = hints if isinstance(hints, list) else re.split(r"[,，、;；\s]+", hints or "")
    hs = [h.strip() for h in hs if len(h.strip()) >= 2]
    hs = list(dict.fromkeys([name] + [p for p in re.split(r"[/／]", name) if len(p) >= 2] + hs))[:30]
    did = db.new_id("c_")
    _ensure()
    db.insert("domains", dict(id=did, name=name, descr=(desc or "").strip()[:60] or name, hints=json.dumps(hs, ensure_ascii=False),
                              created_at=db.now()))
    return {"id": did, "name": name, "desc": (desc or "").strip() or name, "hints": hs, "custom": True}


def remove(did: str) -> dict:
    """删除自建标签，并从所有信息源上摘掉它。内置标签不能删。"""
    if did in BUILTIN_IDS:
        raise ValueError("内置标签不能删除")
    _ensure()
    if not db.query_one("SELECT id FROM domains WHERE id=?", (did,)):
        raise KeyError(did)
    db.execute("DELETE FROM domains WHERE id=?", (did,))
    touched = 0
    for s in db.query("SELECT id, config FROM sources"):
        cfg = s["config"] or {}
        if did in (cfg.get("domains") or []):
            cfg["domains"] = [x for x in cfg["domains"] if x != did]
            db.update("sources", "id", s["id"], {"config": cfg})
            touched += 1
    return {"removed": did, "sources_updated": touched}


def guess(text: str) -> list[str]:
    low = (text or "").lower()
    hits = [d["id"] for d in all_domains() if any(h.lower() in low for h in d["hints"])]
    return hits or ["general", "tech"]


def prompt_list() -> str:
    """给大模型的领域清单：id=说明（自建标签附关键词）。"""
    parts = []
    for d in all_domains():
        extra = f"（如：{'、'.join(d['hints'][:6])}）" if d.get("custom") else ""
        parts.append(f"{d['id']}={d['desc']}{extra}")
    return "，".join(parts)
