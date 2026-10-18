#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
农作物经济效益测算（EconStore）
================================
农户最实际的三个问题：**能卖多少钱、要投多少钱、最后剩多少**。
这里把一组"投入产出参数"存下来并统一算账：

    亩收入   = 亩产(kg) × 收购价(元/kg)
    亩成本   = 种子 + 化肥 + 农药 + 机械 + 人工 + 灌溉 + 其他（都是元/亩）
    亩净收益 = 亩收入 − 亩成本
    总收入 / 总成本 / 总净收益 = 上面的亩均 × 面积
    投入产出比 = 亩收入 ÷ 亩成本
    保本收购价 = 亩成本 ÷ 亩产   ← "价格跌到多少就白干"
    保本亩产   = 亩成本 ÷ 收购价 ← "产量低于多少就亏"

成本一律按**元/亩**填，面积只做等比例放大 —— 免得农户在"这块地一共花了多少"
和"一亩地花多少"之间来回换算。几个作物摆在一起比时，也统一用**亩均净收益**排序
（地块大小不一样也公平）。

与 plot_store / task_store 同款约定：
* 表在**首次使用时自动创建**（CREATE TABLE IF NOT EXISTS），不依赖手工 SQL；
* MySQL 不可用时**整体静默降级**（读返回空、写返回失败原因），绝不抛异常；
* 数据按 `username` 隔离，别人的记录读不到、改不了。
"""
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402

# 七项成本，都是"元/亩"。顺序即页面上的填写顺序。
COST_FIELDS = [
    ("cost_seed", "种子种苗"),
    ("cost_fertilizer", "化肥"),
    ("cost_pesticide", "农药"),
    ("cost_machine", "机械作业"),
    ("cost_labor", "人工"),
    ("cost_irrigation", "灌溉"),
    ("cost_other", "其他"),
]
COST_COLS = [c for c, _ in COST_FIELDS]

DDL = """
CREATE TABLE IF NOT EXISTS `crop_econ` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `crop` varchar(64) NOT NULL COMMENT '作物',
  `area_mu` decimal(8,2) DEFAULT NULL COMMENT '面积（亩）',
  `yield_per_mu` decimal(10,2) NOT NULL COMMENT '亩产（公斤/亩）',
  `price` decimal(10,2) NOT NULL COMMENT '收购价（元/公斤）',
  `cost_seed` decimal(10,2) DEFAULT 0 COMMENT '种子种苗（元/亩）',
  `cost_fertilizer` decimal(10,2) DEFAULT 0 COMMENT '化肥（元/亩）',
  `cost_pesticide` decimal(10,2) DEFAULT 0 COMMENT '农药（元/亩）',
  `cost_machine` decimal(10,2) DEFAULT 0 COMMENT '机械作业（元/亩）',
  `cost_labor` decimal(10,2) DEFAULT 0 COMMENT '人工（元/亩）',
  `cost_irrigation` decimal(10,2) DEFAULT 0 COMMENT '灌溉（元/亩）',
  `cost_other` decimal(10,2) DEFAULT 0 COMMENT '其他（元/亩）',
  `note` varchar(255) DEFAULT '' COMMENT '备注（品种、年份、销路等）',
  `plot_id` int DEFAULT NULL COMMENT '关联地块 user_plot.id（可为空）',
  `plot_name` varchar(64) DEFAULT '' COMMENT '地块名快照：地块改名/删除后，历史测算仍看得出是哪块地',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_user` (`username`),
  KEY `idx_plot` (`plot_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='农作物经济效益测算表'
