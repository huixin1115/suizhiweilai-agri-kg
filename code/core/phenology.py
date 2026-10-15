#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
物候期推算与农事提醒引擎（PhenologyEngine）
============================================
把知识图谱里的「生育期」数据变成"今天该干什么"。

图谱里每个作物都挂着 5~7 个生育期节点，节点上带：
    阶段序号 / 历时天数 / 起始日 / 结束日（均为播种后天数）
再通过 生育期 -[栽培要点]-> 农事操作 关联到具体做法。

因此只要知道**作物 + 播种日期**，就能算出：
    今天处于哪个阶段、第几天、还剩几天、这一阶段该干什么、
    下一阶段什么时候来（提前 5 天预警）、当期与下期要防哪些病虫害。

不用大模型，纯图谱 + 日期推算，结果确定、可复现。
"""
import datetime
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 生育期实拍图目录：static/stages/作物名__阶段名.jpg
STAGE_IMG_DIR = os.path.join(BASE, "static", "stages")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def safe_name(text):
    """文件名安全化：Windows 非法字符 + 空格统一换成下划线
    （阶段名里真的出现过斜杠，比如「分蘖期/苗期」，不处理会写不出文件）"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text or "").strip())
    return s.strip("_.") or "未命名"


def stage_short(name):
    """'小麦-灌浆期' → '灌浆期'"""
    return str(name or "").split("-", 1)[-1].strip()


def stage_image_url(crop, stage_name):
    """
    查该「作物 + 生育期」的实拍图，返回可访问的 URL；没有图返回 None。
    命名规则（两种都认）：
        static/stages/小麦__灌浆期.jpg      ← 推荐
        static/stages/小麦_灌浆期.jpg
    扩展名 jpg / jpeg / png / webp 均可。
    """
    short = stage_short(stage_name)
    if not crop or not short:
        return None
    bases = ["%s__%s" % (safe_name(crop), safe_name(short)),
             "%s_%s" % (safe_name(crop), safe_name(short))]
    for b in bases:
        for ext in IMG_EXTS:
            fn = b + ext
            if os.path.isfile(os.path.join(STAGE_IMG_DIR, fn)):
                return "/static/stages/" + fn
    return None


# 提前预警天数：下一阶段还有几天时开始提示
LEAD_DAYS = 5

# 施肥建议规则：按阶段关键词 → (标题, 要点, 优先施用方式, 该阶段应排在后面的肥料关键词)
FERT_RULES = [
    (("播种", "出苗", "育苗", "定植", "移栽", "整地"),
     "基肥为主", "整地时一次施入，占全季氮肥约 40%~50%；缺锌硼地块配施微肥。",
     ("基肥",), ()),
    (("苗期", "分蘖", "幼苗", "莲座", "团棵"),
     "促根提苗", "看苗追肥，旺长田块控氮；弱苗少量补氮促分蘖。",
     ("追肥", "基肥"), ()),
    (("拔节", "抽薹", "现蕾", "心叶", "蕾期", "花芽"),
     "氮钾并重", "此期需肥量快速上升，追施氮肥配钾肥，促壮秆大穗。",
     ("追肥",), ()),
    (("孕穗", "抽穗", "开花", "授粉", "抽雄", "花序"),
     "稳氮增钾", "控制氮肥防贪青，增加磷钾提高结实率；可叶面补硼。",
     ("追肥", "叶面"), ()),
    (("灌浆", "结荚", "鼓粒", "结球", "膨大", "结瓜", "铃期", "果期"),
     "磷钾为主", "以磷钾为主，配合叶面喷施磷酸二氢钾，防早衰、增粒重。",
     ("叶面", "追肥"), ("尿素", "碳酸氢铵", "硫酸铵", "氯化铵")),
    (("成熟", "收获", "采收", "吐絮"),
     "停止施肥", "收获前停止施肥，控水促成熟；注意安全间隔期，采收前禁用化学农药。",
     (), ("尿素", "复合肥", "氯化钾", "过磷酸钙")),
    (("越冬", "休眠"),
     "控水控氮", "越冬期控水控氮，提高抗寒性；注意防冻害。",
     (), ("尿素",)),
]

# ---------------------------------------------------------------- 土质规则
# 按「质地」给水肥管理差异（沙土要少量多次、黏土要防渍）
SOIL_TEXTURE_RULES = [
    (("砂质", "砂壤", "砾石", "粗砂", "风沙"),
     "保水保肥能力弱",
     ["追肥要「少量多次」：单次用量减 20%~30%，间隔适当缩短，防止漏肥。",
      "灌溉宜小水勤灌，避免大水漫灌把养分淋走。",
      "增施有机肥或秸秆还田，逐步提升保水保肥能力。"]),
    (("黏质", "黏壤", "重壤"),
     "保水强、透气差",
     ["雨后及时开沟排水，防止渍害和沤根。",
      "施肥可适当集中，但忌一次过量，避免烧根。",
      "结合深耕、增施有机肥改善通透性。"]),
    (("壤质", "中壤", "轻壤"),
     "质地适中",
     ["质地适中，水肥管理按常规执行即可。"]),
]

