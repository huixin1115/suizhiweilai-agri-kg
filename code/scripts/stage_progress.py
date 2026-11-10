#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量生成生育期实拍图 · 进度管理
==============================
把 data/stage_images.json 里的 287 组「作物 × 生育期」按优先级排序，
每次输出还没生成的若干组（含提示词与目标目录），生成完一组就少一组，可随时中断续跑。

用法：
    python scripts/stage_progress.py            # 显示总体进度 + 下一批（默认 6 组）
    python scripts/stage_progress.py 12         # 指定本批条数
    python scripts/stage_progress.py --reset    # 清空进度统计缓存

优先级：主粮与主栽作物排前面，其余按主产区数量降序。
"""
import io
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.phenology import safe_name        # noqa: E402

MANIFEST = os.path.join(BASE, "data", "stage_images.json")
RAW_DIR = os.path.join(BASE, "static", "stage_raw")
FINAL_DIR = os.path.join(BASE, "static", "stages")
NORM_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]")

# 优先级：这些作物先做（演示最常用）
PRIORITY_CROPS = [
    "水稻", "小麦", "玉米", "大豆", "马铃薯", "棉花", "番茄", "黄瓜", "辣椒",
    "白菜", "大白菜", "苹果", "梨", "桃", "柑橘", "油菜", "花生", "甘薯",
]


def load():
    with io.open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def raw_stems():
    """stage_raw 下所有原始图文件名（去扩展名、去掉一切符号）——用于按前缀判断是否已生成。
    因为并行生成时 output_dir 不可靠，图片可能落在任意子目录，只能按文件名认。"""
    out = []
    for root, _dirs, fns in os.walk(RAW_DIR):
        for fn in fns:
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                out.append(NORM_RE.sub("", os.path.splitext(fn)[0]))
    return out


def has_final(fname):
    stem = os.path.splitext(fname)[0]
    return any(os.path.isfile(os.path.join(FINAL_DIR, stem + e))
               for e in (".jpg", ".jpeg", ".png", ".webp"))


def ordered(items):
    def key(it):
        try:
            return PRIORITY_CROPS.index(it["crop"])
        except ValueError:
            return len(PRIORITY_CROPS)
    return sorted(items, key=lambda it: (key(it), it["crop"], it["idx"] or 0))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(args[0]) if args else 6
    items = ordered(load())
    stems = raw_stems()

    def is_done(it):
        if has_final(it["file"]):
            return True
        prefix = NORM_RE.sub("", it["crop"] + safe_name(it["short"]))
        return any(s.startswith(prefix) for s in stems)

    todo = [it for it in items if not is_done(it)]
    print("进度：%d / %d 已生成（剩余 %d）" % (len(items) - len(todo), len(items), len(todo)))
    if not todo:
        print("全部生成完毕，请执行：python scripts/collect_stage_images.py")
        return
    print("---- 本批 %d 组 ----" % min(n, len(todo)))
    for it in todo[:n]:
        print("### %s" % it["raw_dir"])
        print("PROMPT: %s" % it["prompt"])
        print("OUTDIR: %s" % os.path.join(RAW_DIR, it["raw_dir"]))


if __name__ == "__main__":
    main()
