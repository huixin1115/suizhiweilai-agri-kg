#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生育期图片归集脚本 collect_stage_images.py
========================================
把 static/stage_raw/ 下（含所有子目录）生成的原始图，按**文件名里的作物与阶段**认领，
统一裁剪水印、压缩为 JPG，落到 static/stages/作物名__阶段名.jpg。

为什么要按文件名认领：文生图工具并行调用时 output_dir 不可靠（多张图会挤进同一个目录），
但生成的文件名里一定带着「专业农业实拍摄影_<作物>处于_<阶段>__…」，据此反查清单最稳。

用法：
    python scripts/collect_stage_images.py            # 归集全部
    python scripts/collect_stage_images.py --dry-run  # 只看会处理什么，不写文件
    python scripts/collect_stage_images.py --keep     # 保留原始文件（默认处理完就删）
"""
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.phenology import safe_name, stage_short      # noqa: E402

MANIFEST = os.path.join(BASE, "data", "stage_images.json")
RAW_DIR = os.path.join(BASE, "static", "stage_raw")
FINAL_DIR = os.path.join(BASE, "static", "stages")

# 早期提示词生成的文件名形如：专业农业实拍摄影_小麦处于_拔节期__茎秆节间…
# 精简提示词后文件名直接在开头带上「作物+阶段」，例如：小麦拔节期_茎秆节间伸长_…
NAME_RE = re.compile(r"^专业农业实拍摄影[_-](.+?)处于[_-](.+?)__")
NORM_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")
WATERMARK_CROP_RATIO = 0.08      # 裁掉底部 8%（生成平台会在此处打「AI生成」水印）
JPEG_QUALITY = 88


def load_manifest():
    with io.open(MANIFEST, encoding="utf-8") as f:
        items = json.load(f)
    idx = {}
    for it in items:
        idx[(it["crop"], safe_name(it["short"]))] = it

    # 归一化前缀索引。除了「作物+完整阶段名」，再补一个「作物+阶段名首段」的宽松前缀，
    # 因为手写提示词时常常省略斜杠后半段（如把「莲座期/旺盛生长期」写成「莲座期」）。
    # 宽松前缀只有在全局唯一时才启用，避免「开花期」和「开花坐果期」互相抢图。
    from collections import Counter
    loose = Counter()
    for it in items:
        head = it["short"].split("/")[0].strip()
        loose[NORM_RE.sub("", it["crop"] + head)] += 1

    prefixes = []
    for it in items:
        prefixes.append((NORM_RE.sub("", it["crop"] + safe_name(it["short"])), it))
        head = it["short"].split("/")[0].strip()
        lp = NORM_RE.sub("", it["crop"] + head)
        if loose[lp] == 1 and len(head) >= 3:        # 太短的段名容易误伤，要求至少 3 个字
            prefixes.append((lp, it))
    prefixes.sort(key=lambda x: -len(x[0]))
    return items, idx, prefixes


def parse_name(fname, idx, prefixes):
    """先按老格式正则认领，再按归一化前缀认领；认不出返回 None"""
    m = NAME_RE.match(fname)
    if m:
        crop = m.group(1).strip().strip("_")
        short = m.group(2).strip().strip("_")
        it = idx.get((crop, safe_name(short)))
        if it:
            return it
    stem = NORM_RE.sub("", os.path.splitext(fname)[0])
    for prefix, it in prefixes:
        if prefix and stem.startswith(prefix):
            return it
    return None


def main():
    dry = "--dry-run" in sys.argv
    keep = "--keep" in sys.argv

    try:
        from PIL import Image
    except ImportError:
        print("缺少 Pillow，请先安装：pip install Pillow")
        sys.exit(1)

    items, idx, prefixes = load_manifest()
    os.makedirs(FINAL_DIR, exist_ok=True)

    files = []
    for root, _dirs, fns in os.walk(RAW_DIR):
        for fn in fns:
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                files.append(os.path.join(root, fn))
    print("待归集原始图：%d 张" % len(files))

    ok, unmapped, failed = 0, [], 0
    for path in files:
        fn = os.path.basename(path)
        it = parse_name(fn, idx, prefixes)
        if not it:
            unmapped.append(fn)
            continue
        target = os.path.join(FINAL_DIR, it["file"])
        if dry:
            print("  %s  ->  %s" % (fn[:52], it["file"]))
            ok += 1
            continue
        try:
            im = Image.open(path).convert("RGB")
            w, h = im.size
            keep_h = max(1, int(h * (1 - WATERMARK_CROP_RATIO)))
            im = im.crop((0, 0, w, keep_h))
            im.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)
            ok += 1
            # 默认保留原始 PNG（既是备份，沙箱里也不允许随手删文件）；要清理请显式加 --clean
            if "--clean" in sys.argv:
                try:
                    os.remove(path)
                except Exception:
                    pass
        except Exception as e:
            failed += 1
            print("  处理失败 %s：%s" % (fn[:40], e))

    if not dry:
        # 清掉空目录
        for root, dirs, fns in os.walk(RAW_DIR, topdown=False):
            if root != RAW_DIR and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    print("-" * 50)
    print("已归集 %d 张 → %s" % (ok, FINAL_DIR))
    if failed:
        print("失败 %d 张" % failed)
    if unmapped:
        print("无法认领 %d 张（文件名里没有匹配到清单）：" % len(unmapped))
        for fn in unmapped[:10]:
            print("   ", fn[:80])
    have = len([f for f in os.listdir(FINAL_DIR)
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])
    print("static/stages 现有图片：%d / %d" % (have, len(items)))


if __name__ == "__main__":
    main()
