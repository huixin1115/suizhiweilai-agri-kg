#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
农作物经济效益测算引擎（EconEngine）
=====================================
回答农户最实际的那个问题：**这块地，种哪个更赚钱？**

模型：
    亩均成本  = 种子 + 化肥 + 农药 + 农膜 + 灌溉 + 机械 + 人工 + 地租 + 其他
    亩均产值  = 亩产(kg) × 收购价(元/kg)
    亩均净收益 = 亩均产值 - 亩均成本
    总净收益  = 亩均净收益 × 面积(亩)
    成本利润率 = 净收益 / 成本
    投入产出比 = 产值 / 成本
    保本产量  = 成本 / 单价      （低于这个产量就亏）
    保本价格  = 成本 / 亩产      （价格跌到这个就不赚）

数据来源：
    · 亩产优先取知识图谱的『平均亩产kg』（52 个作物齐全）
    · 收购价与各项成本取 data/econ_ref.json —— **参考值，非实时行情**，
      目的是给农户一个可编辑的起点，不是替农户做决定。

纯算术，结果确定、可复现，不调用大模型。
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

REF_PATH = os.path.join(BASE, "data", "econ_ref.json")

# 成本项顺序（页面上的排列顺序，也是分组的依据）
COST_ORDER = ["seed", "fertilizer", "pesticide", "film",
              "irrigation", "machine", "labor", "rent", "other"]

# 成本分组：用于在页面上按"物资 / 机械 / 人工"归类展示
COST_GROUPS = [
    ("物资投入", ["seed", "fertilizer", "pesticide", "film"]),
    ("机械与灌溉", ["irrigation", "machine"]),
    ("人工与地租", ["labor", "rent"]),
    ("其他", ["other"]),
]

# 图谱里没有写在参考库的作物，按作物名关键词猜类型
TYPE_HINTS = [
    (("稻", "麦", "米", "玉米", "豆", "粱", "谷", "荞", "薯"), "粮食作物"),
    (("菜", "葱", "蒜", "姜", "茄", "瓜", "椒", "萝卜", "菠菜", "芹", "花菜"), "蔬菜"),
    (("果", "桃", "梨", "橘", "柑", "荔", "莓", "蕉", "柚", "枣", "杏", "李", "梅"), "果树"),
    (("参", "七", "枸杞", "黄", "贝母", "天麻", "灵芝"), "药材"),
    (("花", "麻", "葵"), "油料作物"),
]


def _num(v, default=0.0):
    """安全转数字：空串 / None / 乱输 都不炸，回落默认值"""
    if v is None or v == "":
        return default
    try:
        f = float(str(v).strip().replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):      # NaN / inf
        return default
    return f