# 按酸碱度给改良建议
SOIL_PH_TIPS = {
    "strong_acid": ("偏强酸", ["可施石灰调酸（每亩 50~100 kg，结合整地施入）。",
                          "注意补充钙、镁，防止缺素。"]),
    "acid": ("偏酸", ["适合喜酸作物；种其他作物时可适量施石灰。",
                    "定期测土，避免长期酸化。"]),
    "neutral": ("酸碱度适宜", ["酸碱度适中，多数作物都能正常生长，保持即可。"]),
    "alkaline": ("偏碱", ["易缺铁、锌、硼，建议叶面补微肥。",
                        "可施石膏或生理酸性肥料改良。",
                        "增施有机肥缓冲碱性、改善结构。"]),
}


# ---------------------------------------------------------------- 阶段示意图
# 图谱里有 64 个阶段名（各作物叫法不同），归并到 static/stages/ 下的 12 张通用示意图。
# 规则**有序**：越具体的放前面，泛化的放后面兜底。
ICON_RULES = [
    (("播种", "出苗", "萌芽", "直播"), "sowing"),
    (("分蘖", "分枝", "发棵"), "tillering"),
    (("孕穗", "现蕾", "花芽分化", "蕾期"), "booting"),
    (("抽穗", "开花", "花铃"), "flowering"),
    (("灌浆", "结荚", "鼓粒", "角果", "淀粉积累", "糖分积累"), "filling"),
    (("膨大", "幼果", "浆果", "块根", "块茎", "根茎", "果实发育", "根部", "药用部位"), "enlarging"),
    (("拔节", "伸蔓", "伸长期", "蕾薹", "抽梢", "梢"), "jointing"),
    (("结球", "产品器官"), "heading"),
    (("成熟", "收获", "采收", "采摘", "吐絮", "转色", "采挖", "加工"), "maturing"),
    (("休眠", "越冬"), "dormant"),
    (("育苗", "移栽", "定植", "缓苗", "幼龄", "苗期", "幼苗"), "seedling"),
    (("莲座", "叶片生长", "营养生长", "生殖生长", "旺盛"), "vegetative"),
]


def stage_icon(stage_name):
    """阶段名 → 示意图文件名（不含扩展名）；无法归类时返回 None"""
    name = str(stage_name or "")
    if not name:
        return None
    for kws, icon in ICON_RULES:
        if any(k in name for k in kws):
            return icon
    return None


def _elapsed_text(days):
    """
    把「已经种了多少天」说成人话：28 天 / 61 天（约 2 个月）/ 400 天（约 1.1 年）

    农户既想知道确切天数，也需要一个"种了多久"的直观量级，
    所以小数量级直接给天数，超过一个月再附一个约数（按月 30 天估算）。
    """
    if not days or days < 1:
        return ""

    def _rough(v, unit):
        # 量级越大越不需要小数：61 天说"约 2 个月"比"约 2.03 个月"好读
        s = ("%.0f" % v) if (v >= 10 or abs(v - round(v)) < 0.05) else ("%.1f" % v)
        return "%d 天（约 %s %s）" % (days, s, unit)

    if days < 30:
        return "%d 天" % days
    if days < 365:
        return _rough(round(days / 30.0, 1), "个月")
    return _rough(round(days / 365.0, 1), "年")


def _ph_band(mid):
    """pH 中值 → 档位。阈值按农学惯例：<5.5 偏强酸 / 5.5~6.5 偏酸 / 6.5~7.5 适宜 / >7.5 偏碱"""
    if mid < 5.5:
        return "strong_acid"
    if mid < 6.5:
        return "acid"
    if mid <= 7.5:
        return "neutral"
    return "alkaline"


def _ph_level(value):
    """
    解析酸碱档位，两种输入都吃：
      · 图谱里的区间文本「微酸 pH5.5-6.5」（取中值判档）
      · 农户手填的单个值 6.5（直接用）

    返回 (档位, 区间或 None)；解析不出来返回 (None, None)。
    """
    import re as _re
    s = str(value if value is not None else "").strip()
    if not s:
        return None, None
    rng = _re.search(r"pH\s*([\d.]+)\s*[-~至]\s*([\d.]+)", s)
    if rng:
        lo, hi = float(rng.group(1)), float(rng.group(2))
        return _ph_band((lo + hi) / 2), (lo, hi)
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None, None
    if not 0 <= v <= 14:
        return None, None
    return _ph_band(v), None