"""

# 老库补列（MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，靠错误码 1060 判定"已经有了"）
MIGRATE_CROP_ECON = [
    "ALTER TABLE `crop_econ` ADD COLUMN `plot_id` int DEFAULT NULL",
    "ALTER TABLE `crop_econ` ADD COLUMN `plot_name` varchar(64) DEFAULT ''",
]

# 地块被删除时**不清测算记录**，只解绑（plot_id 置空、留下地块名快照）。
# 理由：测算记录是农户自己填的经营数据，删块地不该把他算过的账一起抹掉。
UNLINK_PLOT_SQL = "UPDATE crop_econ SET plot_id = NULL WHERE username = %s AND plot_id = %s"


def _num(v, default=0.0):
    """把表单里的字符串转成数字；空值/非法值一律按默认值处理（不抛异常）"""
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return default if x != x else x          # NaN
    except (TypeError, ValueError):
        return default


def compute(row):
    """
    给一条参数算出全部经济指标（纯函数，方便单测）。
    row 里成本字段缺的按 0 处理；面积缺省按 1 亩算，并在 area_assumed 里标出来。
    """
    y = _num(row.get("yield_per_mu"))
    price = _num(row.get("price"))
    area_raw = row.get("area_mu")
    area_assumed = (area_raw in (None, ""))
    area = _num(area_raw, 1.0) or 1.0

    per_mu_income = y * price
    per_mu_cost = sum(_num(row.get(k)) for k in COST_COLS)
    per_mu_net = per_mu_income - per_mu_cost

    out = dict(row)
    out.update({
        "area_mu": area,
        "area_assumed": area_assumed,
        "yield_per_mu": y,
        "price": price,
        "per_mu_income": round(per_mu_income, 2),
        "per_mu_cost": round(per_mu_cost, 2),
        "per_mu_net": round(per_mu_net, 2),
        "total_income": round(per_mu_income * area, 2),
        "total_cost": round(per_mu_cost * area, 2),
        "total_net": round(per_mu_net * area, 2),
        # 投入产出比：1 元成本能换回多少元收入（>1 才不亏）
        "roi": round(per_mu_income / per_mu_cost, 2) if per_mu_cost > 0 else None,
        # 保本线：这两个数最实用 —— "价格/产量跌到哪里就白干"
        "break_even_price": round(per_mu_cost / y, 2) if y > 0 else None,
        "break_even_yield": round(per_mu_cost / price, 1) if price > 0 else None,
    })
    return out


def summarize(items):
    """概览：几个方案、谁最划算、平均亩均净收益、有没有亏的"""
    out = {"count": len(items), "best": None, "worst": None,
           "avg_per_mu_net": 0.0, "loss_count": 0}
    if not items:
        return out
    ranked = sorted(items, key=lambda x: x.get("per_mu_net", 0), reverse=True)
    out["best"] = {"crop": ranked[0].get("crop", ""),
                   "per_mu_net": ranked[0].get("per_mu_net", 0),
                   "id": ranked[0].get("id")}
    out["worst"] = {"crop": ranked[-1].get("crop", ""),
                    "per_mu_net": ranked[-1].get("per_mu_net", 0),
                    "id": ranked[-1].get("id")}
    out["avg_per_mu_net"] = round(
        sum(x.get("per_mu_net", 0) for x in items) / len(items), 2)
    out["loss_count"] = sum(1 for x in items if x.get("per_mu_net", 0) < 0)
    return out


class EconStore:
    def __init__(self, mysql_config=None):
        self.cfg = dict(mysql_config or config.MYSQL_CONFIG)
        self.available = True
        self.error = ""
        self._ready = False

    # ------------------------------------------------------------ 基础
    def _connect(self):
        # 统一入口：MySQL（本机开发）或 SQLite（免安装交付版），由 config.DB_BACKEND 决定
        from core import db
        return db.connect(self.cfg)

    def ensure_table(self):
        if self._ready or not self.available:
            return self.available
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(DDL)
                    # 建完立刻补列：已经跑过老版本的表不会被 DDL 改到
                    for sql in MIGRATE_CROP_ECON:
                        try:
                            cur.execute(sql)
                        except Exception as e:
                            # 1060 = duplicate column（已经有了），其它错误也不阻断使用
                            if getattr(e, "args", [None])[0] != 1060:
                                print("WARN: 测算表补列失败（可忽略）：%s" % e)
                conn.commit()
                self._ready = True
            finally:
                conn.close()
        except Exception as e:
            self.available = False
            self.error = str(e)
            print("WARN: 经济效益测算不可用（MySQL 未连接或权限不足）：%s" % e)
        return self.available

    def _run(self, sql, params=None, fetch=True, rowcount=False):
        """
        查询失败 -> []；写失败 -> None（调用处用 `is not None` 判断成功）

        rowcount=True 时写操作返回**受影响行数**（int）而不是 lastrowid ——
        MySQL 的 DELETE / UPDATE 命中 0 行**不报错**，只有靠行数才能区分
        "改掉了"和"压根没这条记录"。⚠ 行数必须在 commit / 关游标之前读。
        """
        if not self.ensure_table():
            return [] if fetch else None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch:
                    return list(cur.fetchall() or [])
                last, n = cur.lastrowid, cur.rowcount
                conn.commit()
                return n if rowcount else last
        except Exception as e:
            print("WARN: 经济效益操作失败：%s" % e)
            return [] if fetch else None
        finally:
            conn.close()

    # ------------------------------------------------------------ 读
    def list_econ(self, username):
        """我的全部方案（按亩均净收益倒序，最划算的排前面）"""
        rows = self._run("""
            SELECT id, crop, area_mu, yield_per_mu, price,
                   cost_seed, cost_fertilizer, cost_pesticide, cost_machine,
                   cost_labor, cost_irrigation, cost_other, note,
                   plot_id, plot_name, create_time, update_time
            FROM crop_econ WHERE username = %s
        """, (username,))
        items = []
        for r in rows:
            raw = {"id": r[0], "crop": r[1] or "", "area_mu": r[2],
                   "yield_per_mu": r[3], "price": r[4],
                   "note": r[12] or "",
                   "plot_id": r[13],
                   "plot_name": r[14] or "",
                   "created": r[15].strftime("%Y-%m-%d") if r[15] else "",
                   "updated": r[16].strftime("%Y-%m-%d %H:%M") if r[16] else ""}
            for i, col in enumerate(COST_COLS):
                raw[col] = float(r[5 + i] or 0)
            items.append(compute(raw))
        items.sort(key=lambda x: x.get("per_mu_net", 0), reverse=True)
        return items

    def latest_by_plot(self, username, plot_ids=None):
        """
        每个地块**最近一条**测算，供地块档案回显：{plot_id: 摘要}

        只挑「还绑着地块」的记录（plot_id IS NOT NULL）。同一块地算过好几次时取最后改的那条
        （按 update_time 倒序，SQL 里排好、Python 侧遇到已见过的 plot_id 就跳过）。
        ⚠ 参数化占位符用 `{IN}` 再 replace —— 直接 `%` 格式化会把后面拼进来的 `%s` 一起吃掉。
        """
        out = {}
        ids = [int(p) for p in (plot_ids or []) if str(p).strip().isdigit()]
        if not ids:
            return out
        ph = ", ".join(["%s"] * len(ids))
        # ⚠ 表里没有 per_mu_cost / per_mu_net 这两个列（那是 compute() 在 Python 侧算的），
        #   所以这里取七项成本自己加，别去 SELECT 不存在的列
        rows = self._run("""
            SELECT plot_id, id, crop, area_mu, yield_per_mu, price,
                   cost_seed, cost_fertilizer, cost_pesticide, cost_machine,
                   cost_labor, cost_irrigation, cost_other, plot_name, update_time
            FROM crop_econ
            WHERE username = %s AND plot_id IN ({IN})
            ORDER BY update_time DESC, id DESC
        """.replace("{IN}", ph), (username,) + tuple(ids))
        for r in rows:
            pid = r[0]
            if pid in out:
                continue
            # 列序：0 plot_id, 1 id, 2 crop, 3 area_mu, 4 yield_per_mu, 5 price,
            #      6..12 七项成本, 13 plot_name, 14 update_time
            # ⚠ 面积是 r[3] 不是 r[2]（r[2] 是作物名）—— 写错会被 _num 静默当成 1 亩，
            #   总净收益就按 1 亩算了（2026-09-28 实测抓到）
            y, price = _num(r[4]), _num(r[5])
            per_mu_cost = sum(_num(r[6 + i]) for i in range(len(COST_COLS)))
            area = _num(r[3], 1.0) or 1.0
            per_mu_net = round(y * price - per_mu_cost, 2)
            out[pid] = {
                "id": r[1], "crop": r[2] or "", "area_mu": area,
                "yield_per_mu": y, "price": price,
                "per_mu_cost": round(per_mu_cost, 2),
                "per_mu_net": per_mu_net,
                "total_net": round(per_mu_net * area, 2),
                "break_even_price": round(per_mu_cost / y, 2) if y > 0 else None,
                "break_even_yield": round(per_mu_cost / price, 1) if price > 0 else None,
                "plot_name": r[13] or "",
                "updated": r[14].strftime("%Y-%m-%d %H:%M") if r[14] else "",
            }
        return out

    def get(self, username, eid):
        for it in self.list_econ(username):
            if str(it.get("id")) == str(eid):
                return it
        return None

    # ------------------------------------------------------------ 写
    def save(self, username, data, eid=None):
        """
        新增（eid 为空）或更新（eid 有值）。返回 (ok, message, id)。
        必填：作物 + 亩产 + 收购价；成本留空按 0 算。
        """
        crop = str(data.get("crop") or "").strip()
        if not crop:
            return False, "请选择作物", None
        y = _num(data.get("yield_per_mu"))
        price = _num(data.get("price"))
        if y <= 0:
            return False, "预期亩产要大于 0（公斤/亩）", None
        if price <= 0:
            return False, "收购价要大于 0（元/公斤）", None
        area = data.get("area_mu")
        area = None if area in (None, "") else _num(area)
        if area is not None and area <= 0:
            return False, "面积要大于 0（留空则按 1 亩估算）", None
        costs = [_num(data.get(k)) for k in COST_COLS]
        if any(c < 0 for c in costs):
            return False, "成本不能为负数", None
        note = str(data.get("note") or "").strip()[:255]
        # 地块绑定：plot_id 由**调用方（app.py）校验过归属**后传进来，这里只管存。
        # 不存在的地块 id 会指向空，所以约定：app 层查不到就传 None。
        try:
            plot_id = int(data.get("plot_id")) if data.get("plot_id") not in (None, "", "0") else None
        except (TypeError, ValueError):
            plot_id = None
        plot_name = str(data.get("plot_name") or "").strip()[:64] if plot_id else ""

        if eid:
            # 更新前先确认这条记录是自己的（归属边界）
            if self.get(username, eid) is None:
                return False, "记录不存在或不属于你", None
            sql = ("""
                UPDATE crop_econ SET crop=%s, area_mu=%s, yield_per_mu=%s, price=%s,
                  cost_seed=%s, cost_fertilizer=%s, cost_pesticide=%s, cost_machine=%s,
                  cost_labor=%s, cost_irrigation=%s, cost_other=%s, note=%s,
                  plot_id=%s, plot_name=%s
                WHERE id=%s AND username=%s
            """)
            params = (crop, area, y, price) + tuple(costs) + (note, plot_id, plot_name,
                                                              int(eid), username)
        else:
            sql = ("""
                INSERT INTO crop_econ
                  (username, crop, area_mu, yield_per_mu, price,
                   cost_seed, cost_fertilizer, cost_pesticide, cost_machine,
                   cost_labor, cost_irrigation, cost_other, note, plot_id, plot_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """)
            params = (username, crop, area, y, price) + tuple(costs) + (note, plot_id, plot_name)
        rid = self._run(sql, params, fetch=False)
        if rid is None:
            return False, "数据库不可用，暂时存不下来", None
        return True, ("已更新「%s」的测算" % crop) if eid else ("已保存「%s」的测算" % crop), \
            (int(eid) if eid else rid)

    def delete(self, username, eid):
        """
        删除自己的某条（越权返回 False）。
        ⚠ 用行数判断：DELETE 命中 0 行在 MySQL 里不算错误，
          只判 `is not None` 会让它对"不存在的记录"也报成功。
        """
        n = self._run("DELETE FROM crop_econ WHERE id=%s AND username=%s",
                      (int(eid), username), fetch=False, rowcount=True)
        return isinstance(n, int) and n > 0

    def unlink_plot(self, username, plot_id):
        """
        地块被删除时调用：**只解绑、不删记录**（plot_id 置空，plot_name 快照留着）。
        返回解绑条数；失败返回 -1。删地块时调用，失败只告警、不影响地块已删除的事实。
        """
        try:
            pid = int(plot_id)
        except (TypeError, ValueError):
            return -1
        n = self._run(UNLINK_PLOT_SQL, (username, pid), fetch=False, rowcount=True)
        return n if isinstance(n, int) else -1


# 与 plot_store / task_store 同款：模块级单例
_STORE = None


def get_econ_store():
    global _STORE
    if _STORE is None:
        _STORE = EconStore()
    return _STORE
