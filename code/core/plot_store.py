#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地块档案存取（PlotStore）
=========================
把"我家的地"记下来，下次打开就能直接看到"今天该干什么"。

表结构在首次使用时自动创建（CREATE TABLE IF NOT EXISTS），
不依赖手工执行 SQL；MySQL 不可用时整体降级为"无档案模式"，
页面仍可用手动选作物 + 选播种日期的方式获得提醒。
"""
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402
from core.db_util import error_text               # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS `user_plot` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `plot_name` varchar(64) NOT NULL COMMENT '地块名称',
  `crop` varchar(64) NOT NULL COMMENT '种植作物',
  `sow_date` date NOT NULL COMMENT '播种日期',
  `area_mu` decimal(8,2) DEFAULT NULL COMMENT '面积（亩）',
  `region` varchar(64) DEFAULT '' COMMENT '所在地区',
  `soil` varchar(64) DEFAULT '' COMMENT '土壤类型',
  `ph` decimal(4,2) DEFAULT NULL COMMENT '土壤 pH（农户自己填的，选填）',
  `note` varchar(255) DEFAULT '' COMMENT '备注',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_plot` (`username`, `plot_name`),
  KEY `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户地块档案表'
"""

# 老库补列：MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，
# 只能执行一次、靠错误码 1060（duplicate column）判定"已经有了"
MIGRATE = [
    "ALTER TABLE `user_plot` ADD COLUMN `ph` decimal(4,2) DEFAULT NULL "
    "COMMENT '土壤 pH（农户自己填的，选填）'",
]

PH_MIN, PH_MAX = 0.0, 14.0


def parse_ph(value):
    """
    把前端传来的 pH 归一化成 float 或 None。
    返回 (值, 错误信息)：错误信息非空时调用方应拒绝保存。
    —— 之所以在这里校验而不是只信前端：手填数字最容易越界（比如把 65 当成 6.5）。
    """
    txt = str(value if value is not None else "").strip()
    if not txt:
        return None, ""
    try:
        v = float(txt)
    except (TypeError, ValueError):
        return None, "pH 请填数字，例如 6.5"
    if v < PH_MIN or v > PH_MAX:
        return None, "pH 只能在 %g ~ %g 之间" % (PH_MIN, PH_MAX)
    return round(v, 2), ""


