#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
08 - 将融合后的知识图谱导入 Neo4j（幂等增量，默认不清库）

用法：
    python scripts/08_import_extended_kg.py                # 增量导入（推荐）
    python scripts/08_import_extended_kg.py --rebuild      # 清空数据库后重建
    python scripts/08_import_extended_kg.py --dry-run      # 只统计，不写库

要点：
1. 沿用项目现有的 (n)-[r:REL {type:'关系名'}]->(m) 模型 ── 已有查询/页面零改动即可命中新数据
2. MERGE 幂等：同名实体自动融合，不会重复造点；已有节点只追加标签，不删旧标签
3. 实体属性写进节点属性（pH 区间、生育期天数、需水量…），可直接做数值推理与方案生成
"""
import csv
import io
import json
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output")
TRIPLETS = os.path.join(OUT, "clean_triplets_merged.csv")
PROPS = os.path.join(OUT, "entity_props.csv")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PWD = "123456"

# 关系 -> (起点标签, 终点标签)，用于没有属性表兜底 的老实体推断类型
REL_LABEL = {
    "主产区": ("作物", "地域"),
    "适宜土壤": ("作物", "土壤"),
    "适宜气候": ("作物", "气候"),
    "适配肥料": ("作物", "农药肥料"),
    "易感病害": ("作物", "病虫害"),
    "易感虫害": ("作物", "病虫害"),
    "易生杂草": ("作物", "杂草"),
    "防治方法": ("病虫害", "农药肥料"),
    "栽培要点": ("作物", "栽培要点"),
    "生育期": ("作物", "生育期"),
    "需要养分": ("作物", "养分元素"),
    "含有养分": ("农药肥料", "养分元素"),
    "适宜轮作": ("作物", "作物"),
    "收获季节": ("作物", "季节"),
    "播种季节": ("作物", "季节"),
    "属于类别": ("作物", "作物类别"),
    "品种属于": ("品种", "作物"),
    "主推品种": ("作物", "品种"),
    "主要土壤": ("地域", "土壤"),
    "属于气候区": ("地域", "气候"),
    "适用作物": ("农药肥料", "作物"),
    "常用改良剂": ("土壤", "农药肥料"),
}

ALL_LABELS = sorted({l for pair in REL_LABEL.values() for l in pair} |
                    {"作物", "病虫害", "地域", "农药肥料", "栽培要点", "土壤", "气候",
                     "生育期", "品种", "作物类别", "养分元素", "季节", "杂草", "防治措施"})


def q(label):
    """标签加反引号，兼容中文标签"""
    return "`%s`" % label


def load_triples():
    with io.open(TRIPLETS, encoding="utf-8-sig") as f:
        rows = [(r["实体1"].strip(), r["关系"].strip(), r["实体2"].strip())
                for r in csv.DictReader(f)]
    return [t for t in rows if all(t)]


def load_props():
    """名称 -> (标签, 属性dict)"""
    m = {}
    if not os.path.exists(PROPS):
        return m
    with io.open(PROPS, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                props = json.loads(r["属性JSON"] or "{}")
            except Exception:
                props = {}
            m[r["实体名称"].strip()] = (r["标签"].strip(), props)
    return m


def stat(triples):
    from collections import Counter
    c = Counter(t[1] for t in triples)
    ents = {t[0] for t in triples} | {t[2] for t in triples}
    print("待导入：%d 条三元组 / %d 个实体" % (len(triples), len(ents)))
    for r, n in c.most_common():
        print("   %-10s %5d" % (r, n))


def import_to_neo4j(triples, props, rebuild=False):
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PWD))
    driver.verify_connectivity()
    print("Neo4j 已连接：", NEO4J_URI)

    with driver.session() as session:
        if rebuild:
            session.run("MATCH (n) DETACH DELETE n")
            print("已清空旧数据")

        for label in ALL_LABELS:
            session.run("CREATE INDEX IF NOT EXISTS FOR (n:%s) ON (n.name)" % q(label))
        print("索引就绪（%d 个标签）" % len(ALL_LABELS))

        # ---------- 1. 写节点属性并打标签 ----------
        by_label = defaultdict(list)
        for name, (label, p) in props.items():
            if name:
                by_label[label].append({"name": name, "props": p})
        for label, items in by_label.items():
            for i in range(0, len(items), 300):
                batch = items[i:i + 300]
                session.run(
                    "UNWIND $batch AS it MERGE (n {name:it.name}) "
                    "SET n:%s SET n += it.props" % q(label),
                    batch=batch)
        print("节点属性写入完成：%d 个实体" % sum(len(v) for v in by_label.values()))

        # ---------- 2. 写关系（按 起点标签/终点标签 分组批量）----------
        groups = defaultdict(list)
        for s, rel, t in triples:
            sl = props.get(s, (None,))[0]
            tl = props.get(t, (None,))[0]
            dflt = REL_LABEL.get(rel, ("Entity", "Entity"))
            sl = sl or dflt[0]
            tl = tl or dflt[1]
            groups[(sl, rel, tl)].append({"s": s, "t": t})

        total_rel = 0
        for (sl, rel, tl), items in sorted(groups.items()):
            cy = ("UNWIND $batch AS it "
                  "MERGE (s {name:it.s}) SET s:%s "
                  "MERGE (t {name:it.t}) SET t:%s "
                  "MERGE (s)-[r:REL {type:$rel}]->(t)" % (q(sl), q(tl)))
            for i in range(0, len(items), 400):
                batch = items[i:i + 400]
                session.run(cy, batch=batch, rel=rel)
                total_rel += len(batch)
        print("关系写入完成：%d 条" % total_rel)

        n_count = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        r_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print("-" * 46)
        print("库中现有：%d 节点 / %d 关系" % (n_count, r_count))
        labels = [rec["l"] for rec in session.run("CALL db.labels() YIELD label AS l RETURN l")]
        print("标签：", "、".join(labels))

    driver.close()


def main():
    args = set(sys.argv[1:])
    if not os.path.exists(TRIPLETS):
        print("缺少 %s，请先运行 07_merge_extended_kg.py" % TRIPLETS)
        sys.exit(1)

    triples = load_triples()
    props = load_props()
    stat(triples)

    if "--dry-run" in args:
        print("\n[dry-run] 未写入数据库")
        return

    print("\n目标库：%s（%s）" % (NEO4J_URI, "清空重建" if "--rebuild" in args else "增量合并"))
    try:
        import_to_neo4j(triples, props, rebuild="--rebuild" in args)
    except Exception as e:
        print("导入失败：", e)
        sys.exit(1)
    print("导入完成")


if __name__ == "__main__":
    main()
