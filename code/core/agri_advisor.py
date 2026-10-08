#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
农业智能决策顾问（AgriAdvisor）
================================
以「知识图谱 + 检索增强 + 大语言模型」为核心的处方生成引擎，面向
农业生产、营销、管理和服务四类场景，输出四类可落地成果：

    1. 病虫害防治建议   pest        —— 症状识别 → 农业/物理/生物/化学防治顺序 → 安全间隔期
    2. 水肥调控处方     fertilizer  —— 生育期 × 推荐肥料 × 养分需求 → 基肥/追肥配比建议
    3. 农事管理方案     farm        —— 生育期时间轴 + 各阶段关键农事操作
    4. 生产规划报告     planning    —— 产区/土壤/气候/温光水条件 → 适种作物组合与茬口

设计原则：
    · 所有事实来自知识图谱检索结果，大模型只做「组织语言」，不做「补充事实」
    · 每条结论都带 sources（图谱三元组），便于溯源与人工复核
    · 大模型未配置时自动降级为「图谱结构化处方」，功能可用、不报错
      （当前默认接入豆包·火山方舟，见 config.py / config_local.py）
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.neo4j_conn import Neo4jConnection          # noqa: E402
from core import llm_client                          # noqa: E402
from core import answer_filler                       # noqa: E402
import config                                        # noqa: E402

# ------------------------------------------------------------------ 意图词典
INTENT_KW = {
    "pest": ["防治", "病害", "虫害", "打药", "用药", "农药", "喷", "发黄", "枯萎", "烂",
             "斑", "霉", "疫病", "虫", "杂草", "除草", "死苗", "卷叶", "病"],
    "fertilizer": ["施肥", "肥料", "追肥", "基肥", "氮肥", "磷肥", "钾肥", "复合肥",
                   "养分", "配方", "水肥", "灌溉", "浇水", "缺素", "叶面肥", "有机肥"],
    "farm": ["什么时候种", "什么时候播", "怎么种", "种植", "栽培", "管理", "农事",
             "生育期", "播期", "收获", "整地", "定植", "育苗", "播种期", "田间管理"],
    "planning": ["适合种", "能种什么", "推荐种植", "规划", "产业", "布局", "茬口",
                 "轮作", "适不适合", "种什么好", "什么作物", "结构调整"],
}

PLAN_TITLE = {
    "pest": "病虫害防治建议",
    "fertilizer": "水肥调控处方",
    "farm": "农事管理方案",
    "planning": "生产规划报告",
    "general": "农业知识问答",
}