class EconEngine:
    """经济效益测算：单方案算账 + 多方案对比 + 敏感度分析"""

    def __init__(self, neo4j_conn=None, ref_path=None):
        self.conn = neo4j_conn
        self.ref_path = ref_path or REF_PATH
        self.ref_data = self._load_ref()
        self._crop_cache = None

    # ------------------------------------------------------------ 参考库
    def _load_ref(self):
        try:
            with io.open(self.ref_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("WARN: 经济效益参考库读取失败，改用空库：%s" % e)
            return {"crops": {}, "cost_templates": {}, "cost_labels": {}}

    @property
    def cost_labels(self):
        labels = self.ref_data.get("cost_labels") or {}
        return {k: labels.get(k, k) for k in COST_ORDER}

    def sales_note(self, crop):
        return (self.ref_data.get("sales_notes") or {}).get(crop, "")

    def rent_hint(self, crop):
        """租地经营时该作物类型的参考租金（元/亩）；默认口径不计地租"""
        ctype = self.ref(crop).get("type")
        return _num((self.ref_data.get("rent_hint") or {}).get(ctype), 0)

    @property
    def rent_note(self):
        return self.ref_data.get("_rent_note", "")

    def guess_type(self, crop):
        """参考库里没有的作物，按名字猜一个类型，至少让成本模板有着落"""
        name = str(crop or "")
        for kws, t in TYPE_HINTS:
            if any(k in name for k in kws):
                return t
        return "粮食作物"

    # ------------------------------------------------------------ 图谱作物
    def graph_crops(self):
        """从知识图谱取作物清单 + 平均亩产（图谱不可用时返回空列表）"""
        if self._crop_cache is not None:
            return self._crop_cache
        rows = []
        if self.conn is not None:
            try:
                rows = self.conn._run("""
                    MATCH (c:作物)
                    OPTIONAL MATCH (c)-[:REL {type:'主产区'}]->(r:地域)
                    RETURN c.name AS name,
                           c.`平均亩产kg` AS yield_kg,
                           c.`播种季节` AS season,
                           c.`收获季节` AS harvest,
                           collect(DISTINCT r.name)[0..3] AS regions
                    ORDER BY c.name
                """) or []
            except Exception as e:
                print("WARN: 图谱作物读取失败，仅用参考库：%s" % e)
                rows = []
        self._crop_cache = rows
        return rows

    def crops(self):
        """
        返回可供农户选择的作物清单，每项带好参考值（价格/成本/亩产）。
        图谱里有的作物全部包含；图谱里没有但参考库有的（如 茶叶）也补上。
        """
        ref_crops = self.ref_data.get("crops") or {}
        seen, out = set(), []

        for r in self.graph_crops():
            name = r.get("name")
            if not name:
                continue
            seen.add(name)
            info = ref_crops.get(name) or {}
            g_yield = _num(r.get("yield_kg"))
            out.append({
                "crop": name,
                "type": info.get("type") or self.guess_type(name),
                "price": info.get("price"),
                "yield_kg": g_yield or info.get("yield_kg"),
                "yield_from_graph": bool(g_yield),
                "season": r.get("season") or "",
                "harvest": r.get("harvest") or "",
                "regions": [x for x in (r.get("regions") or []) if x],
                "in_ref": bool(info),
            })

        for name, info in ref_crops.items():
            if name in seen:
                continue
            out.append({
                "crop": name,
                "type": info.get("type") or self.guess_type(name),
                "price": info.get("price"),
                "yield_kg": info.get("yield_kg"),
                "yield_from_graph": False,
                "season": "", "harvest": "", "regions": [],
                "in_ref": True,
            })
        return out

    # ------------------------------------------------------------ 参考值
    def ref(self, crop):
        """
        取某个作物的一整套参考值（价格 / 亩产 / 各项成本）。
        参考库里没有的作物，按猜出的类型套用成本模板。
        """
        name = str(crop or "").strip()
        info = (self.ref_data.get("crops") or {}).get(name) or {}
        ctype = info.get("type") or self.guess_type(name)
        tpl = dict((self.ref_data.get("cost_templates") or {}).get(ctype) or {})
        tpl.update(info.get("cost") or {})            # 作物特有覆盖
        costs = {k: tpl.get(k, 0) for k in COST_ORDER}

        g_yield = 0.0
        for r in self.graph_crops():
            if r.get("name") == name:
                g_yield = _num(r.get("yield_kg"))
                break

        return {
            "crop": name,
            "type": ctype,
            "price": info.get("price"),
            "yield_kg": g_yield or info.get("yield_kg"),
            "yield_from_graph": bool(g_yield),
            "costs": costs,
            "cost_total": sum(costs.values()),
            "rent_hint": _num((self.ref_data.get("rent_hint") or {}).get(ctype), 0),
            "in_ref": bool(info),
            "note": self.sales_note(name),
        }

    # ------------------------------------------------------------ 单方案测算
    def compute(self, item):
        """
        一个方案的完整账本。
        item = {crop, area_mu, yield_kg, price, costs:{key:值}}
        未提供的字段回落参考值。
        """
        crop = str(item.get("crop") or "").strip()
        r = self.ref(crop)

        area = _num(item.get("area_mu"), 1.0)
        if area <= 0:
            area = 1.0
        yld = _num(item.get("yield_kg"), _num(r.get("yield_kg")))
        price = _num(item.get("price"), _num(r.get("price")))

        raw_costs = item.get("costs")
        if not isinstance(raw_costs, dict) or not raw_costs:
            raw_costs = r.get("costs") or {}
        costs = {k: _num(raw_costs.get(k)) for k in COST_ORDER}

        cost_per_mu = sum(costs.values())
        revenue_per_mu = yld * price
        profit_per_mu = revenue_per_mu - cost_per_mu

        total_cost = cost_per_mu * area
        total_revenue = revenue_per_mu * area
        total_profit = profit_per_mu * area

        profit_rate = (profit_per_mu / cost_per_mu * 100) if cost_per_mu else None
        io_ratio = (revenue_per_mu / cost_per_mu) if cost_per_mu else None

        # 保本点：产量掉到多少 / 价格跌到多少 就不赚钱
        be_yield = (cost_per_mu / price) if price > 0 else None
        be_price = (cost_per_mu / yld) if yld > 0 else None
        margin_price = ((price - be_price) / price * 100) if (be_price is not None and price > 0) else None

        # 成本分组汇总（页面上按"物资/机械/人工"归类）
        groups = []
        for gname, keys in COST_GROUPS:
            gv = sum(costs.get(k, 0) for k in keys)
            if gv or gname != "其他":
                groups.append({
                    "name": gname,
                    "value": round(gv, 2),
                    "percent": round(gv / cost_per_mu * 100, 1) if cost_per_mu else 0,
                })

        return {
            "crop": crop,
            "type": r.get("type"),
            "area_mu": round(area, 2),
            "yield_kg": round(yld, 2),
            "price": round(price, 4),
            "yield_from_graph": r.get("yield_from_graph"),
            "in_ref": r.get("in_ref"),
            "ref_note": r.get("note") or "",
            "costs": costs,
            "cost_labels": self.cost_labels,
            "cost_groups": groups,
            "cost_per_mu": round(cost_per_mu, 2),
            "revenue_per_mu": round(revenue_per_mu, 2),
            "profit_per_mu": round(profit_per_mu, 2),
            "total_cost": round(total_cost, 2),
            "total_revenue": round(total_revenue, 2),
            "total_profit": round(total_profit, 2),
            "profit_rate": round(profit_rate, 1) if profit_rate is not None else None,
            "io_ratio": round(io_ratio, 2) if io_ratio is not None else None,
            "break_even_yield": round(be_yield, 1) if be_yield is not None else None,
            "break_even_price": round(be_price, 3) if be_price is not None else None,
            "price_margin": round(margin_price, 1) if margin_price is not None else None,
            "profitable": profit_per_mu > 0,
        }

    # ------------------------------------------------------------ 多方案对比
    def compare(self, items, base_area=None, top_n=None):
        """
        多作物并排对比，按总净收益排序，给出推荐。
        base_area：统一面积（农户常问"同样 10 亩地种哪个好"）。
        """
        plans = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            it = dict(it)
            if base_area:
                it["area_mu"] = base_area
            if not str(it.get("crop") or "").strip():
                continue
            plans.append(self.compute(it))

        plans.sort(key=lambda p: p["total_profit"], reverse=True)
        for i, p in enumerate(plans, 1):
            p["rank"] = i

        result = {"plans": plans, "count": len(plans)}
        if not plans:
            return result

        best, worst = plans[0], plans[-1]
        result["best"] = {
            "crop": best["crop"],
            "total_profit": best["total_profit"],
            "profit_per_mu": best["profit_per_mu"],
            "profit_rate": best["profit_rate"],
        }
        result["worst"] = {
            "crop": worst["crop"],
            "total_profit": worst["total_profit"],
        }
        if len(plans) > 1:
            result["gap_total"] = round(best["total_profit"] - worst["total_profit"], 2)
            result["gap_per_mu"] = round(best["profit_per_mu"] - worst["profit_per_mu"], 2)
        result["any_loss"] = any(p["profit_per_mu"] < 0 for p in plans)
        return result

    # ------------------------------------------------------------ 敏感度
    def sensitivity(self, item, steps=(-0.2, -0.1, 0.1, 0.2)):
        """
        价格与产量各自上下浮动时，净收益怎么变。
        农产品价格波动大，这一栏是给农户判断"能不能扛"用的。
        """
        base = self.compute(item)
        base_price, base_yield = base["price"], base["yield_kg"]

        def series(axis):
            out = []
            for s in steps:
                it = dict(item)
                if axis == "price":
                    it["price"] = base_price * (1 + s)
                    it["yield_kg"] = base_yield
                else:
                    it["yield_kg"] = base_yield * (1 + s)
                    it["price"] = base_price
                it["costs"] = dict(item.get("costs") or base["costs"])
                c = self.compute(it)
                out.append({
                    "step": round(s * 100),
                    "value": round(base_price * (1 + s), 3) if axis == "price" else round(base_yield * (1 + s), 1),
                    "profit_per_mu": c["profit_per_mu"],
                    "total_profit": c["total_profit"],
                    "profitable": c["profitable"],
                })
            return out

        return {
            "base": base,
            "price": series("price"),
            "yield": series("yield"),
            "break_even_price": base["break_even_price"],
            "break_even_yield": base["break_even_yield"],
        }
