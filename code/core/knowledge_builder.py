#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识聚合器（KnowledgeBuilder）
================================
把「一个问题 → 图谱里所有相关事实」这一步，从各个问答入口零散的
单条查询里抽出来，统一做成**多跳聚合**，供两个入口共用：

    · core/rag_search.py    —— 基础图谱问答（/qa）
    · core/agri_advisor.py  —— 智能决策顾问的通用分支（/advisory）

为什么要单独一层
----------------
原来两个入口各查一两条关系就交给大模型（`作物 -[易感病害]-> 条锈病` 就结束了），
大模型手上没有素材，答案自然单薄。这里按「作物 / 病虫害 / 地域 / 土壤 / 气候 /
农药肥料」六类实体把关系网一次铺开，并顺着关系再往下挖一层细节：

    作物 → 易感病害 → 防治方法 → 措施类型 / 常见剂型 / 安全间隔期 / 使用要点
    作物 → 栽培要点 → 适宜时期 / 作用
    作物 → 生育期   → 阶段序号 / 历时天数 / 各阶段农事操作
    地域 → 主要土壤 / 属于气候区 / 主产作物
    农药肥料 → 含有养分 / 适用作物

输出的 `facts_text` 是给大模型看的**事实清单**，`sources` 是给人复核的
图谱关系，`followups` 是顺着当前话题能继续问的问题。

原则
----
**只搬运图谱里已有的事实**：不推理、不补全、不引入图谱以外的农药名称、
用量或标准。图谱里没有的，就明确写「图谱暂无此项数据」，让大模型无从发挥。
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 属性键 → 展示名（带单位）。没收录的键也会照原键名输出，不会丢信息。
PROP_LABEL = {
    # 作物
    "类别": "类别", "亚类": "亚类", "科属": "科属", "别名": "别名",
    "主要用途": "主要用途", "生育期天数": "全生育期(天)",
    "播种季节": "播种季节", "收获季节": "收获季节",
    "适宜pH下限": "适宜pH下限", "适宜pH上限": "适宜pH上限",
    "适宜温度下限": "适宜温度下限(℃)", "适宜温度上限": "适宜温度上限(℃)",
    "需水等级": "需水等级",
    "需降水量下限": "需降水量下限(mm)", "需降水量上限": "需降水量上限(mm)",
    "平均亩产kg": "平均亩产(kg)", "耐寒性": "耐寒性",
    "连作障碍": "连作障碍", "光照需求": "光照需求",
    # 病虫害 / 杂草
    "类型": "类型", "危害部位": "危害部位", "常发时期": "常发时期",
    "危害等级": "危害等级", "传播途径": "传播途径",
    # 地域
    "范围": "范围", "熟制": "熟制", "优势作物": "优势作物",
    # 土壤
    "质地": "质地", "酸碱度": "酸碱度", "保水性": "保水性",
    "肥力水平": "肥力水平", "主要分布": "主要分布", "常用改良剂": "常用改良剂",
    # 气候
    "年均温": "年均温(℃)", "年降水量": "年降水量(mm)",
    "无霜期": "无霜期(天)", "特征": "特征",
    # 农药肥料 / 防治措施
    "肥料类型": "肥料类型", "主要养分含量": "主要养分含量",
    "施用方式": "施用方式", "措施类型": "措施类型", "常见剂型": "常见剂型",
    "安全间隔期天": "安全间隔期(天)", "使用要点": "使用要点",
    "作用方式": "作用方式", "特点": "特点",
    # 栽培要点
    "适宜时期": "适宜时期", "作用": "作用",
    # 品种 / 养分 / 类别
    "元素符号": "元素符号", "元素类别": "元素类别",
    "生理功能": "生理功能", "说明": "说明",
    # 生育期
    "阶段序号": "阶段序号", "历时天数": "历时(天)",
    "起始日": "起始日", "结束日": "结束日",
}

# 作物关系展示顺序（没列到的关系排在其后，仍然会输出）
CROP_REL_ORDER = [
    "属于类别", "主产区", "适宜土壤", "适宜气候", "播种季节", "收获季节",
    "生育期", "栽培要点", "适配肥料", "需要养分",
    "易感病害", "易感虫害", "易生杂草",
    "适宜轮作", "主推品种", "品种属于",
]

