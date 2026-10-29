#!/usr/bin/env python3
"""Amazon 服装详情页图廊抓取：对 images/index.json 里的 96 个 ASIN 抓完整图组.

每商品通常 7-9 张: MAIN(白底主图) + PT01~PT09(场景/细节/尺码/多角度).
输出:
  images/gallery/<ASIN>/<idx>_<variant>.jpg
  images/gallery_index.json   (每 ASIN 的图组清单, 含 variant 类型)
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grab_apparel_images as g
from amzpy import AmazonScraper

ROOT = Path(__file__).resolve().parent
GAL_DIR = ROOT / "images" / "gallery"
IDX_PATH = ROOT / "images" / "index.json"
GAL_IDX = ROOT / "images" / "gallery_index.json"


def parse_gallery(html: str):
    """从详情页 HTML 提取 initial 图廊数组."""
    m = re.search(
        r"'colorImages'\s*:\s*\{\s*'initial'\s*:\s*A\.\$\.parseJSON\('(.*?)'\)",
        html, re.S)
    if not m:
        # 备用: 双引号 JSON 格式
        m = re.search(r'"colorImages"\s*:\s*\{\s*"initial"\s*:\s*(\[.*?\])', html, re.S)
        if not m:
            return []
        raw = m.group(1)
    else:
        raw = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    try:
        return json.loads(raw)
    except Exception:
        return []


def download(scraper, url: str, dest: Path) -> bool:
    try:
        resp = scraper.session.get(url)
        if not resp or not resp.content or len(resp.content) < 2000:
            return False
        dest.write_bytes(resp.content)
        return True
    except Exception as e:
        print(f"    [img-fail] {e}", file=sys.stderr)
        return False


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--gender", choices=["men", "women"], help="只抓指定性别品类")
    ap.add_argument("--only", help="只抓指定品类 key, 逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="最多抓取的未完成 ASIN 数, 0=不限")
    args = ap.parse_args()

    asins = []
    seen = set()
    for e in json.loads(IDX_PATH.read_text()):
        if args.gender and e["gender"] != args.gender:
            continue
        if args.only and e["key"] not in {k.strip() for k in args.only.split(",")}:
            continue
        for item in e.get("items", []):
            a = item.get("asin")
            if a and a not in seen:
                seen.add(a)
                asins.append({"asin": a, "category": e["key"], "gender": e["gender"],
                              "rank": item["rank"], "title": item.get("title", "")})
    print(f"待抓取 ASIN: {len(asins)}")

    scraper = AmazonScraper(country_code="com")
    g.warm_up(scraper)

    gal_index = json.loads(GAL_IDX.read_text()) if GAL_IDX.exists() else {}
    done = {a for a, v in gal_index.items() if v.get("downloaded")}

    budget = args.limit if args.limit and args.limit > 0 else None
    for n, info in enumerate(asins, 1):
        asin = info["asin"]
        if asin in done:
            continue
        if budget is not None and budget <= 0:
            print(f"[限量 {args.limit}] 已抓够, 停止")
            break
        if budget is not None:
            budget -= 1
        print(f"[{n}/{len(asins)}] {asin} ({info['gender']}/{info['category']} #{info['rank']})")

        url = f"{g.BASE}/dp/{asin}"
        resp = scraper.session.get(url)
        if not resp or not resp.text or "productTitle" not in resp.text:
            # checkpoint / akamai 处理后重试
            if resp and "checkpointForm" in resp.text:
                g.handle_checkpoint(scraper, resp.text)
            if resp and "bm-verify" in resp.text:
                g.handle_akamai_interstitial(scraper, resp.text, url)
            time.sleep(10)
            resp = scraper.session.get(url)
            if not resp or not resp.text or "productTitle" not in resp.text:
                print("  [!] 详情页获取失败, 跳过")
                gal_index[asin] = {**info, "downloaded": False, "reason": "page_fail"}
                GAL_IDX.write_text(json.dumps(gal_index, ensure_ascii=False, indent=2))
                continue

        imgs = parse_gallery(resp.text)
        if not imgs:
            # 备用: 抓所有 hiRes 去重
            imgs = [{"hiRes": u, "variant": f"HIR{i}"} for i, u in
                    enumerate(dict.fromkeys(re.findall(r'"hiRes":"([^"]+)"', resp.text)))]
            print(f"  [fallback] hiRes 提取: {len(imgs)} 张")
        else:
            print(f"  图廊: {len(imgs)} 张")

        dest_dir = GAL_DIR / asin
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for i, img in enumerate(imgs, 1):
            u = img.get("hiRes") or img.get("large")
            if not u:
                continue
            variant = img.get("variant", f"V{i}")
            ext = ".jpg"
            dest = dest_dir / f"{i:02d}_{variant}{ext}"
            ok = download(scraper, u, dest)
            files.append({"file": str(dest.relative_to(ROOT)) if ok else None,
                          "variant": variant, "url": u})
            time.sleep(1.5)
        n_ok = sum(1 for f in files if f["file"])
        print(f"  下载: {n_ok}/{len(files)}")
        gal_index[asin] = {**info, "downloaded": n_ok > 0, "images": files}
        GAL_IDX.write_text(json.dumps(gal_index, ensure_ascii=False, indent=2))

        if budget is None or budget > 0:
            print(f"  [等待 12s]")
            time.sleep(12)

    total = sum(1 for v in gal_index.values() if v.get("downloaded"))
    n_imgs = sum(1 for v in gal_index.values()
                 for f in v.get("images", []) if f.get("file"))
    print(f"\n完成: {total} 个 ASIN, {n_imgs} 张图 → {GAL_DIR}")
    print(f"索引: {GAL_IDX}")


if __name__ == "__main__":
    main()
