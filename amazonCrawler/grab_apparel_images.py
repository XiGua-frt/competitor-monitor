#!/usr/bin/env python3
"""Amazon 服装品类图片采集：男/女 × 多品类 × 每类 Top 3 自然结果主图。

用法:
  python3.11 grab_apparel_images.py --list                 # 查看品类清单
  python3.11 grab_apparel_images.py --only "mens-tshirt"   # 只跑指定品类(逗号分隔, 测试用)
  python3.11 grab_apparel_images.py                        # 全量跑
输出:
  images/<gender>/<category>/<rank>_<asin>.jpg
  images/index.json
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from amzpy import AmazonScraper

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "images"
BASE = "https://www.amazon.com"

# 品类清单: key -> (性别, 中文名, 搜索关键词)
CATEGORIES = [
    # ---- 男款 ----
    ("mens-tshirt",        "men",   "男款T恤",     "t-shirt"),
    ("mens-polo",          "men",   "男款Polo衫",  "polo shirt"),
    ("mens-shirt",         "men",   "男款衬衫",    "button down shirt men"),
    ("mens-hoodie",        "men",   "男款连帽卫衣", "hoodie men"),
    ("mens-sweatshirt",    "men",   "男款套头卫衣", "crewneck sweatshirt men"),
    ("mens-sweater",       "men",   "男款针织毛衣", "sweater men"),
    ("mens-jacket",        "men",   "男款夹克",    "jacket men"),
    ("mens-coat",          "men",   "男款大衣",    "overcoat men"),
    ("mens-jeans",         "men",   "男款牛仔裤",  "jeans men"),
    ("mens-chinos",        "men",   "男款休闲裤",  "chinos men"),
    ("mens-joggers",       "men",   "男款运动裤",  "joggers men"),
    ("mens-shorts",        "men",   "男款短裤",    "shorts men"),
    ("mens-suit",          "men",   "男款西装",    "suit men"),
    ("mens-activewear",    "men",   "男款运动服",  "athletic wear men"),
    ("mens-underwear",     "men",   "男款内衣裤",  "underwear men"),
    ("mens-pajamas",       "men",   "男款睡衣",    "pajamas men"),
    # ---- 女款 ----
    ("womens-tshirt",      "women", "女款T恤",     "t-shirt women"),
    ("womens-blouse",      "women", "女款衬衫",    "blouse women"),
    ("womens-dress",       "women", "女款连衣裙",  "dress women"),
    ("womens-skirt",       "women", "女款半身裙",  "skirt women"),
    ("womens-hoodie",      "women", "女款卫衣",    "hoodie women"),
    ("womens-sweater",     "women", "女款针织毛衣", "sweater women"),
    ("womens-jacket",      "women", "女款夹克",    "jacket women"),
    ("womens-coat",        "women", "女款大衣",    "coat women"),
    ("womens-jeans",       "women", "女款牛仔裤",  "jeans women"),
    ("womens-leggings",    "women", "女款打底裤",  "leggings women"),
    ("womens-pants",       "women", "女款休闲裤",  "pants women"),
    ("womens-shorts",      "women", "女款短裤",    "shorts women"),
    ("womens-activewear",  "women", "女款运动服",  "activewear women"),
    ("womens-swimsuit",    "women", "女款泳衣",    "swimsuit women"),
    ("womens-lingerie",    "women", "女款内衣",    "lingerie women"),
    ("womens-pajamas",     "women", "女款睡衣",    "pajamas women"),
]

TOP_N = 3
GAP_SECONDS = 15          # 每个搜索请求之间的间隔(自然爬取节奏)
IMG_UA_FALLBACK = None


def upscale_image_url(url: str) -> str:
    """把搜索结果缩略图 URL 换成高分辨率版本(去掉 resize 段)."""
    if not url:
        return url
    # _SX300_ / _SY300_ 等尺寸标记替换为最大规格
    url = re.sub(r"._SX\d+(_CR[\d,]+,_\d+,_\d+,_\d+)?_.jpg$", ".jpg", url)
    url = re.sub(r"._SY\d+(_CR[\d,]+,_\d+,_\d+,_\d+)?_.jpg$", ".jpg", url)
    url = re.sub(r"._AC_[^\.]*\.jpg$", ".jpg", url)
    return url


def is_sponsored(container) -> bool:
    classes = " ".join(container.get("class", []))
    if "AdHolder" in classes:
        return True
    text = container.get_text(" ", strip=True)[:400].lower()
    if "sponsored" in text[:200]:
        return True
    return False


def handle_checkpoint(scraper, html: str) -> bool:
    """检测 Continue shopping checkpoint 中间页, 自动提交 token 放行."""
    if "checkpointForm" not in html:
        return False
    m = re.search(r'name="token" value="([^"]+)"', html)
    if not m:
        return False
    print("  [checkpoint] 检测到软拦截, 提交 token 放行...")
    try:
        scraper.session.session.post(
            f"{BASE}/checkpoint/validate", data={"token": m.group(1)},
            headers={"User-Agent": scraper.session.ua_generator.random},
            timeout=25, allow_redirects=True)
    except Exception as e:
        print(f"  [checkpoint] 提交失败: {e}")
    time.sleep(5)
    return True


def handle_akamai_interstitial(scraper, html: str, url: str) -> bool:
    """检测 Akamai bm-verify 小页, 计算 pow 并 POST /_sec/verify 放行."""
    if 'bm-verify' not in html or len(html) > 10000:
        return False
    import re
    m = re.search(r'"bm-verify":\s*"([^"]+)"', html)
    mi = re.search(r'var i = (\d+); var j = i \+ Number\("(\d+)" \+ "(\d+)"\);', html)
    if not (m and mi):
        return False
    pow_val = int(mi.group(1)) + int(mi.group(2) + mi.group(3))
    print("  [akamai] interstitial 检测到, 提交 verify (pow={})...".format(pow_val))
    try:
        scraper.session.session.post(
            f"{BASE}/_sec/verify?provider=interstitial",
            data=json.dumps({"bm-verify": m.group(1), "pow": pow_val}),
            headers={"Content-Type": "application/json",
                     "User-Agent": scraper.session.ua_generator.random,
                     "Referer": url, "Origin": BASE},
            timeout=25)
    except Exception as e:
        print(f"  [akamai] verify 失败: {e}")
    time.sleep(8)
    return True


def warm_up(scraper):
    """访问首页暖 cookie, 避免 Akamai 对冷会话搜索页下发 interstitial."""
    try:
        scraper.session.get(f"{BASE}/")
    except Exception:
        pass
    time.sleep(3)


def top_organic(scraper, query: str, dept: str, top_n: int = TOP_N):
    """搜索并返回前 top_n 个自然结果."""
    url = f"{BASE}/s?k={query.replace(' ', '+')}&i={dept}"
    resp = scraper.session.get(url)
    if not resp or not resp.text:
        return [], "no_response"
    if "captcha" in resp.text[:4000].lower() or "api-services-support@amazon.com" in resp.text[:4000]:
        return [], "captcha"
    if "Robot Check" in resp.text[:2000]:
        return [], "captcha"

    from amzpy.parser import parse_search_page
    products = []
    for attempt in range(3):
        handled = False
        if "checkpointForm" in resp.text:
            handle_checkpoint(scraper, resp.text)
            handled = True
        if handle_akamai_interstitial(scraper, resp.text, url):
            handled = True
        if handled:
            resp = scraper.session.get(url)
            if not resp or not resp.text:
                return [], "no_response"
        products = parse_search_page(resp.text, BASE, country_code="com")
        if products:
            break
        print(f"  [retry {attempt+1}/3] 解析为空({len(resp.text)}B), 等待后重试...")
        time.sleep(20 + attempt * 20)
        if attempt == 1:
            warm_up(scraper)   # 冷会话可能被 Akamai 盯上, 首页暖一下
        resp = scraper.session.get(url)
        if not resp or not resp.text:
            return [], "no_response"
    if not products:
        kind = "checkpoint" if "checkpointForm" in resp.text else \
               "akamai" if 'bm-verify' in resp.text else "unknown"
        return [], f"parse_empty_{kind}"

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "lxml")

    # 收集广告位 ASIN, 按自然顺序过滤
    sponsored_asins = set()
    for c in soup.select("div[data-asin]"):
        if is_sponsored(c) and c.get("data-asin"):
            sponsored_asins.add(c.get("data-asin"))

    organic = [p for p in products if p.get("asin") not in sponsored_asins]
    return organic[:top_n], "ok"


def download_image(scraper, url: str, dest: Path) -> bool:
    try:
        resp = scraper.session.get(url)
        if not resp or not resp.text:
            return False
        data = resp.content
        if not data or len(data) < 2000:   # 过小通常是占位图
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"    [img-fail] {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑指定品类 key, 逗号分隔")
    ap.add_argument("--gap", type=int, default=GAP_SECONDS, help="请求间隔秒数")
    ap.add_argument("--top", type=int, default=TOP_N, help="每类取前 N 个")
    args = ap.parse_args()

    selected = CATEGORIES
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        selected = [c for c in CATEGORIES if c[0] in keys]

    scraper = AmazonScraper(country_code="com")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    warm_up(scraper)   # 暖 cookie, 显著降低 Akamai interstitial 概率
    index = []
    ok_count = 0
    cat_path = OUT_DIR / "index.json"
    if cat_path.exists():
        index = json.loads(cat_path.read_text())

    for i, (key, gender, cn_name, query) in enumerate(selected):
        dept = "fashion-mens" if gender == "men" else "fashion-womens"
        print(f"\n[{i+1}/{len(selected)}] {gender}/{key} ({cn_name}): {query}")
        products, status = top_organic(scraper, query, dept, args.top)
        if status != "ok" or not products:
            print(f"  [!] {status or 'no results'}, 跳过")
            index.append({"key": key, "gender": gender, "name": cn_name,
                          "query": query, "status": status, "items": []})
            continue

        items = []
        cat_dir = OUT_DIR / gender / key
        cat_dir.mkdir(parents=True, exist_ok=True)
        for rank, p in enumerate(products[:args.top], 1):
            asin = p.get("asin")
            img = upscale_image_url(p.get("img_url") or "")
            title = (p.get("title") or "")[:120]
            dest = cat_dir / f"{rank}_{asin}.jpg"
            dl = False
            if img:
                dl = download_image(scraper, img, dest)
                time.sleep(2)
            items.append({"rank": rank, "asin": asin, "title": title,
                          "img_url": img, "file": str(dest.relative_to(OUT_DIR)) if dl else None,
                          "price": p.get("price"), "rating": p.get("rating"),
                          "ratings_count": p.get("ratings_count")})
            print(f"  #{rank} {asin} dl={'✓' if dl else '✗'} {title[:60]}")
            if dl:
                ok_count += 1

        index = [e for e in index if e["key"] != key]
        index.append({"key": key, "gender": gender, "name": cn_name,
                      "query": query, "status": "ok", "items": items})
        cat_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))

        if i < len(selected) - 1:
            print(f"  [等待 {args.gap}s]")
            time.sleep(args.gap)

    cat_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    done = sum(1 for e in index for it in e.get("items", []) if it.get("file"))
    print(f"\n完成: {done} 张图片 → {OUT_DIR}")
    print(f"索引: {cat_path}")


if __name__ == "__main__":
    main()