class PhenologyEngine:
    def __init__(self, conn):
        """conn: Neo4jConnection 实例"""
        self.conn = conn
        self._cache = {}

    # ==================================================== 基础数据
    def list_crops(self):
        """列出所有具备生育期数据的作物（按主产区数量排序）"""
        rows = self.conn._run("""
            MATCH (c:作物)-[:REL {type:'生育期'}]->(s:生育期)
            OPTIONAL MATCH (c)-[:REL {type:'主产区'}]->(r)
            RETURN c.name AS name, count(DISTINCT s) AS stages,
                   count(DISTINCT r) AS regions, c.`生育期天数` AS total_days
            ORDER BY regions DESC, name
        """)
        return rows

    def get_stages(self, crop):
        """
        取某作物的全部生育期阶段（按序号排序）
        返回 [{name, short, idx, days, start, end, practices:[{name, timing, effect}]}]
        """
        if crop in self._cache:
            return self._cache[crop]
        rows = self.conn._run("""
            MATCH (c {name:$crop})-[:REL {type:'生育期'}]->(s:生育期)
            RETURN s.name AS name, s.`阶段序号` AS idx, s.`历时天数` AS days,
                   s.`起始日` AS start, s.`结束日` AS end
            ORDER BY idx
        """, crop=crop)
        stages = []
        for r in rows:
            stages.append({
                "name": r["name"],
                "short": (r["name"].split("-", 1)[-1] if r["name"] else ""),
                "idx": r["idx"] or 0,
                "days": r["days"] or 0,
                "start": r["start"] or 0,
                "end": r["end"] or 0,
                "practices": [],
            })
        if stages:
            names = [s["name"] for s in stages]
            pr = self.conn._run("""
                MATCH (s:生育期)-[r:REL]->(p)
                WHERE s.name IN $names AND NOT p:生育期
                RETURN s.name AS stage, p.name AS practice,
                       p.`适宜时期` AS timing, p.`作用` AS effect
            """, names=names)
            idx = {s["name"]: s for s in stages}
            for row in pr:
                st = idx.get(row["stage"])
                if st is None:
                    continue
                if any(x["name"] == row["practice"] for x in st["practices"]):
                    continue
                st["practices"].append({"name": row["practice"],
                                        "timing": row["timing"] or "",
                                        "effect": row["effect"] or ""})
        self._cache[crop] = stages
        return stages

    @staticmethod
    def _norm(text):
        """标准化阶段名：去「期/盛」等修饰，便于模糊匹配"""
        t = str(text or "").strip()
        t = t.split("至")[0].split("-")[-1]
        for w in ("阶段", "时期", "盛"):
            t = t.replace(w, "")
        return t.replace("期", "")

    @classmethod
    def _match_score(cls, target, stage_short):
        """目标词与阶段名的匹配分（0=不匹配，3=完全一致）"""
        t, s = cls._norm(target), cls._norm(stage_short)
        if not t or not s:
            return 0
        if t == s:
            return 3
        if t in s or s in t:
            return 2
        for n in range(min(len(t), len(s)), 1, -1):
            if t[:n] in s or s[:n] in t:
                return 1
        return 0

    def _locate(self, stages, word):
        """在阶段列表里定位某个词，返回 (序号下标, 分数)"""
        best, score = None, 0
        for i, st in enumerate(stages):
            sc = self._match_score(word, st["short"])
            if sc > score:
                best, score = i, sc
        return best, score

    # ==================================================== 推算
    def resolve(self, crop, sow_date, today=None):
        """
        推算当前物候状态
        参数：crop 作物名；sow_date 'YYYY-MM-DD' 或 date；today 默认今天
        返回 dict（含 status: before_sow / growing / done / unknown）
        """
        stages = self.get_stages(crop)
        if not stages:
            return {"status": "unknown", "crop": crop,
                    "message": "知识图谱中暂无「%s」的生育期数据，可换一个作物试试" % crop}

        if isinstance(sow_date, str):
            sow = datetime.datetime.strptime(sow_date.strip(), "%Y-%m-%d").date()
        else:
            sow = sow_date
        today = today or datetime.date.today()
        day_no = (today - sow).days + 1          # 播种当天记为第 1 天

        total_days = stages[-1]["end"] or stages[-1]["days"]
        harvest = sow + datetime.timedelta(days=max(0, total_days - 1))
        elapsed = day_no if day_no >= 1 else 0
        info = {
            "crop": crop,
            "sow_date": sow.isoformat(),
            "today": today.isoformat(),
            "day_no": day_no,
            "total_days": total_days,
            "stages": stages,
            # ---- 已种植时长（有的地块是种了一段时间后才补录的，这里单独给全） ----
            "elapsed_days": elapsed,
            "elapsed_text": _elapsed_text(elapsed),
            # 预计收获日 = 播种日 + (全生育期天数 - 1)，因为播种当天记为第 1 天
            "harvest_date": harvest.isoformat(),
            "harvest_md": harvest.strftime("%m-%d"),
            "days_to_harvest": total_days - day_no,
        }

        if day_no < 1:
            info.update(status="before_sow",
                        days_to_sow=1 - day_no,
                        message="距播种还有 %d 天（播种日 %s）" % (1 - day_no, sow.isoformat()))
            return info

        cur = None
        for st in stages:
            if st["start"] <= day_no <= st["end"]:
                cur = st
                break
        if cur is None:
            for st in stages:                     # 落在阶段间隙时取最近的上一阶段
                if st["end"] < day_no:
                    cur = st
        if day_no > total_days:
            info.update(status="done", stage=None,
                        days_over=day_no - total_days,
                        message="已过最后一阶段（%s），全生育期 %d 天，可准备下一茬"
                                % (stages[-1]["short"], total_days))
            return info

        i = stages.index(cur)
        in_stage = max(1, min(cur["days"], day_no - cur["start"] + 1))
        info.update(
            status="growing",
            stage=cur,
            stage_index=i,
            stage_day=in_stage,
            stage_left=max(0, cur["end"] - day_no),
            overall_percent=round(day_no / float(total_days) * 100, 1) if total_days else 0,
            stage_percent=round(in_stage / float(cur["days"]) * 100, 1) if cur["days"] else 0,
        )
        nxt = stages[i + 1] if i + 1 < len(stages) else None
        info["next_stage"] = nxt
        if nxt:
            info["days_to_next"] = max(0, nxt["start"] - day_no)
            info["next_soon"] = info["days_to_next"] <= LEAD_DAYS
        else:
            info["days_to_next"] = None
            info["next_soon"] = False
        prev = stages[i - 1] if i > 0 else None
        info["prev_stage"] = prev
        info["message"] = self._stage_message(info)
        return info

    @staticmethod
    def _stage_message(info):
        """统一用「还有 N 天进入下一阶段」的口径，避免与列表摘要不一致"""
        st = info["stage"]
        msg = "播种后第 %d 天，处于【%s】第 %d/%d 天" % (
            info["day_no"], st["short"], info["stage_day"], st["days"])
        if info.get("next_stage") and info.get("days_to_next"):
            msg += "，还有 %d 天进入【%s】" % (info["days_to_next"], info["next_stage"]["short"])
        elif info.get("next_stage") is None:
            msg += "，之后进入成熟收获"
        else:
            msg += "，本阶段即将结束"
        return msg

    # ==================================================== 风险与建议
    def risks(self, crop, stage=None, stage_index=None, stages=None):
        """
        病虫害风险：用「常发时期」与当前阶段做区间匹配
        返回 [{name, kind, level, part, period, measures, urgency}]
        """
        stages = stages or self.get_stages(crop)
        if stage is None or stage_index is None:
            return []
        rows = self.conn._run("""
            MATCH (c {name:$crop})-[:REL]->(p)
            WHERE (p:病虫害 OR p:杂草)
            OPTIONAL MATCH (p)-[:REL {type:'防治方法'}]->(m)
            RETURN p.name AS name, labels(p)[0] AS label, p.`类型` AS kind,
                   p.`危害等级` AS level, p.`危害部位` AS part,
                   p.`常发时期` AS period, collect(DISTINCT m.name) AS measures
        """, crop=crop)

        out = []
        for r in rows:
            hit = self._period_hit(r["period"], stages, stage_index)
            if not hit:
                continue
            measures = [m for m in (r["measures"] or []) if m]
            out.append({
                "name": r["name"],
                "label": r["label"] or "病虫害",
                "kind": r["kind"] or "",
                "level": r["level"] or "",
                "part": r["part"] or "",
                "period": r["period"] or "",
                "measures": measures[:6],
                "urgency": self._urgency(r["level"], hit),
            })
        order = {"重": 0, "中": 1, "轻": 2, "": 3}
        out.sort(key=lambda x: (order.get(x["level"], 3), -x["urgency"]))
        return out

    @classmethod
    def _period_hit(cls, period, stages, cur_index):
        """
        判断「常发时期」是否覆盖当前阶段
        支持：全生育期 / 单阶段（苗期） / 区间（拔节期至灌浆期） / 多值（春茶期|秋茶期）
        """
        if not period:
            return 0
        text = str(period)
        if "全生育期" in text or "整个生育" in text:
            return 1
        best = 0
        for seg in re.split(r"[|、,，/]", text):
            seg = seg.strip()
            if not seg:
                continue
            if "至" in seg:
                a, _, b = seg.partition("至")
                ia, sa = cls._locate_static(stages, a)
                ib, sb = cls._locate_static(stages, b)
                if ia is not None and ib is not None and sa > 0 and sb > 0:
                    lo, hi = sorted((ia, ib))
                    if lo <= cur_index <= hi:
                        best = max(best, 2)
                else:
                    # 区间端点识别不全时退化为包含匹配
                    for i, st in enumerate(stages):
                        if i == cur_index and (cls._match_score(a, st["short"]) or
                                               cls._match_score(b, st["short"])):
                            best = max(best, 1)
            else:
                i, sc = cls._locate_static(stages, seg)
                if i == cur_index and sc > 0:
                    best = max(best, 1 if sc < 3 else 2)
        return best

    @classmethod
    def _locate_static(cls, stages, word):
        best, score = None, 0
        for i, st in enumerate(stages):
            sc = cls._match_score(word, st["short"])
            if sc > score:
                best, score = i, sc
        return best, score

    @staticmethod
    def _urgency(level, hit):
        base = {"重": 3, "中": 2, "轻": 1}.get(level or "", 1)
        return base * (2 if hit >= 2 else 1)

    def fertilize(self, crop, stage):
        """按当前阶段给施肥建议，肥料来自图谱"""
        if not stage:
            return None
        short = stage["short"]
        rule = None
        for kws, title, tip, methods, deprior in FERT_RULES:
            if any(k in short for k in kws):
                rule = (title, tip, methods, deprior)
                break
        if rule is None:
            rule = ("按需追肥", "结合苗情与土壤墒情追肥，避免一次过量。",
                    ("追肥", "基肥"), ())
        title, tip, methods, deprior = rule

        plan = self.conn.get_fertilizer_plan(crop)
        ferts = plan.get("fertilizers") or []

        def rank(f):
            m = str(f.get("method") or "")
            s = 0
            for i, x in enumerate(methods):
                if x in m:
                    s = max(s, len(methods) - i)
            if any(d in str(f.get("name") or "") for d in deprior):
                s -= 10
            return -s

        picked = sorted(ferts, key=rank)
        return {
            "title": title,
            "tip": tip,
            "fertilizers": picked[:6],
            "nutrients": (plan.get("nutrients") or [])[:5],
        }

    # ==================================================== 土质
    def list_soils(self):
        """
        土壤列表（供前端下拉），带属性摘要

        ph       —— 该土类的酸碱度**范围文本**（如 "中性-微酸 pH6.0-7.5"），只做展示
        ph_value —— 从上面的范围取中点算出的**数值型**典型值（如 6.8）。
                    前端选了土质后用它自动填 pH 输入框，农户可再改成自己实测的值。
                    取不到范围时为 None，前端就不自动填。
        """
        rows = self.conn._run("""
            MATCH (s:土壤)
            OPTIONAL MATCH (s)-[:REL {type:'常用改良剂'}]->(f)
            RETURN s.name AS name, s.`质地` AS texture, s.`酸碱度` AS ph,
                   s.`酸碱度典型值` AS ph_value,
                   s.`肥力水平` AS fertility, s.`保水性` AS water,
                   collect(DISTINCT f.name) AS fixes
            ORDER BY (s.`质地` IS NULL), name
        """)
        return rows

    def soil_advice(self, crop, soil_name, ph=None):
        """
        土质建议：把「土壤属性 + 该作物是否适宜 + 改良剂」变成可执行的田间管理提示
        返回 None 表示未选土壤或图谱里没有该土壤

        ph：**农户自己填**的土壤 pH（选填）。图谱里土壤节点的「酸碱度」只是该土类的
        一个大致范围，跟自家这块地测出来的值常常对不上，所以页面上不再显示图谱值，
        改由农户填实测值，再按这个值给调酸/调碱建议；不填就不给酸碱类建议。
        """
        if not soil_name:
            return None
        rows = self.conn._run("""
            MATCH (s:土壤 {name:$soil})
            OPTIONAL MATCH (s)-[:REL {type:'常用改良剂'}]->(f)
            RETURN properties(s) AS props, collect(DISTINCT f.name) AS fixes
        """, soil=soil_name)
        if not rows:
            return None
        props = {k: v for k, v in (rows[0]["props"] or {}).items() if k != "name"}
        fixes = [f for f in (rows[0]["fixes"] or []) if f]

        # 该作物的适宜土壤，用于匹配度判断
        suit = self.conn._run("""
            MATCH (c:作物 {name:$crop})-[:REL {type:'适宜土壤'}]->(s:土壤)
            RETURN collect(DISTINCT s.name) AS soils
        """, crop=crop)
        suitable = [s for s in ((suit[0]["soils"] if suit else []) or []) if s]

        texture = str(props.get("质地") or "")
        tips, texture_label = [], ""
        for kws, label, items in SOIL_TEXTURE_RULES:
            if any(k in texture for k in kws):
                texture_label, tips = label, list(items)
                break
        if not texture_label:
            texture_label = "质地未标注"
            tips.append("该土壤暂无质地记录，建议按当地农技部门测土结果管理水肥。")

        # 酸碱度：只用农户自己填的值；图谱里的「酸碱度」不再展示（见方法说明）
        ph_user, ph_label, ph_tips, ph_level = None, "", [], None
        if ph not in (None, ""):
            try:
                ph_user = round(float(ph), 2)
            except (TypeError, ValueError):
                ph_user = None
        if ph_user is not None:
            ph_level, _ph_range = _ph_level(ph_user)
            if ph_level:
                ph_label, ph_tips = SOIL_PH_TIPS[ph_level]
                ph_tips = ["土壤 pH %s「%s」：%s" % (("%g" % ph_user), ph_label, t)
                           for t in ph_tips]
            else:
                ph_user = None        # 0~14 之外的值当没填，免得给出莫名其妙的建议

        fertility = str(props.get("肥力水平") or "")
        fert_tips = []
        if fertility in ("低", "中下"):
            fert_tips.append("肥力偏低，建议增施有机肥并做测土配方施肥，避免盲目加大化肥。")
        elif fertility == "高":
            fert_tips.append("肥力较高，注意控制氮肥用量，防止贪青晚熟与倒伏。")

        water = str(props.get("保水性") or "")
        water_tips = []
        if "差" in water:
            water_tips.append("保水性差，灌溉频次要高、单次水量要小。")
        elif water in ("强", "较强"):
            water_tips.append("保水性好，注意排水防渍，尤其雨季。")

        matched = soil_name in suitable
        if suitable:
            if matched:
                match_text = "「%s」是 %s 的适宜土壤之一，匹配良好。" % (soil_name, crop)
                match_level = "good"
            else:
                match_text = ("图谱中 %s 的适宜土壤为：%s；当前选择「%s」，"
                              "建议关注下面的水肥调整要点。" % (
                                  crop, "、".join(suitable[:6]), soil_name))
                match_level = "warn"
        else:
            match_text = "图谱中暂无 %s 的适宜土壤记录，以下按土壤本身特性给建议。" % crop
            match_level = "none"

        return {
            "soil": soil_name,
            "props": props,
            "texture": texture or "未标注",
            "texture_label": texture_label,
            # 酸碱度只看农户手填的值（ph_user 为 None 表示没填，页面就不显示这一项）
            "ph_user": ph_user,
            "ph_label": ph_label,
            "ph_level": ph_level,
            "fertility": fertility or "未标注",
            "water": water or "未标注",
            "area": props.get("主要分布") or "",
            "tips": tips,
            "ph_tips": ph_tips,
            "fert_tips": fert_tips,
            "water_tips": water_tips,
            "amendments": fixes,
            "suitable_soils": suitable,
            "matched": matched,
            "match_level": match_level,
            "match_text": match_text,
        }

    @staticmethod
    def _soil_fert_note(advice):
        """把土质折算成一句施肥调整建议（与施肥表一起展示）"""
        if not advice:
            return ""
        texture = str(advice.get("texture") or "")
        water = str(advice.get("water") or "")
        if not texture or texture == "未标注":
            return "该土壤暂无质地记录，施肥先按上面的要点执行；建议做一次测土，再按测土结果调整配方。"
        if any(k in texture for k in ("砂质", "砂壤", "砾石", "粗砂", "风沙")):
            return ("该土壤质地偏砂（{}）：保肥差，基肥建议分次施入，追肥「少量多次」，"
                    "单次用量减 20%~30%、间隔缩短 3~5 天。".format(texture))
        if any(k in texture for k in ("黏质", "黏壤", "重壤")):
            return ("该土壤质地偏黏（{}）：保肥但透气差，施肥可适当集中，忌一次过量；"
                    "雨后注意排水防沤根。".format(texture))
        if "差" in water:
            return "该土壤保水性差：追肥后及时小水勤灌，帮助养分溶解吸收。"
        return "该土壤质地与保水性适中，按上面的施肥要点执行即可。"

    # ==================================================== 宫格（物候提醒页首页）
    def stage_image(self, crop, stage_name):
        """该作物该生育期的实拍图 URL，没有则 None"""
        return stage_image_url(crop, stage_name)

    def season(self, crop):
        """作物播种季节（图谱属性），带缓存"""
        key = "season::" + str(crop)
        if key in self._cache:
            return self._cache[key]
        rows = self.conn._run(
            "MATCH (c:作物 {name:$c}) RETURN c.`播种季节` AS s", c=crop)
        val = (rows[0]["s"] if rows else "") or ""
        self._cache[key] = val
        return val

    def cell(self, crop, sow_date, today=None, soil=None):
        """
        把一个「作物 + 播种日期」压成宫格格子所需的数据包。
        物候还没开始（before_sow）时展示第一阶段，已过全生育期（done）时展示最后阶段，
        这样每格都有图可看，不会出现空白格子。
        """
        info = self.resolve(crop, sow_date, today)
        stages = info.get("stages") or []
        cell = {
            "crop": crop,
            "sow_date": info.get("sow_date"),
            "today": info.get("today"),
            "day_no": info.get("day_no"),
            "total_days": info.get("total_days"),
            "status": info.get("status") or "unknown",
            "status_text": info.get("message") or "",
            "percent": info.get("overall_percent") or 0,
            "stage": "", "stage_short": "", "stage_index": -1,
            "stage_day": None, "stage_days": None, "stage_count": len(stages),
            "days_to_next": info.get("days_to_next"),
            "next_stage": (info.get("next_stage") or {}).get("short") or "",
            "risk_count": 0, "top_risks": [],
            "image": None, "season": self.season(crop),
        }
        if info.get("status") == "growing":
            cur = info["stage"]
            cell.update(stage=cur["name"], stage_short=cur["short"],
                        stage_index=info["stage_index"], stage_day=info["stage_day"],
                        stage_days=cur["days"])
            risks = self.risks(crop, cur, info["stage_index"], stages)
            cell["risk_count"] = len(risks)
            cell["top_risks"] = [{"name": r["name"], "level": r["level"]}
                                 for r in risks[:3]]
        elif stages:
            cur = stages[0] if info.get("status") == "before_sow" else stages[-1]
            cell.update(stage=cur["name"], stage_short=cur["short"],
                        stage_index=stages.index(cur), stage_days=cur["days"])
        if cell["stage"]:
            cell["image"] = self.stage_image(crop, cell["stage"])
        return cell

    def plot_cells(self, plots, today=None):
        """我的地块 → 宫格格子（带跳转详情页的地址）"""
        out = []
        for p in plots or []:
            try:
                c = self.cell(p["crop"], p["sow_date"], today=today)
            except Exception as e:                    # 单块地出错不影响其它格子
                c = {"crop": p.get("crop"), "status": "unknown",
                     "status_text": str(e), "stage": "", "stage_short": "",
                     "percent": 0, "risk_count": 0, "top_risks": [],
                     "image": None, "season": "", "stage_count": 0}
            c.update(plot_id=p["id"], plot_name=p["plot_name"],
                     area_mu=p.get("area_mu"), region=p.get("region") or "",
                     plot_soil=p.get("soil") or "",
                     # 手填 pH 一起带给宫格，详情页才不会与建档时填的对不上
                     plot_ph=p.get("ph"),
                     url="/plot/%s" % p["id"])
            out.append(c)
        return out

    def hero(self, crop, info):
        """
        详情页顶部大图用：挑出该展示哪个阶段的实拍图
        生育中 → 当前阶段；未播种 → 第一阶段；已收获 → 最后阶段
        """
        stages = (info or {}).get("stages") or []
        name = ""
        if info.get("status") == "growing" and info.get("stage"):
            name = info["stage"]["name"]
        elif stages:
            name = stages[0]["name"] if info.get("status") == "before_sow" else stages[-1]["name"]
        return {"hero_stage": stage_short(name) if name else "",
                "hero_image": stage_image_url(crop, name) if name else None}

    # ==================================================== 指定生育期的资料包
    def stage_pack(self, crop, key, soil=None, cur_index=None, ph=None):
        """
        取「某一个指定生育期」的完整资料（农事 / 施肥 / 病虫害风险 / 土质调整）。

        用于物候详情页时间轴：点哪个方块就看哪个时期，不必非得是当前时期。
        key：阶段下标（0 开始）或阶段名（全名 / 短名都行，模糊匹配兜底）
        cur_index：当前实际所处阶段的下标，用于标出「这是当前阶段」
        ph：农户手填的土壤 pH（可选，只影响酸碱类建议）
        """
        stages = self.get_stages(crop)
        if not stages:
            return {"crop": crop, "error": "图谱里没有该作物的生育期数据"}

        idx = None
        raw = str(key).strip() if key is not None else ""
        if raw.isdigit():
            i = int(raw)
            if 0 <= i < len(stages):
                idx = i
        if idx is None:
            for i, st in enumerate(stages):
                if raw and raw in (st["name"], st["short"]):
                    idx = i
                    break
        if idx is None and raw:
            hit, score = self._locate(stages, raw)
            if hit is not None and score >= 2:       # 只在比较确定时才认，避免点错串台
                idx = hit
        if idx is None:
            return {"crop": crop, "error": "没有找到这个生育期"}

        stage = stages[idx]
        pack = {
            "crop": crop,
            "stage": stage,
            "stage_index": idx,
            "stage_count": len(stages),
            "is_current": (cur_index is not None and str(cur_index).isdigit()
                           and int(cur_index) == idx),
            "todo": stage["practices"],
            "fertilize": self.fertilize(crop, stage),
            "risks": self.risks(crop, stage, idx, stages),
            "image": self.stage_image(crop, stage["name"]),
            "start": stage["start"], "end": stage["end"], "days": stage["days"],
        }
        if idx + 1 < len(stages):
            pack["next_stage"] = stages[idx + 1]
        if idx > 0:
            pack["prev_stage"] = stages[idx - 1]
        if soil:
            advice = self.soil_advice(crop, soil, ph=ph)
            if advice:
                pack["soil"] = advice
                if pack["fertilize"]:
                    pack["fertilize"]["soil_note"] = self._soil_fert_note(advice)
        return pack

    # ==================================================== 汇总入口
    def reminder(self, crop, sow_date, today=None, soil=None, ph=None):
        """
        一次拿全：物候状态 + 土质建议 + 当期农事 + 施肥建议 + 病虫害风险 + 下阶段预警
        soil：地块土壤类型（可选，来自图谱的土壤节点名）
        ph  ：农户手填的土壤 pH（可选，只影响酸碱类建议）
        """
        info = self.resolve(crop, sow_date, today)
        info["crop"] = crop
        # 手填 pH 与"有没有选土质"无关：没选土质也应该显示农户自己填的值
        try:
            ph_num = round(float(ph), 2) if ph not in (None, "") else None
        except (TypeError, ValueError):
            ph_num = None
        info["ph_user"] = ph_num if (ph_num is not None and 0 <= ph_num <= 14) else None
        if soil:
            advice = self.soil_advice(crop, soil, ph=ph)
            if advice:
                info["soil"] = advice
        if info.get("status") != "growing":
            if info.get("status") == "before_sow":
                stages = info.get("stages") or self.get_stages(crop)
                if stages:
                    info["tips"] = [p for p in stages[0]["practices"]]
            return info

        stage, i, stages = info["stage"], info["stage_index"], info["stages"]
        info["icon"] = stage_icon(stage["name"])
        info["fertilize"] = self.fertilize(crop, stage)
        if info.get("soil") and info.get("fertilize"):
            info["fertilize"]["soil_note"] = self._soil_fert_note(info["soil"])
        info["risks"] = self.risks(crop, stage, i, stages)

        # 本期农事
        info["todo"] = stage["practices"]
        # 下阶段农事（提前准备）
        if info.get("next_stage"):
            info["next_todo"] = info["next_stage"]["practices"]
        # 下阶段风险预警
        if info.get("next_soon") and info["next_stage"] is not None:
            ni = stages.index(info["next_stage"])
            info["next_risks"] = self.risks(crop, info["next_stage"], ni, stages)
        return info

    # ==================================================== 单个阶段详情
    def stage_detail(self, crop, index, sow_date=None, soil=None, today=None, ph=None):
        """
        查询**任意一个**阶段的详情（不限于当前阶段），供时间轴点击查看。
        index 为阶段下标（0 起）；给了 sow_date 时会标出哪个是当前阶段。
        ph 为农户手填的土壤 pH（可选）—— 切阶段时土质区块要保持一致。
        """
        stages = self.get_stages(crop)
        if not stages:
            return None
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(stages):
            return None

        st = stages[index]
        cur_idx = None
        if sow_date:
            info = self.resolve(crop, sow_date, today)
            if info.get("status") == "growing":
                cur_idx = info.get("stage_index")

        fert = self.fertilize(crop, st)
        advice = self.soil_advice(crop, soil, ph=ph) if soil else None
        if advice and fert:
            fert["soil_note"] = self._soil_fert_note(advice)

        return {
            "crop": crop,
            "index": index,
            "total": len(stages),
            "name": st["name"],
            "short": st["short"],
            "days": st["days"],
            "start": st["start"],
            "end": st["end"],
            "day_range": "第 %d–%d 天" % (st["start"], st["end"]),
            "icon": stage_icon(st["name"]),
            "todo": st["practices"],
            "fertilize": fert,
            "risks": self.risks(crop, st, index, stages),
            "is_current": (cur_idx == index),
            "has_current": cur_idx is not None,
            "current_index": cur_idx,
        }

    def summary_line(self, crop, sow_date, today=None, soil=None, ph=None):
        """一行摘要 + 风险计数 + 土质匹配度，用于地块列表（地块多时不必逐个拉全量提醒）"""
        info = self.resolve(crop, sow_date, today)
        st = info.get("status")
        soil_info = {}
        if soil:
            sa = self.soil_advice(crop, soil, ph=ph)
            if sa:
                soil_info = {"soil": soil, "soil_match": sa["match_level"],
                             "soil_texture": sa["texture"],
                             "soil_ph_user": sa.get("ph_user")}
        if st == "growing":
            risks = self.risks(crop, info["stage"], info["stage_index"], info["stages"])
            out = {
                "status": "growing",
                "stage": info["stage"]["short"],
                "text": info["message"],
                "percent": info["overall_percent"],
                "risk_count": len(risks),
                "top_risks": [r["name"] for r in risks[:3]],
                "days_to_next": info.get("days_to_next"),
                "next_stage": (info["next_stage"]["short"] if info.get("next_stage") else ""),
                # 已种植时长 + 预计收获（地块可能是种了一段时间后才补录的）
                "elapsed_days": info.get("elapsed_days"),
                "elapsed_text": info.get("elapsed_text", ""),
                "harvest_date": info.get("harvest_date", ""),
                "harvest_md": info.get("harvest_md", ""),
                "days_to_harvest": info.get("days_to_harvest"),
            }
            out.update(soil_info)
            return out
        out = {"status": st, "stage": "", "text": info.get("message", ""),
               "percent": 0, "risk_count": 0, "top_risks": [],
               "days_to_next": None, "next_stage": "",
               "elapsed_days": info.get("elapsed_days"),
               "elapsed_text": info.get("elapsed_text", ""),
               "harvest_date": info.get("harvest_date", ""),
               "harvest_md": info.get("harvest_md", ""),
               "days_to_harvest": info.get("days_to_harvest")}
        out.update(soil_info)
        return out


