"""图片下载与质检：保存清晰原图到本地，记录尺寸、哈希，筛掉小图/横幅/重复图。"""
from __future__ import annotations

import hashlib
import io
import re

from .. import config, db
from .fetch import FetchError, fetcher

MIN_W, MIN_H = 320, 180          # 低于此尺寸不适合做正文配图
EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp", "AVIF": "avif"}


def ahash(im) -> str:
    """平均哈希：8x8 灰度，64 位，用于发现同一张图的不同尺寸/压缩版本。"""
    g = im.convert("L").resize((8, 8))
    px = list(g.get_flattened_data() if hasattr(g, "get_flattened_data") else g.getdata())
    avg = sum(px) / 64
    bits = "".join("1" if p >= avg else "0" for p in px)
    return f"{int(bits, 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


HIRES_RULES = [
    # 公众号图片：/640?wx_fmt=… 是 640 宽的压缩版，/0? 是原图
    (re.compile(r"(mmbiz\.qpic\.cn/.+?)/\d{2,4}(\?|$)"), r"\1/0\2"),
    # 知乎：_720w / _b / _hd 等是缩放版，_r 是原图
    (re.compile(r"(zhimg\.com/.+?)_(?:\d+w|b|hd|l|xl|qhd|180x120)(\.\w+)"), r"\1_r\2"),
    # 新浪图床：/mw690/ /thumb150/ 等 → /large/
    (re.compile(r"(sinaimg\.cn)/(?:mw\d+|thumb\d+|orj\d+|bmiddle|small)/"), r"\1/large/"),
    # 阿里云 OSS、七牛、腾讯云的在线缩放参数
    (re.compile(r"[?&](?:x-oss-process|imageView2|imageMogr2|imageslim)[^#]*"), ""),
    # 头条/抖音系：~tplv-xxx-resize:640:0.image
    (re.compile(r"~tplv-[\w-]+(?::\d+)*(?::\d+)?\.(?:image|webp|jpeg|png)"), ""),
    # 文件名里的尺寸后缀：xxx_640x360.jpg、xxx-300x200.png（WordPress 缩略图）
    (re.compile(r"[-_]\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp))"), r"\1"),
]


def hires_candidates(url: str) -> list[str]:
    """同一张图可能的原图地址（去掉 CDN 的缩放参数）。只返回和原地址不同的。"""
    out = []
    for pat, rep in HIRES_RULES:
        u = pat.sub(rep, url)
        if u != url and u not in out:
            out.append(u.rstrip("?&"))
    return out


def download_best(url: str, referer: str | None = None) -> dict:
    """先试原图地址，比给出的地址更大才用；否则用给出的地址。结果里 upgraded_from 记录升级前的尺寸。"""
    base = download(url, referer)
    for u in hires_candidates(url)[:2]:
        alt = download(u, referer)
        if alt.get("ok") and (alt.get("width") or 0) > (base.get("width") or 0) * 1.15:
            alt.update(url=u, upgraded_from=f"{base.get('width')}×{base.get('height')}" if base.get("width") else "无法下载")
            return alt
    return base


def download(url: str, referer: str | None = None) -> dict:
    """下载并质检一张图片。返回 {ok, local_path, width, height, sha256, phash, reason}。"""
    from PIL import Image, UnidentifiedImageError
    headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer      # 很多图床有防盗链
    try:
        r = fetcher.get(url, respect_robots=False, cache_ttl=7 * 86400, headers=headers)
    except FetchError as e:
        return {"ok": False, "reason": f"下载失败：{e.kind} {e.reason}"}
    data = r.content
    if len(data) < 2048:
        return {"ok": False, "reason": f"文件过小 {len(data)}B（占位图/像素点）"}
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError) as e:
        return {"ok": False, "reason": f"不是可解析的图片：{e}"}
    w, h = im.size
    sha = hashlib.sha256(data).hexdigest()
    ph = ahash(im)
    out = {"ok": True, "width": w, "height": h, "sha256": sha, "phash": ph,
           "bytes": len(data), "format": im.format}
    if w / h > 4.5 or h / w > 4.5:
        out.update(ok=False, reason=f"宽高比异常 {w}x{h}（横幅/长图）")
    elif w < MIN_W or h < MIN_H:
        out.update(ok=False, reason=f"分辨率过低 {w}x{h}")
    ext = EXT.get(im.format or "", "img")
    path = config.IMAGE_DIR / f"{sha[:20]}.{ext}"
    if not path.exists():
        path.write_bytes(data)
    out["local_path"] = str(path.relative_to(config.DATA_DIR))
    return out


def store_images(doc_id: str, page_url: str, images: list[dict], max_images: int = 12,
                 download_fn=download) -> dict:
    """把抽取出的候选图片落库：被规则筛掉的只记录原因不下载；其余下载质检后去重。"""
    kept = rejected = 0
    existing = db.query("SELECT url, phash FROM images WHERE doc_id=?", (doc_id,))
    seen_urls = {e["url"] for e in existing}
    seen_hash = [e["phash"] for e in existing if e["phash"]]
    for im in images:
        if im["url"] in seen_urls:
            continue
        seen_urls.add(im["url"])
        row = dict(id=db.new_id("img_"), doc_id=doc_id, url=im["url"], caption=im.get("caption"),
                   context_text="\n".join(x for x in (im.get("context_before"),
                                                      im.get("context_after")) if x) or None,
                   position=im.get("position"), license=im.get("license") or
                   "未声明（版权归原作者，仅作引用）", created_at=db.now())
        reason = im.get("reject_reason")
        if not reason and kept >= max_images:
            reason = f"超过单篇上限 {max_images} 张"
        if not reason:
            res = download_fn(im["url"], referer=page_url)
            row.update(width=res.get("width"), height=res.get("height"),
                       sha256=res.get("sha256"), phash=res.get("phash"),
                       local_path=res.get("local_path"))
            reason = None if res["ok"] else res["reason"]
            if not reason and res.get("phash"):
                dup = any(hamming(res["phash"], h) <= 4 for h in seen_hash)
                if dup:
                    reason = "与本文另一张图片近似重复"
                else:
                    seen_hash.append(res["phash"])
        if not row.get("caption") and im.get("alt"):
            row["caption"] = im["alt"]
        row.update(status="rejected" if reason else "kept", reject_reason=reason)
        db.insert("images", row)
        if reason:
            rejected += 1
        else:
            kept += 1
    return {"kept": kept, "rejected": rejected}
