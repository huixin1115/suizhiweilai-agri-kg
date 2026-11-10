#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「作物 × 生育期」图片清单
=============================
从 Neo4j 里取出所有 (作物, 生育期) 组合，按阶段语义拼出用于文生图的提示词，
输出到 data/stage_images.json，供批量生成与放置图片时对照使用。

用法：
    python scripts/gen_stage_manifest.py            # 生成清单
    python scripts/gen_stage_manifest.py --list     # 只打印，不写文件

图片命名规则（详见 static/stages/README.md）：
    static/stages/作物名__阶段名.jpg
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.phenology import safe_name, stage_short      # noqa: E402
import config                                          # noqa: E402

OUT_JSON = os.path.join(BASE, "data", "stage_images.json")

# ---------------------------------------------------------------- 阶段视觉描述
# 按顺序匹配，先命中先用；关键词取自图谱里真实出现过的 64 种阶段名
STAGE_VISUAL = [
    (("播种出苗", "萌芽出苗", "直播出苗", "育苗"), "刚破土出苗，幼苗矮小、子叶舒展，土面湿润，出苗整齐"),
    (("幼苗", "苗期", "幼龄"), "苗期，植株矮小，叶片嫩绿舒展，株行间还能看见土壤"),
    (("分蘖",), "分蘖期，植株基部分蘖多、株丛密集，叶色浓绿"),
    (("移栽", "定植", "缓苗"), "刚移栽定植缓苗，植株挺立，叶片略有失水后恢复的状态"),
    (("拔节", "伸蔓", "抽梢"), "茎秆节间明显伸长、株型快速增高，叶片宽大浓绿"),
    (("孕穗",), "孕穗期，旗叶包裹着鼓起的幼穗，叶色深绿"),
    (("抽穗", "开花", "授粉", "花铃"), "花期，穗子或花序完全抽出、花朵开放，田间一片繁茂"),
    (("蕾", "花芽"), "现蕾期，植株顶端出现花蕾，叶片繁茂"),
    (("灌浆",), "灌浆期，穗部籽粒开始灌浆饱满，下部叶片开始转黄"),
    (("结荚", "鼓粒", "角果"), "结荚鼓粒期，豆荚或角果挂满植株，籽粒逐渐饱满"),
    (("结球", "产品器官"), "产品器官形成期，叶球或食用器官紧实成形"),
    (("块茎", "块根", "根茎", "根部", "药用部位"), "地下部产品器官快速膨大期，地上部健壮生长"),
    (("膨大", "发育", "浆果", "幼果"), "果实挂满枝头，果实快速膨大、色泽饱满"),
    (("淀粉", "糖分"), "产品器官积累期，果实或块茎充实、糖分/淀粉积累"),
    (("着色", "转色"), "果实转色成熟期，果面由绿转红/黄，色泽鲜亮"),
    (("吐絮",), "吐絮期，棉铃开裂、白色棉絮外露"),
    (("成熟", "收获", "采收", "采摘", "采挖"), "成熟期，籽粒或果实饱满成熟，植株整体转黄，可收获"),
    (("休眠", "越冬", "落叶"), "休眠或越冬期，植株停止生长，枝叶稀疏或落叶"),
    (("莲座",), "莲座期，叶片平展成盘状，叶面积迅速扩大"),
    (("发棵", "分枝"), "发棵分枝期，侧枝大量发生，株幅明显增大"),
    (("旺盛生长", "生长盛期", "营养生长", "生殖生长"), "旺盛生长期，植株高大茂密、长势强健"),
]

CROP_TYPE_HINT = [
    (("苹果", "梨", "桃", "柑橘", "龙眼", "荔枝", "芒果", "石榴", "樱桃", "杏", "李", "枣",
      "核桃", "板栗", "柿", "猕猴桃", "柠檬", "柚子", "枇杷", "山楂", "木瓜"),
     "果园里整株果树的树冠与结果情况，背景是成排果园"),
    (("茶叶", "茶"), "茶园成行茶蓬，采摘面整齐"),
    (("水稻", "小麦", "玉米", "大麦", "高粱", "谷子", "燕麦", "黑麦"), "大田里成片生长的禾本科作物，可见穗部"),
    (("大豆", "花生", "油菜", "芝麻", "向日葵", "棉花"), "大田成片生长，植株整齐，可见花或荚果"),
    (("甘蓝", "白菜", "萝卜", "胡萝卜", "番茄", "黄瓜", "辣椒", "茄子", "南瓜", "冬瓜",
      "西瓜", "甜瓜", "洋葱", "大蒜", "大葱", "芹菜", "菠菜", "生菜", "豇豆", "菜豆"),
     "菜田里成垄生长的蔬菜植株"),
    (("人参", "三七", "枸杞", "黄芪", "当归", "天麻", "灵芝"), "药园畦面上成片生长的药用植物"),
]


def visual_for(short):
    for kws, desc in STAGE_VISUAL:
        if any(k in short for k in kws):
            return desc
    return "该生育期的田间生长状态，植株长势正常"


def scene_for(crop, short):
    base = "自然农场环境里成片生长的植株"
    for kws, desc in CROP_TYPE_HINT:
        if any(k in crop for k in kws):
            base = desc
            break
    # 苗期阶段不该出现穗、果、花，避免图文不符
    early = any(k in short for k in ("播种", "出苗", "幼苗", "苗期", "育苗", "分蘖",
                                     "移栽", "定植", "缓苗", "莲座", "幼龄", "萌芽"))
    if early:
        base = (base.replace("，可见穗部", "")
                    .replace("树冠与结果情况，", "树势与枝叶情况，")
                    .replace("，可见花或荚果", ""))
    return base


def build_prompt(crop, short, days):
    # 提示词保持精简：批量生成时工具会把整段提示词回显，越长越拖慢流程
    return "{crop}{short}：{visual}。{scene}。写实农业摄影，自然光，无文字水印。".format(
        crop=crop, short=short, visual=visual_for(short), scene=scene_for(crop, short))


def main():
    write = "--list" not in sys.argv
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PWD))
    with drv.session() as s:
        rows = s.run("""
            MATCH (c:作物)-[:REL {type:'生育期'}]->(st:生育期)
            RETURN c.name AS crop, st.name AS stage, st.`阶段序号` AS idx,
                   st.`历时天数` AS days, st.`起始日` AS start, st.`结束日` AS end
            ORDER BY c.name, idx
        """).data()
    drv.close()

    seen, items = set(), []
    for r in rows:
        crop, stage = r["crop"], r["stage"]
        key = (crop, stage)
        if key in seen:
            continue
        seen.add(key)
        short = stage_short(stage)
        items.append({
            "crop": crop,
            "stage": stage,
            "short": short,
            "idx": r["idx"],
            "days": r["days"],
            "start": r["start"],
            "end": r["end"],
            # 生成时的临时目录名（生成完统一搬到 static/stages/）
            "raw_dir": "%s__%s" % (safe_name(crop), safe_name(short)),
            "file": "%s__%s.jpg" % (safe_name(crop), safe_name(short)),
            "prompt": build_prompt(crop, short, r["days"]),
        })

    print("作物 %d 个 / 组合 %d 组" % (len({i['crop'] for i in items}), len(items)))
    if write:
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with io.open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print("已写出：", OUT_JSON)
    else:
        for i in items[:6]:
            print("-", i["raw_dir"])
            print("   ", i["prompt"])


if __name__ == "__main__":
    main()
