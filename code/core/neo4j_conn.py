#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from neo4j import GraphDatabase

class Neo4jConnection:
    def __init__(self, uri, user=None, password=None, auth=None):
        self.driver = None
        if auth:
            user, password = auth
        try:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            self.driver.verify_connectivity()
            print("INFO: Neo4j 连接成功")
        except Exception as e:
            print(f"ERROR: Neo4j 连接失败: {e}")
            raise

    def close(self):
        if self.driver:
            self.driver.close()
            print("INFO: Neo4j 连接已关闭")

    def get_all_crops(self):
        """获取所有有效作物"""
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (n:作物)
                    WHERE n.name IS NOT NULL AND NOT (n.name CONTAINS '病' OR n.name CONTAINS '虫')
                    RETURN n.name AS name ORDER BY n.name
                """)
                return [record["name"] for record in result if record["name"]]
        except Exception as e:
            print(f"ERROR: 获取作物列表失败: {e}")
            return []

    def get_crop_kg(self, crop_name):
        """查询作物所有关系"""
        if not crop_name:
            return []
        try:
            with self.driver.session() as session:
                result = session.run("""
                    MATCH (s {name: $crop})-[r:REL]->(t)
                    RETURN s.name AS source, r.type AS relation, t.name AS target, labels(t)[0] AS target_label
                """, crop=crop_name)
                return [{"crop": r["source"], "relation_type": r["relation"], "node": r["target"], "node_label": r["target_label"]} for r in result]
        except Exception as e:
            print(f"ERROR: 获取 {crop_name} 图谱失败: {e}")
            return []

    def query_triple(self, source, relation=None, target_label=None):
        """通用三元组查询"""
        try:
            with self.driver.session() as session:
                cypher = "MATCH (s {name: $source})-[r:REL]->(t)"
                params = {"source": source}
                if relation:
                    cypher += " WHERE r.type = $relation"
                    params["relation"] = relation
                if target_label:
                    cypher += " AND labels(t)[0] = $target_label" if relation else " WHERE labels(t)[0] = $target_label"
                    params["target_label"] = target_label
                cypher += " RETURN t.name as target"
                return [r["target"] for r in session.run(cypher,** params) if r["target"]]
        except Exception as e:
            print(f"ERROR: 三元组查询失败: {source}-{relation}-{target_label} : {e}")
            return []

    # ==================================================================
    # 以下为知识图谱扩展能力（2026-09 新增，不改动上方任何原有逻辑）
    # 数据来源：data/agri_kg_ext（669 节点 / 3440 关系）融合进原三元组后共
    #           1320 节点 / 5700 关系，13 类实体、21 类关系
    # ==================================================================

    # 关系 -> (起点标签, 终点标签)
    REL_LABELS = {
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

    def _run(self, cypher, **params):
        with self.driver.session() as session:
            return [dict(r) for r in session.run(cypher, **params)]

    def get_entity_profile(self, name):
        """单个实体的完整画像：标签、属性、按关系分组的关联实体"""
        if not name:
            return {}
        try:
            rows = self._run(
                "MATCH (n {name:$name}) RETURN labels(n) AS labels, properties(n) AS props",
                name=name)
            if not rows:
                return {}
            node = rows[0]
            out = self._run("""
                MATCH (n {name:$name})-[r:REL]->(t)
                RETURN r.type AS rel, t.name AS name, labels(t)[0] AS label
            """, name=name)
            inn = self._run("""
                MATCH (s)-[r:REL]->(n {name:$name})
                RETURN r.type AS rel, s.name AS name, labels(s)[0] AS label
            """, name=name)
            relations, incoming = {}, {}
            for r in out:
                relations.setdefault(r["rel"], []).append(
                    {"name": r["name"], "label": r["label"]})
            for r in inn:
                incoming.setdefault(r["rel"], []).append(
                    {"name": r["name"], "label": r["label"]})
            return {
                "name": name,
                "labels": node["labels"],
                "props": node["props"],
                "relations": relations,
                "incoming": incoming,
            }
        except Exception as e:
            print(f"ERROR: 实体画像失败 {name}: {e}")
            return {}

    def get_subgraph(self, name, depth=1, limit=260):
        """以实体为中心取 N 跳子图，供前端可视化（节点/边带类型与颜色分组）"""
        if not name:
            return {"nodes": [], "links": [], "center": name}
        depth = 1 if depth not in (1, 2) else depth
        try:
            rows = self._run("""
                MATCH p = (c {name:$name})-[:REL*1..%d]-(m)
                WITH c, m, length(p) AS d
                RETURN DISTINCT m.name AS name, labels(m)[0] AS label, min(d) AS depth
                ORDER BY depth, name LIMIT $limit
            """ % depth, name=name, limit=limit)
            nodes, keep = [], {name}
            nodes.append({"name": name, "label": "中心", "depth": 0})
            for r in rows:
                if r["name"] == name:
                    continue
                nodes.append({"name": r["name"], "label": r["label"], "depth": r["depth"]})
                keep.add(r["name"])
            links = []
            if nodes:
                rels = self._run("""
                    MATCH (s)-[r:REL]->(t)
                    WHERE s.name IN $names AND t.name IN $names
                    RETURN s.name AS source, t.name AS target, r.type AS type
                    LIMIT 1200
                """, names=list(keep))
                links = [{"source": x["source"], "target": x["target"], "type": x["type"]}
                         for x in rels]
            return {"nodes": nodes, "links": links, "center": name, "depth": depth}
        except Exception as e:
            print(f"ERROR: 子图查询失败 {name}: {e}")
            return {"nodes": [], "links": [], "center": name}

    def search_entities(self, keyword, limit=20):
        """实体模糊检索（名称包含），按度数排序，供检索框联想"""
        if not keyword:
            return []
        try:
            return self._run("""
                MATCH (n)
                WHERE n.name CONTAINS $kw
                OPTIONAL MATCH (n)-[r:REL]-()
                WITH n, count(r) AS deg
                RETURN n.name AS name, labels(n)[0] AS label, deg
                ORDER BY (CASE WHEN n.name = $kw THEN 0 ELSE 1 END), deg DESC, size(n.name)
                LIMIT $limit
            """, kw=keyword, limit=limit)
        except Exception as e:
            print(f"ERROR: 实体检索失败 {keyword}: {e}")
            return []

    def get_crop_full(self, crop):
        """作物全部关系（不限于原来的 8 类），返回 {关系: [实体]}"""
        if not crop:
            return {}
        try:
            rows = self._run("""
                MATCH (c {name:$crop})-[r:REL]->(t)
                RETURN r.type AS rel, collect(DISTINCT t.name) AS items
            """, crop=crop)
            return {r["rel"]: r["items"] for r in rows}
        except Exception as e:
            print(f"ERROR: 作物全关系失败 {crop}: {e}")
            return {}

    def get_crop_timeline(self, crop):
        """生育期时间轴 + 各阶段对应的关键农事操作"""
        if not crop:
            return []
        try:
            rows = self._run("""
                MATCH (c {name:$crop})-[:REL {type:'生育期'}]->(s:生育期)
                OPTIONAL MATCH (s)-[:REL]->(p)
                RETURN s.name AS stage, s.`阶段序号` AS idx, s.`历时天数` AS days,
                       s.`起始日` AS start, s.`结束日` AS end,
                       collect(DISTINCT p.name) AS practices
                ORDER BY idx
            """, crop=crop)
            return rows
        except Exception as e:
            print(f"ERROR: 生育期查询失败 {crop}: {e}")
            return []

    def get_fertilizer_plan(self, crop):
        """水肥调控处方素材：生育期 + 推荐肥料 + 所需养分 + 肥料养分含量与用法"""
        if not crop:
            return {}
        try:
            ferts = self._run("""
                MATCH (c {name:$crop})-[:REL {type:'适配肥料'}]->(f:农药肥料)
                OPTIONAL MATCH (f)-[:REL {type:'含有养分'}]->(n:养分元素)
                RETURN f.name AS name, f.`肥料类型` AS type, f.`主要养分含量` AS nutrient,
                       f.`施用方式` AS method, f.`特点` AS note,
                       collect(DISTINCT n.name) AS contains
            """, crop=crop)
            nutri = self._run("""
                MATCH (c {name:$crop})-[:REL {type:'需要养分'}]->(n:养分元素)
                RETURN n.name AS name, n.`生理功能` AS fn
            """, crop=crop)
            stages = self._run("""
                MATCH (c {name:$crop})-[:REL {type:'生育期'}]->(s:生育期)
                RETURN s.name AS stage, s.`阶段序号` AS idx
                ORDER BY idx
            """, crop=crop)
            return {"fertilizers": ferts, "nutrients": nutri, "stages": stages}
        except Exception as e:
            print(f"ERROR: 水肥处方查询失败 {crop}: {e}")
            return {}

    def get_pest_control(self, crop, pest=None):
        """病虫害防治素材：作物 → 病虫害 → 防治措施（含安全间隔期）"""
        if not crop and not pest:
            return []
        try:
            if pest:
                return self._run("""
                    MATCH (p {name:$pest})
                    OPTIONAL MATCH (p)-[:REL {type:'防治方法'}]->(m)
                    RETURN p.name AS pest, p.`类型` AS kind, p.`危害部位` AS part,
                           p.`常发时期` AS period, p.`危害等级` AS level,
                           collect(DISTINCT m.name) AS measures
                """, pest=pest)
            return self._run("""
                MATCH (c {name:$crop})-[:REL]->(p:病虫害)
                OPTIONAL MATCH (p)-[:REL {type:'防治方法'}]->(m)
                RETURN p.name AS pest, p.`类型` AS kind, p.`危害部位` AS part,
                       p.`常发时期` AS period, p.`危害等级` AS level,
                       collect(DISTINCT m.name) AS measures
                ORDER BY p.name
            """, crop=crop)
        except Exception as e:
            print(f"ERROR: 防治查询失败: {e}")
            return []

    def suggest_crops(self, ph=None, temp=None, rain=None, region=None,
                      soil=None, season=None, limit=10):
        """条件筛选：按 pH / 气温 / 降水 / 产区 / 土壤 / 播种季节 推荐适种作物"""
        try:
            cypher = """
                MATCH (c:作物)
                WHERE ($ph  IS NULL OR (c.`适宜pH下限`   <= $ph  AND c.`适宜pH上限`   >= $ph))
                  AND ($tem IS NULL OR (c.`适宜温度下限` <= $tem AND c.`适宜温度上限` >= $tem))
                  AND ($rai IS NULL OR (c.`需降水量下限` <= $rai AND c.`需降水量上限` >= $rai))
                  AND ($sea IS NULL OR c.`播种季节` CONTAINS $sea)
            """
            if region:
                cypher += """
                  AND EXISTS { MATCH (c)-[:REL {type:'主产区'}]->(r {name:$reg}) }
                """
            if soil:
                cypher += """
                  AND EXISTS { MATCH (c)-[:REL {type:'适宜土壤'}]->(s {name:$soi}) }
                """
            cypher += """
                RETURN c.name AS name, c.`类别` AS category, c.`生育期天数` AS days,
                       c.`适宜pH下限` AS ph_low, c.`适宜pH上限` AS ph_high,
                       c.`适宜温度下限` AS t_low, c.`适宜温度上限` AS t_high,
                       c.`需水等级` AS water, c.`平均亩产kg` AS yield_kg
                ORDER BY (c.`生育期天数` IS NULL), c.`生育期天数` LIMIT $limit
            """
            return self._run(cypher, ph=ph, tem=temp, rai=rain,
                             sea=season, reg=region, soi=soil, limit=limit)
        except Exception as e:
            print(f"ERROR: 条件筛选失败: {e}")
            return []

    def get_region_plan(self, region):
        """生产规划素材：产区 → 优势作物、土壤、气候、熟制"""
        if not region:
            return {}
        try:
            rows = self._run("""
                MATCH (r {name:$region})
                OPTIONAL MATCH (r)-[:REL {type:'主要土壤'}]->(s:土壤)
                OPTIONAL MATCH (r)-[:REL {type:'属于气候区'}]->(cl:气候)
                RETURN r.name AS name, properties(r) AS props,
                       collect(DISTINCT s.name) AS soils,
                       collect(DISTINCT cl.name) AS climates
            """, region=region)
            crops = self._run("""
                MATCH (c:作物)-[:REL {type:'主产区'}]->(r {name:$region})
                RETURN c.name AS name, c.`类别` AS category,
                       c.`生育期天数` AS days, c.`平均亩产kg` AS yield_kg
                ORDER BY (c.`平均亩产kg` IS NULL), c.`平均亩产kg` DESC LIMIT 30
            """, region=region)
            base = rows[0] if rows else {}
            base["crops"] = crops
            return base
        except Exception as e:
            print(f"ERROR: 产区规划失败 {region}: {e}")
            return {}

    def graph_stats(self):
        """图谱规模统计，用于首页/大屏展示"""
        try:
            n = self._run("MATCH (n) RETURN count(n) AS c")[0]["c"]
            r = self._run("MATCH ()-[r:REL]->() RETURN count(r) AS c")[0]["c"]
            labels = self._run("""
                MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC
            """)
            rels = self._run("""
                MATCH ()-[r:REL]->() RETURN r.type AS rel, count(*) AS c ORDER BY c DESC
            """)
            return {"nodes": n, "relations": r, "labels": labels, "rel_types": rels}
        except Exception as e:
            print(f"ERROR: 统计失败: {e}")
            return {"nodes": 0, "relations": 0, "labels": [], "rel_types": []}
