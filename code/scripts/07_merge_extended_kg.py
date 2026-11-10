#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
07 - 将扩展知识图谱（data/agri_kg_ext）与项目原有三元组（clean_triplets.csv）融合

设计原则：
1. 只读原文件，绝不覆盖 clean_triplets.csv ── 原数据一条不少
2. 关系名统一对齐到项目现有词表（主产区/适宜土壤/适宜气候/适配肥料/
   易感病害/易感虫害/防治方法/栽培要点），保证 app.py、rag_search.py、
   kg_display.html 不改代码即可命中新数据
3. 生长期、养分配方、轮作、季节等新型关系原样保留，供升级后的查询使用
4. 同时导出实体属性表，让 Neo4j 节点带上可计算的结构化属性

输出：
    output/clean_triplets_merged.csv   融合后的三元组
    output/entity_props.csv            实体 → 标签 → 属性
    output/merge_report.txt            融合统计
"""
import csv
import io
import json
import os
import sys
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # code/
OUT = os.path.join(BASE, "output")
EXT = os.path.join(BASE, "data", "agri_kg_ext")
ORIG_TRIPLETS = os.path.join(BASE, "clean_triplets.csv")

# ---------------------------------------------------------------- 标签映射
# 扩展图谱标签 -> 项目 Neo4j 标签（向后兼容）
# 注意：防治措施 / 病虫害 两类还要按实体属性二次细分，见 refine_label()
LABEL_MAP = {
    "农作物": "作物",
    "病虫害": "病虫害",
    "肥料": "农药肥料",
    "防治措施": "防治措施",
    "土壤类型": "土壤",
    "气候类型": "气候",
    "产区": "地域",
    "农事操作": "栽培要点",
    "生育期": "生育期",
    "品种": "品种",
    "作物类别": "作物类别",
    "养分元素": "养分元素",
    "季节": "季节",
}

# 防治措施细分：措施类型含这些词 → 农药肥料（化学/生物药剂），否则归入防治措施（农艺·物理防治）
CHEM_KW = ("农药",)
WEED_TYPE = ("草害", "杂草")


def refine_label(src_label, props):
    """
    按实体属性精修标签，避免"农药肥料"里混进农业/物理防治措施，
    避免"病虫害"里混进杂草。
    """
    if src_label == "病虫害":
        return "杂草" if str(props.get("类型", "")).strip() in WEED_TYPE else "病虫害"
    if src_label == "防治措施":
        kind = str(props.get("措施类型", "")).strip()
        return "农药肥料" if any(w in kind for w in CHEM_KW) else "防治措施"
    return LABEL_MAP.get(src_label, src_label)


# ---------------------------------------------------------------- 关系映射
REL_MAP = {
    "主产区": "主产区",
    "适宜土壤": "适宜土壤",
    "适宜气候": "适宜气候",
    "推荐肥料": "适配肥料",
    "防治措施": "防治方法",
    "关键管理措施": "栽培要点",
    "农事操作": "栽培要点",
    "经历生育期": "生育期",
    "养分需求": "需要养分",
    "含有养分": "含有养分",
    "适宜轮作": "适宜轮作",
    "收获季节": "收获季节",
    "播种季节": "播种季节",
    "属于类别": "属于类别",
    "品种属于": "品种属于",
    "主推品种": "主推品种",
    "主要土壤": "主要土壤",
    "属于气候区": "属于气候区",
    "适用作物": "适用作物",
    "常用改良剂": "常用改良剂",
    "易生杂草": "易生杂草",
    "易感病害": "易感病害",
    "易感虫害": "易感虫害",
}

# 病虫害按名称判别（仅在缺少"类型"属性时兜底）
PEST_KW = ("虫", "螟", "蛾", "蚜", "蝽", "蝇", "蛆", "蝼", "蛴", "螨",
           "虱", "蝉", "蚧", "甲", "蛉", "蜂", "蚁", "蛄", "螬", "蚋")


def map_pest_rel(tgt):
    """
    「易发病虫害」按目标实体的真实类型拆分：
        草害  -> 易生杂草
        虫害  -> 易感虫害
        其余  -> 易感病害
    """
    kind = str(tgt["props"].get("类型", "")).strip()
    if tgt["label"] == "杂草" or kind in WEED_TYPE:
        return "易生杂草"
    if kind == "虫害" or (not kind and any(k in tgt["name"] for k in PEST_KW)):
        return "易感虫害"
    return "易感病害"


def read_orig():
    """读取项目原有三元组（保留全部有效行）"""
    rows, bad = [], 0
    if not os.path.exists(ORIG_TRIPLETS):
        print("WARN: 找不到原始三元组", ORIG_TRIPLETS)
        return rows
    with io.open(ORIG_TRIPLETS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            s = (r.get("实体1") or "").strip()
            rel = (r.get("关系") or "").strip()
            t = (r.get("实体2") or "").strip()
            if s and rel and t and rel != "关系":
                rows.append((s, rel, t))
            else:
                bad += 1
    print("原有三元组：%d 条（忽略脏行 %d 条）" % (len(rows), bad))
    return rows


def read_ext():
    """读取扩展图谱 -> (triples, nodes)"""
    nodes = {}
    with io.open(os.path.join(EXT, "nodes.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            src_label = (r.get("节点类型") or "").strip()
            try:
                props = json.loads(r.get("属性(JSON)") or "{}")
            except Exception:
                props = {}
            nodes[r["节点ID"]] = {
                "name": (r.get("节点名称") or "").strip(),
                "label": refine_label(src_label, props),
                "src_label": src_label,
                "props": props,
            }

    triples, skipped = [], 0
    with io.open(os.path.join(EXT, "edges.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            s, t = nodes.get(r["起点ID"]), nodes.get(r["终点ID"])
            if not s or not t:
                skipped += 1
                continue
            rel = (r.get("关系类型") or "").strip()
            rel = map_pest_rel(t) if rel == "易发病虫害" else REL_MAP.get(rel, rel)
            triples.append((s["name"], rel, t["name"]))
    n_weed = len([1 for n in nodes.values() if n["label"] == "杂草"])
    n_ctrl = len([1 for n in nodes.values() if n["label"] == "防治措施"])
    print("扩展图谱三元组：%d 条（跳过 %d 条悬挂边）" % (len(triples), skipped))
    print("  标签精修：杂草 %d 个、非药剂类防治措施 %d 个" % (n_weed, n_ctrl))
    return triples, nodes


def main():
    if not os.path.exists(EXT):
        print("找不到扩展数据目录：", EXT)
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)

    orig = read_orig()
    ext, nodes = read_ext()

    # ---- 融合去重（原有数据全部保留在前，新增行按 (s,rel,t) 去重）
    seen, merged, dup, fresh = set(), [], 0, 0
    for tri in orig:
        if tri in seen:
            dup += 1
            continue
        seen.add(tri)
        merged.append(tri)
    for tri in ext:
        if tri in seen:
            dup += 1
            continue
        seen.add(tri)
        merged.append(tri)
        fresh += 1

    # ---- 写融合三元组
    trip_path = os.path.join(OUT, "clean_triplets_merged.csv")
    with io.open(trip_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["实体1", "关系", "实体2"])
        w.writerows(merged)

    # ---- 写实体属性表
    props_path = os.path.join(OUT, "entity_props.csv")
    with io.open(props_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["实体名称", "标签", "属性JSON"])
        for n in nodes.values():
            if n["name"] and n["props"]:
                w.writerow([n["name"], n["label"],
                            json.dumps(n["props"], ensure_ascii=False)])

    # ---- 统计报告
    rel_cnt = Counter(t[1] for t in merged)
    entities = set(t[0] for t in merged) | set(t[2] for t in merged)
    new_entities = set(t[0] for t in ext) | set(t[2] for t in ext)
    lines = [
        "知识图谱融合报告",
        "=" * 46,
        "原有三元组      : %d" % len(orig),
        "扩展图谱三元组  : %d" % len(ext),
        "完全重复去重    : %d" % dup,
        "融合后总三元组  : %d（其中新增 %d 条）" % (len(merged), fresh),
        "融合后实体总数  : %d（扩展图谱引入 %d 个）" % (len(entities), len(new_entities)),
        "带属性实体      : %d" % len([1 for n in nodes.values() if n["props"]]),
        "",
        "关系分布（融合后）：",
    ]
    for r, c in rel_cnt.most_common():
        tag = "  <兼容原生>" if r in ("主产区", "适宜土壤", "适宜气候", "适配肥料",
                                 "易感病害", "易感虫害", "防治方法", "栽培要点") else ""
        lines.append("  %-10s %5d%s" % (r, c, tag))
    rep = "\n".join(lines)
    with io.open(os.path.join(OUT, "merge_report.txt"), "w", encoding="utf-8") as f:
        f.write(rep + "\n")

    print("-" * 46)
    print(rep.encode("utf-8").decode("utf-8"))


if __name__ == "__main__":
    main()