class PlotStore:
    def __init__(self, mysql_config=None):
        self.cfg = dict(mysql_config or config.MYSQL_CONFIG)
        self.available = True
        self.error = ""
        self._ready = False
        self.last_task_cleaned = 0      # 上次删除地块级联清掉的勾选条数
        self.last_econ_unlinked = 0     # 上次删除地块级联解绑的测算条数

    # ------------------------------------------------------------ 基础
    def _connect(self):
        # 统一入口：MySQL（本机开发）或 SQLite（免安装交付版），由 config.DB_BACKEND 决定
        from core import db
        return db.connect(self.cfg)

    def ensure_table(self):
        """首次使用时自动建表（并给老表补 ph 列）；失败则标记不可用（不抛异常，保证页面能用）"""
        if self._ready or not self.available:
            return self.available
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(DDL)
                    for sql in MIGRATE:
                        try:
                            cur.execute(sql)
                        except Exception as e:
                            if getattr(e, "args", [None])[0] != 1060:
                                print("WARN: 地块表补列失败（可忽略）：%s" % e)
                conn.commit()
                self._ready = True
            finally:
                conn.close()
        except Exception as e:
            self.available = False
            self.error = error_text(self.cfg, e, "地块档案")
            print("WARN: 地块档案不可用（MySQL 未连接或权限不足）：%s" % e)
        return self.available

    def _run(self, sql, params=None, fetch=True, rowcount=False):
        """
        统一出口。返回值语义（与 task_store / history_store / feedback_store 一致）：
            查询成功 -> [行, ...]（可能为空）   查询失败 -> []
            写成功   -> True                   写失败   -> None

        ⚠ 原来写失败返 `0`、写成功返 `[]` —— 两个都是 falsy，外面**根本分不出来**，
          这正是"保存地块失败了却回 ok=true 已保存"的根因（2026-09-28 改掉）。

        rowcount=True 时写操作返回「受影响行数」（int）而不是 True ——
        DELETE 命中 0 行在 MySQL 里**不算错误**，只有靠行数才能区分"删掉了"和"压根没这块地"。
        ⚠ 行数必须在 commit / 关游标之前读。
        """
        if not self.ensure_table():
            return [] if fetch else None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch:
                    return list(cur.fetchall() or [])
                n = cur.rowcount
                conn.commit()
                return n if rowcount else True
        except Exception as e:
            print("WARN: 地块档案操作失败：%s" % e)
            self.error = error_text(self.cfg, e, "地块档案")
            return [] if fetch else None
        finally:
            conn.close()

    # ------------------------------------------------------------ 增删改查
    def list_plots(self, username):
        rows = self._run("""
            SELECT id, plot_name, crop, sow_date, area_mu, region, soil, ph, note
            FROM user_plot WHERE username = %s ORDER BY update_time DESC
        """, (username,))
        out = []
        for r in rows:
            out.append({
                "id": r[0],
                "plot_name": r[1],
                "crop": r[2],
                "sow_date": r[3].isoformat() if isinstance(r[3], (datetime.date, datetime.datetime)) else str(r[3]),
                "area_mu": float(r[4]) if r[4] is not None else None,
                "region": r[5] or "",
                "soil": r[6] or "",
                # 农户自己填的 pH：没填就是 None（前端据此决定显不显示）
                "ph": float(r[7]) if r[7] is not None else None,
                "note": r[8] or "",
            })
        return out

    def save_plot(self, username, plot_name, crop, sow_date, area_mu=None,
                  region="", soil="", note="", ph=None):
        """
        新增或按地块名覆盖更新（ph 传空字符串/None 表示清空为未填）。
        ⚠ 必须返回真实结果：成功 True / 失败 False。原来无条件 `return True`，
          数据库写失败（日期非法、库挂了）时页面照样显示"已保存"，农户以为存上了。
        """
        if isinstance(sow_date, str):
            sow_date = sow_date.strip()
        ph_val, _ = parse_ph(ph)
        r = self._run("""
            INSERT INTO user_plot (username, plot_name, crop, sow_date, area_mu, region, soil, ph, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              crop = VALUES(crop), sow_date = VALUES(sow_date), area_mu = VALUES(area_mu),
              region = VALUES(region), soil = VALUES(soil), ph = VALUES(ph), note = VALUES(note)
        """, (username, plot_name, crop, sow_date, area_mu, region, soil, ph_val, note), fetch=False)
        return r is not None

    def delete_plot(self, username, plot_id):
        """
        删除地块。同样要返回真实结果（删 0 行也算失败，别谎报成功）。

        删成功后**级联清掉这块地的勾选记录**（plot_task_log），否则会留下孤儿：
        地块没了，今日待办里那条完成记录还在库里占着（按天复位也治不了，因为压根没有地块可对上）。
        清理条数记在 self.last_task_cleaned，供接口回显；清理失败不影响"地块已删除"这个事实。
        """
        self.last_task_cleaned = 0
        deleted = self._run("DELETE FROM user_plot WHERE username = %s AND id = %s",
                            (username, plot_id), fetch=False, rowcount=True)
        if not isinstance(deleted, int):
            return False                       # 数据库不可用 / SQL 报错
        if deleted == 0:
            # MySQL 删 0 行不报错，只能靠行数判断：这块地不存在（或不是他的）
            self.error = "地块不存在或已被删除"
            return False
        try:
            # 延迟导入：plot_store 与 task_store 互不依赖，避免启动时互相拉起
            from core.task_store import get_task_store
            n = get_task_store().clear_plot(username, plot_id)
            self.last_task_cleaned = max(n, 0)
            if n < 0:
                print("WARN: 地块已删除，但勾选记录清理失败（留下孤儿）：%s/%s"
                      % (username, plot_id))
        except Exception as e:
            # 级联失败绝不能把"删除成功"改成失败 —— 地块已经删掉了，回滚不了
            print("WARN: 级联清理勾选记录异常（地块仍已删除）：%s" % e)
        try:
            # 经济效益测算：**只解绑不删记录**（测算账是农户自己填的经营数据，不该跟着地块没）。
            # 解绑后 plot_id 置空、plot_name 快照留着，历史测算仍看得出是哪块地算的。
            from core.econ_store import get_econ_store
            n = get_econ_store().unlink_plot(username, plot_id)
            self.last_econ_unlinked = max(n, 0)
            if n < 0:
                print("WARN: 地块已删除，但测算记录解绑失败（仍指向不存在的地块）：%s/%s"
                      % (username, plot_id))
        except Exception as e:
            print("WARN: 级联解绑测算记录异常（地块仍已删除）：%s" % e)
        return True

    def count(self, username):
        rows = self._run("SELECT COUNT(*) FROM user_plot WHERE username = %s", (username,))
        return rows[0][0] if rows else 0