class AgriAdvisor:
    def __init__(self, neo4j_uri=None, neo4j_user=None, neo4j_pwd=None):
        self.conn = Neo4jConnection(neo4j_uri or config.NEO4J_URI,
                                    neo4j_user or config.NEO4J_USER,
                                    neo4j_pwd or config.NEO4J_PWD)
        self.entities = self._load_entities()
        self.aliases = self._load_aliases()
        print("INFO: AgriAdvisor 就绪，已加载 %d 个实体、%d 个别名"
              % (len(self.entities), len(self.aliases)))
        self._local_llm = None
        self._last_followups = []
        self._local_kb = None      # 第二级证据源（本地知识库），首次用到时才建，惰性加载

    # ============================================================ 基础
    def _load_entities(self):
        """加载全部实体（名称 + 标签 + 度量），长的优先匹配，避免短名先命中"""
        rows = self.conn._run("""
            MATCH (n)
            WHERE n.name IS NOT NULL
            OPTIONAL MATCH (n)-[r:REL]-()
            WITH n, count(r) AS deg
            RETURN n.name AS name, labels(n)[0] AS label, deg
            ORDER BY size(n.name) DESC
        """)
        return [(r["name"], r["label"], r["deg"]) for r in rows]

    def _load_aliases(self):
        """
        别名 → (正式名, 标签)。

        图谱里 52 种作物带 `别名` 属性（土豆→马铃薯、西红柿→番茄、苞谷→玉米…），
        农户提问时说的多半是土叫法，只按正式名匹配会一条都命中不了。
        这里把别名也接进实体识别，答不上来的情况会明显变少。
        """
        try:
            rows = self.conn._run("""
                MATCH (n) WHERE n.别名 IS NOT NULL
                RETURN n.name AS name, labels(n)[0] AS label, n.别名 AS alias
            """)
        except Exception as e:
            print("WARN: 别名加载失败（不影响其它功能）：%s" % e)
            return {}
        out = {}
        try:
            for r in rows:
                for a in str(r["alias"] or "").split("|"):
                    a = re.sub(r"[（(].*?[)）]", "", a).strip()
                    # 单字别名（棉、蒜、姜…）太容易误伤，只收 2 字以上的
                    if len(a) >= 2 and a != r["name"] and a not in out:
                        out[a] = (r["name"], r["label"])
        except Exception as e:
            print("WARN: 别名解析失败（不影响其它功能）：%s" % e)
        return out

    def close(self):
        self.conn.close()

    # ============================================================ 实体 / 意图
    def extract(self, q):
        """从自然语言中抽取作物、病虫害、地域、土壤、季节与数值条件"""
        found = {"crop": None, "pest": None, "disease": None, "weed": None,
                 "region": None, "soil": None, "climate": None, "fertilizer": None,
                 "stage": None, "season": None}
        label2key = {"作物": "crop", "杂草": "weed",
                     "地域": "region", "土壤": "soil", "气候": "climate",
                     "农药肥料": "fertilizer", "生育期": "stage", "季节": "season"}
        pest_hits = []          # 已命中的病虫害名（用于去重，见下）
        for name, label, _deg in self.entities:
            if name and name in q:
                if label == "病虫害":
                    # 图谱里「锈病」和「小麦条锈病」是两个节点、说的是同一种病。
                    # entities 按名字长度倒序，所以先命中的更具体，短的那个丢掉。
                    if any(name in h or h in name for h in pest_hits):
                        continue
                    pest_hits.append(name)
                    # 「小麦红蜘蛛」这种名字不带"虫"字，不能只看关键字
                    if "病" in name and not found["disease"]:
                        found["disease"] = name
                    elif not found["pest"]:
                        found["pest"] = name
                    continue
                key = label2key.get(label)
                if key and not found[key]:
                    found[key] = name
        # 别名兜底：农户多说土叫法（土豆、西红柿、苞谷），别名也认
        for alias in sorted(self.aliases, key=len, reverse=True):
            if alias in q:
                real, label = self.aliases[alias]
                if label == "作物" and not found["crop"]:
                    found["crop"] = real
                elif label == "农药肥料" and not found["fertilizer"]:
                    found["fertilizer"] = real
        # 生育期节点名形如「小麦-拔节期」，问句里常见简称
        if not found["stage"] and found["crop"]:
            for name, label, _deg in self.entities:
                if label == "生育期" and name.startswith(found["crop"] + "-"):
                    short = name.split("-", 1)[1]
                    if short in q:
                        found["stage"] = name
                        break
        nums = self._extract_numbers(q)
        found.update(nums)
        return found

    @staticmethod
    def _extract_numbers(q):
        """抽取 pH / 气温 / 降水 等可计算条件"""
        out = {"ph": None, "temp": None, "rain": None}
        m = re.search(r"pH\s*[值]?\s*[:：]?\s*(\d+(?:\.\d+)?)", q, re.I)
        if not m:
            m = re.search(r"酸碱度\s*[:：]?\s*(\d+(?:\.\d+)?)", q)
        if m:
            out["ph"] = float(m.group(1))
        m = re.search(r"(?:均温|气温|温度|积温)?\s*[:：]?\s*(-?\d+(?:\.\d+)?)\s*[℃°]", q)
        if not m:
            m = re.search(r"温度\s*[:：]?\s*(-?\d+(?:\.\d+)?)", q)
        if m:
            out["temp"] = float(m.group(1))
        m = re.search(r"(?:降水|降雨|雨量)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?", q)
        if m:
            out["rain"] = float(m.group(1))
        m = re.search(r"年平均气温\s*[:：]?\s*(-?\d+(?:\.\d+)?)", q)
        if m and out["temp"] is None:
            out["temp"] = float(m.group(1))
        return out

    def detect_intent(self, q):
        """关键词打分选意图，避免"种"字把防治问题误判为种植问题"""
        score = {k: 0 for k in INTENT_KW}
        for intent, kws in INTENT_KW.items():
            for kw in kws:
                if kw in q:
                    score[intent] += 1 if len(kw) <= 2 else 2
        ent = self.extract(q)
        # 有病虫害实体 + 问防治 → 强指向 pest
        if ent["pest"] or ent["disease"] or ent["weed"]:
            score["pest"] += 2
        if ent["stage"]:
            score["farm"] += 1
            score["fertilizer"] += 1
        if ent["ph"] is not None or ent["temp"] is not None:
            score["planning"] += 2
        if ent["region"] and not ent["crop"]:
            score["planning"] += 2
        best = max(score, key=lambda k: score[k])
        return best if score[best] > 0 else "general"

    # ============================================================ 四类处方
    def pest_advice(self, crop=None, pest=None):
        """
        病虫害防治建议：先辨病/虫，再按 农业→物理→生物→化学 排序给药。
        没点名具体病/虫时，按危害等级从高到低挑前 MAX_PEST_ITEMS 个展开，
        避免一次把整份清单倒给大模型（既不聚焦，也拖长响应时间）。
        """
        MAX_PEST_ITEMS = 8
        sources, blocks = [], []
        rows = self.conn.get_pest_control(crop=crop, pest=pest)
        if not rows:
            return None, []
        total_count = len(rows)
        if not pest and len(rows) > MAX_PEST_ITEMS:
            rank = {"高": 0, "较高": 1, "中": 2, "中等": 2, "低": 3, "较低": 3}
            rows = sorted(rows, key=lambda r: (rank.get(r.get("level") or "", 5),
                                               r.get("pest") or ""))
        for r in rows[:MAX_PEST_ITEMS if not pest else len(rows)]:
            name = r["pest"]
            kind = r.get("kind") or ""
            part = r.get("part") or ""
            period = r.get("period") or ""
            level = r.get("level") or ""
            measures = [m for m in (r.get("measures") or []) if m]
            if not measures:
                continue
            # 按 绿色防控优先 排序：农业防治 → 生物防治 → 物理防治 → 生物农药 → 化学农药
            order = {"农业防治": 0, "生物防治": 1, "物理防治": 2,
                     "生物农药": 3, "化学农药": 4}
            # 注意属性名是「安全间隔期天」（原写成"安全间隔期天数"，取不到值，
            # 所以以前处方里从来没有出现间隔期）
            mat = self.conn._run("""
                MATCH (p {name:$p})-[:REL {type:'防治方法'}]->(m)
                RETURN m.name AS name, m.`措施类型` AS kind,
                       m.`常见剂型` AS form, m.`作用方式` AS mode,
                       m.`安全间隔期天` AS interval, m.`使用要点` AS tip
            """, p=name)
            mat.sort(key=lambda x: order.get(x.get("kind") or "综合措施", 5))
            lines = ["### %s%s%s" % (name, ("（%s）" % kind) if kind else "",
                                    ("　危害等级：%s" % level) if level else "")]
            meta = []
            if part:
                meta.append("危害部位：%s" % part)
            if period:
                meta.append("常发时期：%s" % period)
            if meta:
                lines.append("　" + "；".join(meta))
            for m in mat:
                tag = m.get("kind") or "综合措施"
                seg = "- **%s**（%s）" % (m["name"], tag)
                if m.get("form"):
                    seg += "　常见剂型：%s" % m["form"]
                if m.get("mode"):
                    seg += "　作用方式：%s" % m["mode"]
                if m.get("tip"):
                    seg += "　%s" % m["tip"]
                if m.get("interval") not in (None, "", 0, "0", "-"):
                    seg += "　安全间隔期 %s 天" % m["interval"]
                lines.append(seg)
                sources.append("%s -[防治方法]-> %s（%s）" % (name, m["name"], tag))
            blocks.append("\n".join(lines))
        if not blocks:
            return None, []
        head = "## %s病虫害防治建议" % (crop or "")
        note = ("> 用药原则：农业防治、生物防治优先，化学农药作为最后手段；"
                "严格执行安全间隔期，采收前禁用期不得施药。")
        if total_count > len(blocks):
            note += ("\n> 图谱中「%s」共记录 %d 种病虫害，此处按危害等级列出前 %d 种；"
                     "想细看某一种，可直接问「%s条锈病怎么防治」这类问题。"
                     % (crop or "该作物", total_count, len(blocks), crop or ""))
        return head + "\n" + note + "\n\n" + "\n\n".join(blocks), sources

    def fertilizer_rx(self, crop, stage=None):
        """水肥调控处方：生育期 × 推荐肥料 × 养分需求"""
        if not crop:
            return None, []
        d = self.conn.get_fertilizer_plan(crop)
        ferts = d.get("fertilizers") or []
        nutris = d.get("nutrients") or []
        stages = d.get("stages") or []
        if not ferts and not nutris:
            return None, []
        sources = []

        def ftype(f):
            return f.get("type") or "肥料"

        base = [f for f in ferts if f.get("method") and "基肥" in str(f["method"])]
        top = [f for f in ferts if f.get("method") and "追肥" in str(f["method"])]
        foliar = [f for f in ferts if f.get("method") and "叶面" in str(f["method"])]

        lines = ["## %s水肥调控处方" % crop]
        if stage:
            lines.append("> 当前生育期：**%s**，施肥方案按该阶段需肥特点调整。" % stage)
        lines.append("")
        lines.append("### 一、需肥结构")
        if nutris:
            lines.append("该作物明确需要的养分元素：" +
                         "、".join("%s（%s）" % (n["name"], (n.get("fn") or "")[:18]) for n in nutris))
            for n in nutris:
                sources.append("%s -[需要养分]-> %s" % (crop, n["name"]))
        lines.append("")
        lines.append("### 二、推荐肥料")
        for f in ferts[:12]:
            seg = "- **%s**（%s）" % (f["name"], ftype(f))
            if f.get("nutrient"):
                seg += "　养分含量：%s" % f["nutrient"]
            if f.get("method"):
                seg += "　施用方式：%s" % f["method"]
            if f.get("note"):
                seg += "　%s" % f["note"]
            lines.append(seg)
            sources.append("%s -[适配肥料]-> %s" % (crop, f["name"]))
        lines.append("")
        lines.append("### 三、分阶段施用建议")
        if base:
            lines.append("1. **基肥（播前/定植前）**：" + "、".join(f["name"] for f in base[:5]) +
                         "　结合整地一次施入，占全季氮肥约 40%~50%。")
        if top:
            lines.append("2. **追肥（关键生育期）**：" + "、".join(f["name"] for f in top[:5]) +
                         "　分 2~3 次随水或开沟施用，旺长田块适当控氮。")
        if foliar:
            lines.append("3. **叶面喷施（中后期）**：" + "、".join(f["name"] for f in foliar[:5]) +
                         "　上午 10 点前或下午 4 点后喷施，避开高温与花期。")
        if stages:
            lines.append("")
            lines.append("生育期节点：" + " → ".join(s["stage"].split("-")[-1] for s in stages))
        lines.append("")
        lines.append("> 具体用量请以当地测土配方施肥建议卡为准；"
                     "沙质土少量多次，黏质土可适当集中。")
        return "\n".join(lines), sources

    def farm_plan(self, crop, season=None):
        """农事管理方案：生育期时间轴 + 每阶段农事操作"""
        if not crop:
            return None, []
        timeline = self.conn.get_crop_timeline(crop)
        profile = self.conn.get_entity_profile(crop)
        props = (profile or {}).get("props") or {}
        if not timeline:
            return None, []
        sources = []
        lines = ["## %s农事管理方案" % crop]
        bits = []
        if props.get("生育期天数"):
            bits.append("全生育期约 %s 天" % props["生育期天数"])
        if props.get("播种季节"):
            bits.append("播种季节：%s" % props["播种季节"])
        if props.get("收获季节"):
            bits.append("收获季节：%s" % props["收获季节"])
        if props.get("需水等级"):
            bits.append("需水等级：%s" % props["需水等级"])
        if bits:
            lines.append("> " + "；".join(bits) + "。")
        lines.append("")
        lines.append("| 阶段 | 历时 | 起止（播种后天数） | 关键农事操作 |")
        lines.append("|---|---|---|---|")
        for s in timeline:
            name = s["stage"].split("-", 1)[-1]
            lines.append("| %s | %s 天 | 第 %s–%s 天 | %s |" % (
                name, s.get("days", "-"), s.get("start", "-"), s.get("end", "-"),
                "、".join(s.get("practices") or []) or "—"))
            sources.append("%s -[生育期]-> %s" % (crop, s["stage"]))
            for p in (s.get("practices") or []):
                sources.append("%s -[栽培要点]-> %s" % (s["stage"], p))
        lines.append("")
        lines.append("### 常规田间管理要点")
        practice = self.conn.get_crop_full(crop).get("栽培要点") or []
        if practice:
            sources.extend("%s -[栽培要点]-> %s" % (crop, p) for p in practice)
            for p in practice[:12]:
                info = self.conn._run(
                    "MATCH (n {name:$p}) RETURN n.`适宜时期` AS t, n.`作用` AS f", p=p)
                extra = ""
                if info and info[0].get("f"):
                    extra = "：%s" % info[0]["f"]
                if info and info[0].get("t"):
                    extra = "（%s）%s" % (info[0]["t"], extra)
                lines.append("- **%s**%s" % (p, extra))
        sources = list(dict.fromkeys(sources))
        return "\n".join(lines), sources

    def production_plan(self, region=None, ph=None, temp=None, rain=None,
                        soil=None, season=None, limit=8):
        """生产规划报告：条件筛选 + 产区基础信息 + 茬口建议"""
        sources, lines = [], []
        title = "## 生产规划报告"
        if region:
            title += " · %s" % region
        lines.append(title)
        cond = []
        if region:
            cond.append("产区：%s" % region)
        if ph is not None:
            cond.append("土壤 pH %.1f" % ph)
        if temp is not None:
            cond.append("年均温 %.1f℃" % temp)
        if rain is not None:
            cond.append("年降水 %.0f mm" % rain)
        if soil:
            cond.append("土壤类型：%s" % soil)
        if season:
            cond.append("茬口季节：%s" % season)
        if cond:
            lines.append("> 规划条件：" + "；".join(cond))
        lines.append("")

        region_d = self.conn.get_region_plan(region) if region else {}
        if region_d:
            props = region_d.get("props") or {}
            info = []
            if props.get("范围"):
                info.append("覆盖范围：%s" % props["范围"])
            if props.get("熟制"):
                info.append("熟制：%s" % props["熟制"])
            if props.get("优势作物"):
                info.append("传统优势作物：%s" % props["优势作物"])
            if region_d.get("soils"):
                info.append("主要土壤：" + "、".join([s for s in region_d["soils"] if s]))
            if region_d.get("climates"):
                info.append("气候区：" + "、".join([c for c in region_d["climates"] if c]))
            if info:
                lines.append("### 一、区域本底")
                lines.extend("- " + i for i in info)
                lines.append("")
                sources.extend(["%s -[主要土壤]-> %s" % (region, s) for s in region_d["soils"]])
                sources.extend(["%s -[属于气候区]-> %s" % (region, c) for c in region_d["climates"]])

        crops = self.conn.suggest_crops(ph=ph, temp=temp, rain=rain,
                                       region=region, soil=soil,
                                       season=season, limit=limit)
        has_region = bool(region_d and (region_d.get("props") or region_d.get("crops")))
        if crops:
            idx = "二" if has_region else "一"
            lines.append("### %s、适种作物推荐（按生育期排序）" % idx)
            lines.append("")
            lines.append("| 作物 | 类别 | 生育期 | 适宜pH | 适宜温度 | 需水 | 参考亩产 |")
            lines.append("|---|---|---|---|---|---|---|")
            for c in crops:
                lines.append("| %s | %s | %s 天 | %s–%s | %s–%s℃ | %s | %s kg |" % (
                    c["name"], c.get("category") or "-", c.get("days") or "-",
                    c.get("ph_low", "-"), c.get("ph_high", "-"),
                    c.get("t_low", "-"), c.get("t_high", "-"),
                    c.get("water") or "-", c.get("yield_kg") or "-"))
                sources.append("%s -[适宜温度/适宜pH/需水量]-> 匹配条件" % c["name"])
            lines.append("")
        # 已有产业基础（产区原有作物）
        if region_d and region_d.get("crops"):
            lines.append("### 三、本地已有种植基础")
            for c in region_d["crops"][:12]:
                lines.append("- %s（%s，生育期 %s 天）" % (
                    c["name"], c.get("category") or "-", c.get("days") or "-"))
                sources.append("%s -[主产区]-> %s" % (c["name"], region))
            lines.append("")
        if not crops and not region_d:
            return None, []
        lines.append("### 茬口与风险提示")
        lines.append("1. 优先选择与本地已有作物**不同科属**的品种进行轮作，降低连作障碍。")
        lines.append("2. 生育期天数应留出 15~20 天农耗（整地、播种、收获），避免茬口冲突。")
        lines.append("3. 需水等级高的作物在降水不足区域须配套灌溉能力后再规模种植。")
        lines.append("4. 新品种引进建议先做 1~2 季小面积试种，再确定主推品种。")
        sources = list(dict.fromkeys(sources))
        return "\n".join(lines), sources

    def general_answer(self, question, crop=None, fertilizer=None, ent=None):
        """
        通用问答（知识卡片）

        走 knowledge_builder 做多跳聚合，而不是只列关系名：
        原来输出「栽培要点：整地、播种」这种清单，农户看不出该怎么做；
        现在会带上栽培要点的「适宜时期/作用」、肥料的「养分含量/施用方式」、
        病害的「防治措施类型/剂型/安全间隔期」、生育期的各阶段农事。
        """
        ent = dict(ent or {})
        if crop:
            ent.setdefault("crop", crop)
        if fertilizer:
            ent.setdefault("fertilizer", fertilizer)
        ent = {k: v for k, v in ent.items() if v}
        if not ent:
            return None, []

        try:
            from core.knowledge_builder import KnowledgeBuilder
            pack = KnowledgeBuilder(self.conn).collect(question, ent)
        except Exception as e:
            print("WARN: 知识聚合失败，退回单实体画像：%s" % e)
            pack = {}

        if not pack.get("facts_text"):
            return None, []
        if pack.get("followups"):
            self._last_followups = pack["followups"]

        names = "、".join(t["name"] for t in pack.get("topics") or []) or \
            (crop or fertilizer or "")
        lines = ["## %s 知识卡片" % names]
        lines.append(pack["facts_text"])
        if pack.get("sources"):
            lines.append("")
            lines.append("> 以上内容全部来自知识图谱的关系数据，可逐条复核。")
        return "\n".join(lines), pack.get("sources") or []

    # ============================================================ 统一入口
    # 三级路由：graph=知识图谱命中｜local_kb=本地知识库命中｜llm=前两级都没有
    ROUTE_NAME = {"graph": "知识图谱", "local_kb": "本地知识库", "llm": "大模型生成"}
    # 第 ① 级命中门槛：图谱铺出来的事实跟问题的相关度低于它，就当图谱"没有"
    GRAPH_HIT_THRESHOLD = float(getattr(config, "GRAPH_HIT_THRESHOLD", 0.60))

    def _graph_relevance(self, question, structured):
        """
        图谱证据与问题的相关度（0~1）；**算不出来时返回 1.0 按命中处理**，
        免得嵌入模型不可用时整条链路退化。
        """
        try:
            from core import local_kb
            return local_kb.relevance(question, structured) or 1.0
        except Exception as e:
            print("WARN: 相关度计算失败（按命中处理）：%s" % e)
            return 1.0

    def _kb(self):
        """本地知识库单例（惰性加载；不可用时返回 None 并只警告一次）"""
        if self._local_kb is None:
            try:
                from core import local_kb
                self._local_kb = local_kb.get_kb()
            except Exception as e:
                print("WARN: 本地知识库不可用（将直接由大模型兜底）：%s" % e)
                self._local_kb = False
        return self._local_kb or None

    def _from_local_kb(self, question):
        """
        第 ② 级：图谱查不到时，到本地语料库做向量召回。

        只在 `_prepare` 的兜底链最后调用 —— 图谱那一串（处方 → 换角度 → 通用卡片）
        都落空了才轮到它。返回 (片段文本, sources)，没命中返回 (None, [])。
        """
        kb = self._kb()
        if not kb:
            return None, []
        try:
            hits = kb.search(question)
        except Exception as e:
            print("WARN: 本地知识库检索失败：%s" % e)
            return None, []
        if not hits:
            return None, []
        lines = ["## 本地知识库检索结果（知识图谱未命中）", "",
                 kb.format(hits)]
        sources = ["本地知识库片段#%d（相似度 %.2f）" % (i + 1, h.get("score", 0))
                   for i, h in enumerate(hits)]
        return "\n".join(lines), sources

    def _prepare(self, question, mode="auto"):
        """
        到「证据就绪」为止 —— **不含**大模型润色。

        流式（`advise_stream`）与非流式（`advise`）共用这一段，两边才不会算出两份
        不一样的溯源/实体。

        证据按三级逐级兜底：
            ① 知识图谱   → 四类处方 / 通用知识卡片（`route="graph"`）
            ② 本地知识库 → 原始语料向量召回（`route="local_kb"`）
            ③ 都没有     → `no_data=True`，交给大模型依据通用农技常识直出（`route="llm"`）

        返回：{answer, plan_type, plan_name, sources, structured, entities,
               followups, no_data, route}
        """
        question = (question or "").strip()
        if not question:
            return {"answer": "请输入问题", "plan_type": "general",
                    "plan_name": PLAN_TITLE.get("general", "农业知识问答"),
                    "sources": [], "structured": "", "entities": {},
                    "followups": [], "no_data": True, "route": "llm"}

        ent = self.extract(question)
        intent = mode if mode and mode != "auto" else self.detect_intent(question)
        if intent not in PLAN_TITLE:
            intent = "general"

        # 追问建议：不管走哪个分支都按命中的实体生成，答完就能接着问
        try:
            from core.knowledge_builder import KnowledgeBuilder
            self._last_followups = KnowledgeBuilder.followups_of(ent, asked=question)
        except Exception:
            self._last_followups = []

        structured, sources = None, []
        if intent == "pest":
            # 病虫害/杂草问到哪个就查哪个；都没点名时给出该作物全部病虫害的防治办法
            target = ent["weed"] or ent["pest"] or ent["disease"]
            structured, sources = self.pest_advice(crop=ent["crop"], pest=target)
        elif intent == "fertilizer":
            structured, sources = self.fertilizer_rx(ent["crop"], ent["stage"])
        elif intent == "farm":
            structured, sources = self.farm_plan(ent["crop"], ent["season"])
        elif intent == "planning":
            structured, sources = self.production_plan(
                region=ent["region"], ph=ent["ph"], temp=ent["temp"],
                rain=ent["rain"], soil=ent["soil"], season=ent["season"])
        else:
            structured, sources = self.general_answer(question, ent=ent)

        # 处方缺失时逐级兜底：换个角度再试一次，尽量别走到"没有数据"
        # 注意每一步都要求"有对应实体/条件"，否则会拿一堆无关作物硬凑一份报告
        if not structured and intent == "pest" and ent["crop"]:
            structured, sources = self.farm_plan(ent["crop"], ent["season"])
            if structured:
                intent = "farm"
        if not structured:
            structured, sources = self.general_answer(question, ent=ent)
        if not structured and ent["crop"]:
            structured, sources = self.farm_plan(ent["crop"], ent["season"])
            if structured:
                intent = "farm"
        if not structured and (ent["region"] or ent["ph"] is not None
                               or ent["temp"] is not None or ent["rain"] is not None):
            structured, sources = self._by_intent(question, ent, "planning")
            if structured:
                intent = "planning"
        if not structured and ent["crop"]:
            structured, sources = self.fertilizer_rx(ent["crop"], ent["stage"])
            if structured:
                intent = "fertilizer"

        # 「有东西」不等于「有答案」：图谱铺出来的事实还得跟问题相关才算命中。
        # 否则只要问句里蹭到某个实体名（"葡萄园能用无人机授粉吗" 里的"葡萄"），
        # 每张处方都能凑出两三千字，第 ②③ 级就永远触发不了。
        if structured:
            score = self._graph_relevance(question, structured)
            if score < self.GRAPH_HIT_THRESHOLD:
                print("INFO: 图谱事实与问题相关度 %.3f < %.2f，视为未命中，转本地知识库"
                      % (score, self.GRAPH_HIT_THRESHOLD))
                structured, sources = None, []

        # ② 图谱这一串都落空 → 到本地知识库做向量召回
        if not structured:
            structured, sources = self._from_local_kb(question)
            if structured:
                return {"answer": structured, "plan_type": intent,
                        "plan_name": PLAN_TITLE[intent], "sources": sources,
                        "structured": structured,
                        "entities": {k: v for k, v in ent.items() if v},
                        "followups": self._last_followups,
                        "no_data": False, "route": "local_kb"}

        # ③ 前两级都没有 → 交给大模型直出（structured 留空，别把兜底文案当素材喂进去）
        if not structured:
            fallback = ("暂未在知识图谱中找到与「%s」直接相关的数据。\n\n"
                        "可尝试这样提问：\n"
                        "- 小麦条锈病怎么防治\n"
                        "- 番茄怎么施肥\n"
                        "- 水稻的农事管理方案\n"
                        "- pH 6.5、年均温 18℃ 适合种什么"
                        % question)
            return {"answer": fallback, "plan_type": intent,
                    "plan_name": PLAN_TITLE[intent], "sources": [],
                    "structured": "",
                    "entities": {k: v for k, v in ent.items() if v},
                    "followups": self._last_followups,
                    "no_data": True, "route": "llm"}

        return {"answer": structured, "plan_type": intent,
                "plan_name": PLAN_TITLE[intent], "sources": sources,
                "structured": structured,
                "entities": {k: v for k, v in ent.items() if v},
                "followups": self._last_followups,
                "no_data": False, "route": "graph"}

    def advise(self, question, mode="auto", use_llm=True):
        """
        统一问答入口（一次性返回）
        返回：{answer, plan_type, plan_name, sources, structured, llm_used, route, route_name}

        三级路由（route）：graph 用图谱事实、local_kb 用语料片段、llm 由模型直出。
        """
        base = self._prepare(question, mode)
        base.pop("no_data", None)
        route = base.get("route") or "graph"
        base["route"] = route
        base["route_name"] = self.ROUTE_NAME.get(route, "")

        llm_used = False
        # route=="llm" 时 structured 是空的，但仍要调模型（这才是第三级的意义）；
        # 只有问题本身为空才跳过。
        if use_llm and (question or "").strip():
            refined = self._refine(question, base["plan_type"],
                                   base["structured"], route=route)
            if refined:
                # 只有第 ① 级需要补空小节（认的是「图谱暂无此项数据」这个标记）
                if route == "graph":
                    refined = answer_filler.fill(
                        refined, question, base["structured"], llm_client.chat,
                        max_tokens=self._sup_tokens(),
                        enabled=self._sup_enabled())
                base["answer"] = refined
                llm_used = True
        cfg = config.resolve_llm()
        base["llm_used"] = llm_used
        base["llm_label"] = cfg["label"] if llm_used else ""
        base["llm_model"] = cfg["model"] if llm_used else ""
        return base

    def advise_stream(self, question, mode="auto"):
        """
        **流式版** advise（生成器），给前端"边生成边显示"用。

        依次 yield：
          {"event":"meta",  plan_name / plan_type / entities / sources / followups}
              —— 图谱检索一结束就发，前端先把溯源与类型显示出来，不必干等大模型
          {"event":"delta", "text": "..."}    —— 大模型正文片段，0~N 条
          {"event":"done",  ...}              —— 与 `advise()` 返回**完全一致**的字段

        ⚠ 前端一律以 `done` 里的 `answer` 做最终渲染：中间片段可能因为断流只吐了半截，
          那种情况下后端会把答案退回成图谱原文（`llm_used=False`），由 done 一次性覆盖。
        """
        base = self._prepare(question, mode)
        base.pop("no_data", None)
        route = base.get("route") or "graph"
        base["route"] = route
        base["route_name"] = self.ROUTE_NAME.get(route, "")
        yield {"event": "meta",
               "plan_name": base.get("plan_name") or "",
               "plan_type": base.get("plan_type") or "",
               "entities": base.get("entities") or {},
               "sources": base.get("sources") or [],
               "followups": base.get("followups") or [],
               "route": route, "route_name": base["route_name"]}

        got = ""
        if (question or "").strip():
            # 三级走同一段代码，只是提示词不同：graph 给图谱事实、local_kb 给语料片段、
            # llm 不给素材让模型按通用农技常识直出
            system, user = self._refine_prompt(
                question, base["plan_type"], base["structured"], route=route)
            broke = False
            for ev in llm_client.chat_stream(system, user, max_tokens=1400):
                if ev.get("type") == "delta":
                    got += ev.get("text") or ""
                    yield {"event": "delta", "text": ev.get("text") or ""}
                elif not ev.get("ok"):
                    broke = True
            # 一个字都没吐出来（未配置 / 首帧就失败）→ 试本地小模型，再不行用原文
            if not got.strip():
                local = self._llm_local(system, user, max_tokens=400)
                if local:
                    got = local
                    yield {"event": "delta", "text": local}
            # 只吐了一两句就断流 —— 半截答案不如完整的原文
            if broke and len(got.strip()) < 40:
                got = ""

        # 图谱没覆盖到的小节 → 再发一次请求补齐，补出来的内容照样以 delta 流出去。
        # 只有第 ① 级走这条（认的是「图谱暂无此项数据」这个标记）
        if got.strip() and route == "graph":
            for ev in answer_filler.stream_supplement(
                    got, question, base["structured"], llm_client.chat_stream,
                    max_tokens=self._sup_tokens(), enabled=self._sup_enabled()):
                if ev["event"] == "delta":
                    yield {"event": "delta", "text": ev["text"]}
                else:
                    got = ev["text"]

        if got.strip():
            cfg = config.resolve_llm()
            base["answer"] = self._apply_route_note(got.strip(), route)
            base["llm_used"] = True
            base["llm_label"] = cfg["label"]
            base["llm_model"] = cfg["model"]
        else:
            base["answer"] = base["structured"] or base["answer"]
            base["llm_used"] = False
            base["llm_label"] = ""
            base["llm_model"] = ""
        yield {"event": "done", **base}

    def _by_intent(self, question, ent, intent):
        """按指定意图再试一次，用于兜底（失败返回 (None, [])）"""
        try:
            if intent == "pest":
                return self.pest_advice(crop=ent["crop"],
                                        pest=ent["weed"] or ent["pest"] or ent["disease"])
            if intent == "fertilizer":
                return self.fertilizer_rx(ent["crop"], ent["stage"])
            if intent == "farm":
                return self.farm_plan(ent["crop"], ent["season"])
            if intent == "planning":
                return self.production_plan(region=ent["region"], ph=ent["ph"],
                                            temp=ent["temp"], rain=ent["rain"],
                                            soil=ent["soil"], season=ent["season"])
        except Exception as e:
            print("WARN: 兜底意图 %s 失败：%s" % (intent, e))
        return None, []

    # ============================================================ LLM 层
    SYS_PROMPT = (
        "你是「农业智能决策顾问」，服务基层农业从业者。\n"
        "你拿到的【知识图谱事实】是**唯一可信来源**：只允许使用其中出现的事实、数字、名称和措施，"
        "严禁编造或补充图谱以外的农药、用量、标准、年份。\n"
        "写作要求：\n"
        "1) 说人话、句子短，不要书面套话；\n"
        "2) 图谱里出现的数字与专有名词（天数、pH、温度、降水、亩产、安全间隔期、药剂名称）"
        "一个都不能漏；\n"
        "3) 输出 Markdown，用二级/三级标题分段，措施用有序列表分条；\n"
        "4) 涉及防治措施时，必须按「农业防治 → 物理防治 → 生物防治 → 化学农药」的优先顺序呈现，"
        "并写清药剂名称、剂型、使用要点和安全间隔期；\n"
        "5) 结尾单独给一节「注意事项」（安全间隔期、施用时期与天气限制、轮作与连作提醒等）；\n"
        "6) 图谱确实没有的类别，写一行「图谱暂无此项数据」，不要编，也不要整段省略。"
    )

    # 第 ② 级（本地知识库片段）与第 ③ 级（模型直出）各有一套提示词 ——
    # 约束强度必须不一样，否则"图谱里没有的东西"看起来会像是图谱给的。
    SYS_LOCAL_KB = (
        "你是「农业智能决策顾问」，服务基层农业从业者。\n"
        "你拿到的【本地知识库片段】是**唯一可信来源**：这些片段来自系统本地语料库"
        "（不是知识图谱），只允许使用其中出现的事实、数字、名称和措施，"
        "严禁编造或补充片段以外的农药、用量、标准、年份。\n"
        "写作要求：\n"
        "1) 说人话、句子短，不要书面套话；\n"
        "2) 片段里出现的数字与专有名词一个都不能漏；\n"
        "3) 输出 Markdown，用二级/三级标题分段，措施用有序列表分条；\n"
        "4) 涉及防治措施时按「农业防治 → 物理防治 → 生物防治 → 化学农药」的优先顺序呈现；\n"
        "5) 结尾单独给一节「注意事项」；\n"
        "6) 片段确实没有的类别，写一行「本地知识库暂无此项数据」，不要编。"
    )

    SYS_LLM = (
        "你是「农业智能决策顾问」，服务基层农业从业者。\n"
        "本次提问在系统的**知识图谱**和**本地知识库**中都没有查到可用依据，"
        "因此请你依据**公开的通用农技常识**生成方案。\n"
        "硬性要求：\n"
        "1) 只写长期验证过的常规做法，说清什么时候做、怎么做、注意什么；\n"
        "2) **严禁编造**具体农药的推荐用量、登记证号、地方标准或年份数据 —— "
        "药剂可以给名称，用量一律写成「按产品标签说明」；\n"
        "3) 拿不准的内容不要写，宁可少一条也不硬凑；\n"
        "4) 不要写「根据图谱」「根据资料」这类客套，也不要解释为什么查不到。"
    )

    # 第 ③ 级答案开头的来源标注、第 ② 级答案结尾的来源标注
    ROUTE_HEAD_LLM = "> 本条由**大模型豆包生成**，未命中知识图谱与本地知识库。\n\n"
    ROUTE_TAIL_KB = ("\n\n---\n\n"
                     "> 本条依据**本地知识库**片段整理（知识图谱未命中），由大模型豆包输出。")

    @classmethod
    def _apply_route_note(cls, text, route):
        """按路由补一行来源标注；模型自己已经写了就不重复加"""
        text = (text or "").strip()
        if not text:
            return text
        if route == "llm" and "未命中" not in text:
            text = cls.ROUTE_HEAD_LLM + text
        elif route == "local_kb" and "本地知识库" not in text:
            text += cls.ROUTE_TAIL_KB
        return text

    # 每类成果的骨架：结构稳定了，内容才不容易漏项
    OUTLINE = {
        "pest": ("## 一句话结论\n"
                 "## 一、症状与发生规律（危害部位、常发时期、危害等级）\n"
                 "## 二、防治措施（农业/物理/生物在前、化学农药在后，逐条写清用法与安全间隔期）\n"
                 "## 三、用药与安全注意\n"
                 "## 四、依据（引用了哪些图谱关系）"),
        "fertilizer": ("## 一句话结论（该作物的需肥特点）\n"
                       "## 一、需肥结构（需要哪些养分、各起什么作用）\n"
                       "## 二、推荐肥料（名称、养分含量、施用方式）\n"
                       "## 三、分阶段施用方案（基肥 / 追肥 / 叶面肥，结合生育期）\n"
                       "## 四、注意事项"),
        "farm": ("## 一句话结论（全生育期总览）\n"
                 "## 一、生育期时间轴（阶段、历时、对应农事）\n"
                 "## 二、各阶段关键操作\n"
                 "## 三、常规田间管理要点\n"
                 "## 四、注意事项"),
        "planning": ("## 一句话结论（该条件下适合种什么）\n"
                     "## 一、区域与条件本底\n"
                     "## 二、适种作物推荐（含生育期与产量参考）\n"
                     "## 三、茬口安排与轮作建议\n"
                     "## 四、风险提示"),
        "general": ("## 一句话结论\n"
                    "## 一、基本档案（属性与适宜条件）\n"
                    "## 二、生产管理要点（栽培要点、生育期、肥料）\n"
                    "## 三、主要病虫害与防治方向\n"
                    "## 四、注意事项"),
    }

    def _refine_prompt(self, question, intent, structured, route="graph"):
        """
        组装「让大模型按骨架组织语言」的提示词。

        流式与非流式**必须共用这一份**，否则两条链路问法不同、答出来的东西也不一样。
        route 决定给模型喂什么：graph=图谱事实、local_kb=语料片段、llm=不给素材。
        返回 (system, user)。
        """
        outline = self.OUTLINE.get(intent, self.OUTLINE["general"])
        title = PLAN_TITLE.get(intent, "回答")

        if route == "local_kb":
            user = ("【农户问题】%s\n\n"
                    "【本地知识库片段（唯一可信来源，不得超出）】\n%s\n\n"
                    "请据此生成一份《%s》，按下面的骨架组织；骨架里片段确实没有的项，"
                    "写一行「本地知识库暂无此项数据」：\n%s"
                    % (question, structured, title, outline))
            return self.SYS_LOCAL_KB, user

        if route == "llm":
            user = ("【农户问题】%s\n\n"
                    "【说明】系统知识图谱与本地知识库均未查到可用依据，"
                    "请依据通用农技常识直接生成一份《%s》，按下面的骨架组织，"
                    "并在开头标注来源：\n%s"
                    % (question, title, outline))
            return self.SYS_LLM, user

        user = ("【农户问题】%s\n\n"
                "【知识图谱事实（唯一可信来源，不得超出）】\n%s\n\n"
                "请据此生成一份《%s》，按下面的骨架组织；骨架里图谱确实没有的项，"
                "写一行「图谱暂无此项数据」：\n%s"
                % (question, structured, title, outline))
        return self.SYS_PROMPT, user

    def _refine(self, question, intent, structured, route="graph", max_tokens=1400):
        """交给大模型组织语言；未配置则尝试本地小模型；都不可用返回 None"""
        system, user = self._refine_prompt(question, intent, structured, route=route)
        text = self._llm_api(system, user, max_tokens=max_tokens)
        if text:
            return self._apply_route_note(text, route)
        return self._llm_local(system, user, max_tokens=400)

    # ---- 空小节补全（图谱没覆盖到的条目，让大模型用通用农技知识补） ----
    def _sup_enabled(self):
        return bool(getattr(config, "ENABLE_LLM_SUPPLEMENT", True))

    def _sup_tokens(self):
        return int(getattr(config, "SUPPLEMENT_MAX_TOKENS", 900))

    # 兼容旧方法名
    def _llm_deepseek(self, system, user, max_tokens=None):
        return self._llm_api(system, user, max_tokens)

    def _llm_api(self, system, user, max_tokens=None):
        """
        调用大模型（统一走 core/llm_client.py，当前接入豆包·火山方舟）
        llm_client 内部已处理：附加参数（豆包关思维链）、超时与重试。
        """
        text, err = llm_client.chat(system, user, max_tokens=max_tokens)
        return text

    def llm_status(self):
        """当前大模型接入状态，便于前端与答辩展示"""
        return llm_client.status()

    def _llm_local(self, system, user, max_tokens=400):
        if not getattr(config, "ENABLE_LOCAL_LLM", False):
            return None
        try:
            if self._local_llm is None:
                import torch
                from transformers import AutoTokenizer, AutoModelForCausalLM
                path = os.path.join(BASE, "models", config.LOCAL_LLM_DIR)
                if not os.path.isdir(path):
                    return None
                tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
                model = AutoModelForCausalLM.from_pretrained(
                    path, trust_remote_code=True, torch_dtype=torch.float32)
                self._local_llm = (tok, model)
                print("INFO: 本地小模型已加载（DeepSeek 未配置时的兜底）")
            tok, model = self._local_llm
            prompt = "<|im_start|>system\n%s<|im_end|>\n<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % (system, user)
            ids = tok(prompt, return_tensors="pt").input_ids
            out = model.generate(ids, max_new_tokens=max_tokens, do_sample=False)
            text = tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True).strip()
            return text or None
        except Exception as e:
            print("WARN: 本地模型不可用：%s" % e)
            return None


if __name__ == "__main__":
    adv = AgriAdvisor()
    tests = [
        ("小麦条锈病怎么防治", "auto"),
        ("番茄怎么施肥", "auto"),
        ("水稻的农事管理方案", "auto"),
        ("pH 6.5、年均温 18℃ 适合种什么", "auto"),
    ]
    for q, m in tests:
        r = adv.advise(q, m, use_llm=False)
        print("=" * 70)
        print("Q:", q, "->", r["plan_name"], "| 溯源 %d 条" % len(r["sources"]))
        print(r["answer"][:600])
    adv.close()