# 绿色防控优先：防治措施排序权重
MEASURE_ORDER = {"农业防治": 0, "物理防治": 1, "生物防治": 2,
                 "生物农药": 3, "化学农药": 4}

# 危害等级排序（用于挑要把哪几个病害展开讲）
LEVEL_ORDER = {"高": 0, "较高": 1, "中": 2, "中等": 2, "低": 3, "较低": 3}

# 单块事实的条目上限，避免某条关系有几十个目标时把提示词撑爆
MAX_LIST = 10
MAX_DETAIL_DISEASE = 3


def _fmt(v):
    """属性值格式化：整数浮点去掉 .0，列表用顿号连接，空的返回空串"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return str(int(v)) if abs(v - int(v)) < 1e-9 else ("%.2f" % v).rstrip("0").rstrip(".")
    if isinstance(v, (list, tuple, set)):
        return "、".join([_fmt(x) for x in v if _fmt(x) != ""])
    s = str(v).strip()
    # 图谱里多值属性用「|」分隔（如 主要用途=口粮|面粉|饲料），展示时换成顿号更好读
    if "|" in s:
        s = "、".join([x.strip() for x in s.split("|") if x.strip()])
    # 原始数据里用这些符号表示"空"，别当成有效事实传给大模型
    if s in ("-", "--", "—", "/", "N/A", "n/a", "null", "None"):
        return ""
    return s


class KnowledgeBuilder:
    """把问题里命中的实体，展开成一份多跳的图谱事实清单"""

    def __init__(self, conn):
        self.conn = conn

    # ------------------------------------------------------------ 对外入口
    def collect(self, question, entities, max_blocks=3, focus=None):
        """
        question  : 原始问句
        entities  : {crop/pest/disease/weed/region/soil/climate/fertilizer/stage: 名称}
        focus     : 需要重点展开细节的实体名集合（一般是问句里点名的病虫害）

        返回 {topics, facts_text, sources, followups, block_count}
        """
        entities = entities or {}
        focus = set(focus or [])
        # 只有「病虫害/杂草/农药肥料」这类需要展开细节的实体进重点集合；
        # 作物本身不进来 —— 否则「小麦」会把「小麦白粉病」等一串病害都判成重点
        for k in ("disease", "pest", "weed", "fertilizer"):
            if entities.get(k):
                focus.add(entities[k])
        self._focus = focus

        self.sources = []
        self.facts = []
        self._detailed = set()          # 已在别的块里展开过的实体，避免重复成块
        topics = []

        # 顺序：作物 → 病虫害/杂草 → 地域 → 土壤 → 气候 → 农药肥料
        # 作物放第一位，因为它往往是提问的主体，也最容易顺出别的实体
        plan = [
            ("crop", "作物", self._crop_block),
            ("disease", "病虫害", self._pest_block),
            ("pest", "病虫害", self._pest_block),
            ("weed", "杂草", self._pest_block),
            ("region", "地域", self._region_block),
            ("soil", "土壤", self._soil_block),
            ("climate", "气候", self._climate_block),
            ("fertilizer", "农药肥料", self._fertilizer_block),
        ]
        seen = set()
        for key, label, fn in plan:
            name = entities.get(key)
            if not name or name in seen:
                continue
            # 作物块里已经把问到的这个病害/虫害展开过了，就不必再单独出一块
            # （图谱里同一种病有长短两个节点，所以用包含关系判断）
            if any(name in d or d in name for d in self._detailed):
                continue
            if len(self.facts) >= max_blocks:
                break
            try:
                block = fn(name, focus)
            except Exception as e:                      # 单块失败不影响其它块
                print("WARN: 聚合 %s(%s) 失败：%s" % (label, name, e))
                block = None
            if not block:
                continue
            seen.add(name)
            topics.append({"name": name, "label": label, "key": key})
            self.facts.append(block)

        return {
            "topics": topics,
            "facts_text": "\n\n".join(self.facts),
            "sources": list(dict.fromkeys(self.sources))[:60],
            "followups": self.followups_of(entities, asked=question),
            "block_count": len(self.facts),
        }

    # ------------------------------------------------------------ 数值格式化
    @staticmethod
    def _props_line(props, skip=("name",)):
        """把节点属性拼成「键：值；键：值」一行"""
        bits = []
        for k, v in (props or {}).items():
            if k in skip:
                continue
            s = _fmt(v)
            if not s:
                continue
            bits.append("%s：%s" % (PROP_LABEL.get(k, k), s))
        return "；".join(bits)

    def _node_props(self, name):
        rows = self.conn._run(
            "MATCH (n {name:$name}) RETURN properties(n) AS p LIMIT 1", name=name)
        return (rows[0]["p"] if rows else {}) or {}

    def _rel_targets(self, name, rel_type):
        """某个实体按指定关系指向的目标（返回 [{name,label,props}]）"""
        rows = self.conn._run("""
            MATCH (n {name:$name})-[:REL {type:$t}]->(t)
            RETURN DISTINCT t.name AS name, labels(t)[0] AS label,
                   properties(t) AS p
        """, name=name, t=rel_type)
        return rows

    def _incoming(self, name, limit=12):
        """谁指向了我（如 作物 -[易感病害]-> 我）：{关系: [名称...]}"""
        rows = self.conn._run("""
            MATCH (s)-[r:REL]->(n {name:$name})
            RETURN r.type AS rel, collect(DISTINCT s.name) AS items
        """, name=name)
        out = {}
        for r in rows:
            items = [x for x in (r["items"] or []) if x][:limit]
            if items:
                out[r["rel"]] = items
        return out

    # ------------------------------------------------------------ 各实体事实块
    def _crop_block(self, name, focus):
        profile = self.conn.get_entity_profile(name)
        if not profile:
            return None
        props = profile.get("props") or {}
        rels = profile.get("relations") or {}
        lines = ["### 【作物】%s" % name]

        p = self._props_line(props)
        lines.append("- 基本属性：" + (p or "图谱暂无此项数据"))
        if props:
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))

        # 关系：先按预设顺序，未列到的补在后面
        timeline = self.conn.get_crop_timeline(name)
        ordered = [r for r in CROP_REL_ORDER if r in rels]
        ordered += [r for r in rels if r not in ordered]
        # 有生育期时间轴时，就不再单列一行「生育期」（下面会给出阶段+历时+农事，信息量更大）
        if timeline and "生育期" in ordered:
            ordered.remove("生育期")
        detail_later = []
        for rel in ordered:
            items = [x["name"] for x in rels[rel] if x.get("name")]
            if not items:
                continue
            shown = items[:MAX_LIST]
            more = "" if len(items) <= MAX_LIST else "（共 %d 项，此处列前 %d）" % (len(items), MAX_LIST)
            lines.append("- %s%s：%s" % (rel, more, "、".join(shown)))
            self.sources.append("%s -[%s]-> %s" % (name, rel, "、".join(shown)))
            if rel in ("易感病害", "易感虫害", "易生杂草"):
                detail_later.append((rel, items))

        # 生育期进程（阶段 + 历时 + 该阶段农事）
        if timeline:
            segs = []
            for s in timeline:
                stage = (s.get("stage") or "").split("-", 1)[-1]
                days = _fmt(s.get("days"))
                acts = "、".join([x for x in (s.get("practices") or []) if x][:4])
                seg = "%s(%s天)" % (stage, days) if days else stage
                if acts:
                    seg += "〔%s〕" % acts
                segs.append(seg)
                self.sources.append("%s -[生育期]-> %s" % (name, s.get("stage")))
                for a in [x for x in (s.get("practices") or []) if x]:
                    self.sources.append("%s -[栽培要点]-> %s" % (s.get("stage"), a))
            lines.append("- 生育期进程：" + " → ".join(segs))

        # 栽培要点带细节（适宜时期 / 作用）
        points = [x["name"] for x in rels.get("栽培要点", [])]
        if points:
            lines.append("- 关键栽培要点：")
            for pt in points[:MAX_LIST]:
                pp = self._node_props(pt)
                when = _fmt(pp.get("适宜时期"))
                what = _fmt(pp.get("作用"))
                tail = ""
                if when or what:
                    tail = "（%s）" % "；".join([x for x in [when, what] if x])
                lines.append("  · %s%s" % (pt, tail))

        # 适配肥料带养分含量 / 施用方式
        ferts = rels.get("适配肥料", [])
        if ferts:
            lines.append("- 适配肥料：")
            for f in ferts[:MAX_LIST]:
                fp = self._node_props(f["name"])
                bits = [_fmt(fp.get("肥料类型")), _fmt(fp.get("主要养分含量"))]
                bits = [x for x in bits if x]
                way = _fmt(fp.get("施用方式"))
                tail = ""
                if bits:
                    tail = "（%s）" % "，".join(bits)
                if way:
                    tail += "　施用方式：%s" % way
                lines.append("  · %s%s" % (f["name"], tail))

        # 病害 / 虫害 / 杂草：点名问到的优先展开，其余按「病害→虫害→杂草 + 危害等级」挑前几个
        REL_PRI = {"易感病害": 0, "易感虫害": 1, "易生杂草": 2}
        flat = []
        for rel, items in detail_later:
            for n in items:
                flat.append((rel, n))
        flat.sort(key=lambda x: (0 if self._is_focus(x[1]) else 1,
                                 REL_PRI.get(x[0], 3),
                                 self._level_rank(x[1]), x[1]))

        def overlapped(n):
            """图谱里同一种病有长短两个节点（大斑病 / 玉米大斑病），只展开一个"""
            return any(n in p[1] or p[1] in n for p in picked)

        picked = []
        for item in flat:                       # 第一步：问句点名的
            if self._is_focus(item[1]) and not overlapped(item[1]):
                picked.append(item)
        hit = len(picked)
        for item in flat:                       # 第二步：补足条数以提供上下文
            if len(picked) >= (2 if hit else MAX_DETAIL_DISEASE):
                break
            if item not in picked and not overlapped(item[1]):
                picked.append(item)
        for rel, n in picked:
            d = self._pest_detail(n, indent="    ")
            if d:
                lines.append("  · 重点展开（%s）：" % rel)
                lines.extend(d)
                self._detailed.add(n)

        return "\n".join(lines)

    def _is_focus(self, name):
        """
        判断某个病害/虫害是不是问句里点名要问的。

        图谱里同一种病有「条锈病」和「小麦条锈病」两个节点，问句里说的是前者，
        所以这里用包含关系做匹配（互为子串就算命中）。
        """
        for f in getattr(self, "_focus", ()) or ():
            if f == name:
                return True
            if len(f) >= 2 and (f in name or name in f):
                return True
        return False

    def _level_rank(self, name):
        try:
            props = self._node_props(name)
        except Exception:
            return 9
        return LEVEL_ORDER.get(_fmt(props.get("危害等级")), 5)

    def _pest_block(self, name, focus):
        props = self._node_props(name)
        if not props:
            return None
        lines = ["### 【%s】%s" % ("杂草" if props.get("类型") == "杂草" else "病虫害", name)]
        p = self._props_line(props)
        if p:
            lines.append("- 基本属性：" + p)
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))
        lines.extend(self._pest_detail(name, indent=""))
        inc = self._incoming(name)
        for rel in ("易感病害", "易感虫害", "易生杂草", "适用作物"):
            if rel in inc:
                lines.append("- 关联作物（%s）：%s" % (rel, "、".join(inc[rel][:MAX_LIST])))
                self.sources.append("%s -[%s]-> %s" % ("、".join(inc[rel][:MAX_LIST]), rel, name))
        return "\n".join(lines)

    def _pest_detail(self, name, indent="  "):
        """防治方法明细（按绿色防控优先排序），可嵌进作物块里复用"""
        rows = self.conn._run("""
            MATCH (n {name:$name})-[:REL {type:'防治方法'}]->(m)
            RETURN m.name AS name, labels(m)[0] AS label, properties(m) AS p
        """, name=name)
        if not rows:
            return []
        rows = [r for r in rows if r.get("name")]
        rows.sort(key=lambda r: MEASURE_ORDER.get(_fmt((r.get("p") or {}).get("措施类型")), 5))
        lines = ["%s· %s 的防治方法（按 农业防治→生物防治→化学农药 优先排序）："
                 % (indent, name)]
        for r in rows:
            mp = r.get("p") or {}
            kind = _fmt(mp.get("措施类型")) or (r.get("label") or "综合措施")
            seg = "%s  - 【%s】%s" % (indent, kind, r["name"])
            extra = []
            for key in ("常见剂型", "作用方式", "使用要点"):
                val = _fmt(mp.get(key))
                if val:
                    extra.append("%s：%s" % (PROP_LABEL.get(key, key), val))
            if extra:
                seg += "（" + "；".join(extra) + "）"
            interval = _fmt(mp.get("安全间隔期天"))
            if interval and interval != "0":
                seg += "　安全间隔期 %s 天" % interval
            lines.append(seg)
            self.sources.append("%s -[防治方法]-> %s（%s）" % (name, r["name"], kind))
        return lines

    def _region_block(self, name, focus):
        props = self._node_props(name)
        lines = ["### 【地域】%s" % name]
        p = self._props_line(props)
        lines.append("- 区域本底：" + (p or "图谱暂无此项数据"))
        if p:
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))

        soils = [x["name"] for x in self._rel_targets(name, "主要土壤")]
        climates = self._rel_targets(name, "属于气候区")
        if soils:
            lines.append("- 主要土壤：%s" % "、".join(soils[:MAX_LIST]))
            self.sources.append("%s -[主要土壤]-> %s" % (name, "、".join(soils[:MAX_LIST])))
        if climates:
            for c in climates[:MAX_LIST]:
                cp = self._props_line(c.get("p") or {}, skip=("name",))
                lines.append("- 气候区：%s%s" % (c["name"], ("（%s）" % cp) if cp else ""))
                self.sources.append("%s -[属于气候区]-> %s" % (name, c["name"]))

        rows = self.conn._run("""
            MATCH (c:作物)-[:REL {type:'主产区'}]->(r {name:$name})
            RETURN c.name AS name, c.`类别` AS cat, c.`生育期天数` AS days,
                   c.`平均亩产kg` AS y
        """, name=name)
        if rows:
            lines.append("- 本地已有主产作物（%d 个）：" % len(rows))
            for r in rows[:MAX_LIST]:
                bits = [x for x in [_fmt(r.get("cat")),
                                    ("生育期%s天" % _fmt(r.get("days"))) if _fmt(r.get("days")) else "",
                                    ("亩产%s kg" % _fmt(r.get("y"))) if _fmt(r.get("y")) else ""] if x]
                lines.append("  · %s%s" % (r["name"], ("（%s）" % "，".join(bits)) if bits else ""))
                self.sources.append("%s -[主产区]-> %s" % (r["name"], name))
            if len(rows) > MAX_LIST:
                lines.append("  · ……另有 %d 个未列出" % (len(rows) - MAX_LIST))
        if len(lines) == 1:
            return None
        return "\n".join(lines)

    def _soil_block(self, name, focus):
        props = self._node_props(name)
        if not props:
            return None
        lines = ["### 【土壤】%s" % name]
        p = self._props_line(props)
        lines.append("- 土壤性状：" + (p or "图谱暂无此项数据"))
        if p:
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))
        crops = self._incoming(name).get("适宜土壤") or []
        if crops:
            lines.append("- 适宜作物（%d 个）：%s" % (len(crops), "、".join(crops[:MAX_LIST])))
            self.sources.append("%s -[适宜土壤]-> %s" % ("、".join(crops[:MAX_LIST]), name))
        mod = [x["name"] for x in self._rel_targets(name, "常用改良剂")]
        if mod:
            lines.append("- 常用改良剂：%s" % "、".join(mod[:MAX_LIST]))
            self.sources.append("%s -[常用改良剂]-> %s" % (name, "、".join(mod[:MAX_LIST])))
        return "\n".join(lines)

    def _climate_block(self, name, focus):
        props = self._node_props(name)
        if not props:
            return None
        lines = ["### 【气候】%s" % name]
        p = self._props_line(props)
        lines.append("- 气候条件：" + (p or "图谱暂无此项数据"))
        if p:
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))
        crops = self._incoming(name).get("适宜气候") or []
        if crops:
            lines.append("- 适宜作物：%s" % "、".join(crops[:MAX_LIST]))
            self.sources.append("%s -[适宜气候]-> %s" % ("、".join(crops[:MAX_LIST]), name))
        regions = self._incoming(name).get("属于气候区") or []
        if regions:
            lines.append("- 所属地域：%s" % "、".join(regions[:MAX_LIST]))
            self.sources.append("%s -[属于气候区]-> %s" % ("、".join(regions[:MAX_LIST]), name))
        return "\n".join(lines)

    def _fertilizer_block(self, name, focus):
        props = self._node_props(name)
        if not props:
            return None
        lines = ["### 【农药肥料】%s" % name]
        p = self._props_line(props)
        lines.append("- 基本信息：" + (p or "图谱暂无此项数据"))
        if p:
            self.sources.append("%s -[属性]-> %s" % (name, p[:80]))
        nutris = [x["name"] for x in self._rel_targets(name, "含有养分")]
        if nutris:
            lines.append("- 含有养分：%s" % "、".join(nutris[:MAX_LIST]))
            self.sources.append("%s -[含有养分]-> %s" % (name, "、".join(nutris[:MAX_LIST])))
        inc = self._incoming(name)
        for rel in ("适用作物", "适配肥料", "防治方法"):
            if rel in inc:
                lines.append("- 关联作物（%s）：%s" % (rel, "、".join(inc[rel][:MAX_LIST])))
                self.sources.append("%s -[%s]-> %s" % ("、".join(inc[rel][:MAX_LIST]), rel, name))
        return "\n".join(lines)

    # ------------------------------------------------------------ 追问建议
    @staticmethod
    def _plain(s):
        """去掉标点做归一化，用于判断两个问法是不是同一个问题"""
        return re.sub(r"[\s，。？、,.?！!：:；;（）()【】\[\]－\-]", "", s or "")

    @staticmethod
    def followups_of(ent, asked=None):
        """
        按命中的实体给出可以继续追问的问题（advisor 的各类处方也共用它）。
        传了 asked 时会把与刚问过的问题重复的项去掉，免得推荐"再问一次同一个问题"。
        """
        out = []
        c = ent.get("crop")
        if c:
            out += ["%s的农事管理方案" % c, "%s怎么施肥" % c, "%s易感哪些病害" % c]
        tgt = ent.get("disease") or ent.get("pest") or ent.get("weed")
        if tgt:
            out += ["%s怎么防治" % tgt, "%s主要危害哪些作物" % tgt]
        r = ent.get("region")
        if r:
            out += ["%s适合种什么" % r, "%s的主要土壤和气候" % r]
        s = ent.get("soil")
        if s:
            out += ["%s适合种什么" % s]
        f = ent.get("fertilizer")
        if f:
            out += ["%s适合哪些作物" % f, "%s怎么施用" % f]
        if not out:
            out = ["小麦条锈病怎么防治", "番茄怎么施肥",
                   "水稻的农事管理方案", "pH 6.5、年均温 18℃ 适合种什么"]
        out = list(dict.fromkeys(out))
        if asked:
            q = KnowledgeBuilder._plain(asked)
            out = [f for f in out
                   if KnowledgeBuilder._plain(f) != q
                   and KnowledgeBuilder._plain(f) not in q]
        return out[:4]

    # 旧调用名，保持兼容
    _followups = followups_of


if __name__ == "__main__":
    from core.neo4j_conn import Neo4jConnection
    conn = Neo4jConnection("bolt://localhost:7687", "neo4j", "123456")
    kb = KnowledgeBuilder(conn)
    for q, ents in [
        ("小麦条锈病怎么防治", {"crop": "小麦", "disease": "条锈病"}),
        ("黄淮海平原适合种什么", {"region": "黄淮海平原"}),
        ("番茄怎么施肥", {"crop": "番茄"}),
    ]:
        pack = kb.collect(q, ents)
        print("=" * 72)
        print("Q:", q)
        print(pack["facts_text"][:1500])
        print("-- 溯源 %d 条 --" % len(pack["sources"]))
        print("-- 追问:", " / ".join(pack["followups"]))
    conn.close()