if __name__ == "__main__":
    import json
    from core.neo4j_conn import Neo4jConnection
    import config

    conn = Neo4jConnection(config.NEO4J_URI, config.NEO4J_USER, config.NEO4J_PWD)
    eng = PhenologyEngine(conn)

    crops = eng.list_crops()
    print("有生育期数据的作物：%d 个" % len(crops))
    print("前 8 个：", "、".join(c["name"] for c in crops[:8]))

    # 小麦：假设 240 天前播种 → 应落在灌浆期附近
    today = datetime.date.today()
    cases = [
        ("小麦", (today - datetime.timedelta(days=200)).isoformat()),
        ("水稻", (today - datetime.timedelta(days=60)).isoformat()),
        ("玉米", (today - datetime.timedelta(days=45)).isoformat()),
        ("番茄", (today - datetime.timedelta(days=10)).isoformat()),
    ]
    for crop, sow in cases:
        r = eng.reminder(crop, sow)
        print("=" * 70)
        print("%s 播种日 %s -> %s" % (crop, sow, r.get("message")))
        if r.get("status") == "growing":
            print("  农事：", "、".join(p["name"] for p in r.get("todo") or []))
            f = r.get("fertilize") or {}
            print("  施肥：%s —— %s" % (f.get("title"), f.get("tip")))
            print("  肥料：", "、".join(x["name"] for x in f.get("fertilizers") or []))
            risks = r.get("risks") or []
            print("  风险 %d 项：" % len(risks),
                  "、".join("%s(%s)" % (x["name"], x["level"] or "-") for x in risks[:5]))
            if r.get("next_soon"):
                print("  预警：%d 天后进入【%s】" % (r["days_to_next"], r["next_stage"]["short"]))
                print("  下期风险：", "、".join(x["name"] for x in (r.get("next_risks") or [])[:4]))
    conn.close()
