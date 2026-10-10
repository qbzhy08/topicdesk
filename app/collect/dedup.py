"""去重与归并：URL 规范化、SimHash 近似重复、标题相似度事件聚类。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = re.compile(r"^(utm_\w+|spm|f|from|source|ref|ref_src|share|share_token|scene|"
                      r"chksm|mpshare|srcid|sharer_\w+|fbclid|gclid|igshid|_hsenc|_hsmi|"
                      r"mc_cid|mc_eid|wt\.\w+|ns_\w+)$", re.I)


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:   # 移动版与桌面版视为同一页
        host = host[2:]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not TRACKING.match(k)]
    path = re.sub(r"/+$", "", p.path) or "/"
    path = re.sub(r"/index\.(html?|php)$", "", path) or "/"
    # arXiv 的 abs/pdf/版本号指向同一篇论文
    if host.endswith("arxiv.org"):
        m = re.search(r"(\d{4}\.\d{4,5})", path)
        if m:
            return f"https://arxiv.org/abs/{m.group(1)}"
    return urlunsplit(("https", host, path, urlencode(sorted(q)), ""))


def _norm_text(s: str) -> str:
    s = re.sub(r"https?://\S+", "", s or "")
    s = re.sub(r"[\s\W_]+", "", s.lower())
    return s


def _shingles(s: str, n: int = 3):
    s = _norm_text(s)
    if len(s) <= n:
        return [s] if s else []
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def simhash(text: str) -> str:
    v = [0] * 64
    sh = _shingles(text[:20000])
    if not sh:
        return "0" * 16
    for g in sh:
        h = int.from_bytes(hashlib.md5(g.encode()).digest()[:8], "big")
        for i in range(64):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if v[i] > 0:
            out |= 1 << i
    return f"{out:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def content_hash(text: str) -> str:
    return hashlib.sha256(_norm_text(text).encode()).hexdigest()[:32]


def title_sim(a: str, b: str) -> float:
    """字符二元组 Jaccard，中英文都适用。"""
    def grams(s):
        s = _norm_text(s)
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}
    ga, gb = grams(a or ""), grams(b or "")
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def parse_date(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, timezone.utc)
    s = str(s).strip()
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    s2 = re.sub(r"\s+", " ", s).replace("Z", "+00:00")
    s2 = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s2)          # +0800 → +08:00
    s2 = re.sub(r"\s+([+-]\d{2}:\d{2})$", r"\1", s2)           # "15:30:18 +08:00" → 去掉空格
    for cand in (s2, s2.replace(" ", "T", 1)):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue
    m = re.match(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    return None


def iso(s) -> str | None:
    d = parse_date(s)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None
