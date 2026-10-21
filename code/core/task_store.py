#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
地块待办完成记录（TaskStore）
=============================
回答农户每天的一句话："今天这块地的事，干了没有？"

    plot_task_log  (username, plot_id, task_date, task_name) 唯一
      · task_name = `__plot__` → "整块地今天的待办已完成"（今日待办页上的那个勾）
      · 其它值 = 具体农事条目名（阶段农事表逐条勾选用，已预留）

设计与 plot_store / history_store 保持一致：

* 表在**首次使用时自动创建**（CREATE TABLE IF NOT EXISTS），不依赖手工执行 SQL；
* MySQL 不可用时**整体静默降级**（读返回空、写返回 False），绝不抛异常、
  更不允许影响「今日待办」页的正常浏览；
* 勾选**按天记**（task_date）：昨天勾过不会让今天显示"已完成"，
  历史记录天然留下来，后面想做"本周完成率"直接按日期查即可。
"""
import datetime
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402
from core.db_util import error_text               # noqa: E402

# 列表页那个"整块地今天完成"的勾用这个保留名，避免与真实农事名撞车
PLOT_TASK = "__plot__"

DDL = """
CREATE TABLE IF NOT EXISTS `plot_task_log` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `plot_id` int NOT NULL COMMENT '地块ID（user_plot.id）',
  `task_date` date NOT NULL COMMENT '归属日期',
  `task_name` varchar(128) NOT NULL COMMENT '农事条目名；__plot__ = 整块地当天完成',
  `done` tinyint(1) NOT NULL DEFAULT 1 COMMENT '1 已完成 / 0 待办',
  `note` varchar(255) DEFAULT '' COMMENT '备注（选填）',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task` (`username`, `plot_id`, `task_date`, `task_name`),
  KEY `idx_user_date` (`username`, `task_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='地块待办完成记录表'
"""

MAX_NAME = 128
MAX_NOTE = 255


def today_str():
    """服务器本地今天（与物候引擎的"今天"保持同一个口径）"""
    return datetime.date.today().isoformat()


def valid_date(s):
    """YYYY-MM-DD 合法性校验 —— 写库前先挡一道，避免脏日期进去查不出来"""
    try:
        datetime.date.fromisoformat(str(s or ""))
        return True
    except ValueError:
        return False


class TaskStore:
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
                conn.commit()
                self._ready = True
            finally:
                conn.close()
        except Exception as e:
            self.available = False
            self.error = error_text(self.cfg, e, "待办完成记录")
            print("WARN: 待办完成记录不可用（MySQL 未连接或权限不足）：%s" % e)
        return self.available

    def _run(self, sql, params=None, fetch=True, rowcount=False):
        """
        统一出口。返回值语义：
            查询失败 -> []        写失败 -> None
        写操作**必须用 `is not None` 判断成功**（照抄 history_store 的规矩）

        rowcount=True 时写操作返回「受影响行数」（int）而不是 True —— 删除类操作要报条数用。
        ⚠ 行数必须在 commit / 关游标**之前**读，否则 cur 已关闭拿不到。
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
            print("WARN: 待办记录操作失败：%s" % e)
            return [] if fetch else None
        finally:
            conn.close()

    # ------------------------------------------------------------ 写
    def set_task(self, username, plot_id, task_name=PLOT_TASK, done=True,
                 date=None, note=""):
        """
        勾选 / 取消勾选一条（同一格 upsert）。成功 True，数据库不可用 False。
        """
        d = (date or today_str())
        name = (str(task_name or PLOT_TASK).strip() or PLOT_TASK)[:MAX_NAME]
        try:
            pid = int(plot_id)
        except (TypeError, ValueError):
            return False
        r = self._run("""
            INSERT INTO plot_task_log (username, plot_id, task_date, task_name, done, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE done = VALUES(done), note = VALUES(note)
        """, (username, pid, d, name, 1 if done else 0,
              (note or "")[:MAX_NOTE]), fetch=False)
        return r is not None

    # ------------------------------------------------------------ 清理
    def clear_plot(self, username, plot_id):
        """
        清掉某块地的全部勾选记录（地块被删除时级联调用）。

        ⚠ 键是 **plot_id 不是 plot_name**：表的唯一键就是 (username, plot_id, task_date, task_name)，
          按 id 删既能命中全部历史日期，又不会因为两块地同名而误删别人的记录。

        返回删除条数；数据库不可用 / 执行失败返回 -1（调用方据此决定是否告警）。
        """
        try:
            pid = int(plot_id)
        except (TypeError, ValueError):
            return -1
        n = self._run("DELETE FROM plot_task_log WHERE username = %s AND plot_id = %s",
                      (username, pid), fetch=False, rowcount=True)
        return n if isinstance(n, int) else -1

    def purge_orphans(self, username=None):
        """
        清理历史遗留的孤儿记录：地块已经不在 user_plot 里，勾选记录还留着。
        （本次级联是"以后不再产生"，这条 SQL 用来治"以前已经产生的"。）

        username 为空则全库清理。返回删除条数，失败返回 -1。
        """
        sql = """
            DELETE t FROM plot_task_log t
            LEFT JOIN user_plot p ON p.id = t.plot_id AND p.username = t.username
            WHERE p.id IS NULL
        """
        args = ()
        if username:
            sql += " AND t.username = %s"
            args = (username,)
        n = self._run(sql, args, fetch=False, rowcount=True)
        return n if isinstance(n, int) else -1

    # ------------------------------------------------------------ 读
    def tasks_of_date(self, username, date=None):
        """某天的全部记录 -> [{plot_id, task_name, done, note, time}]"""
        d = (date or today_str())
        rows = self._run("""
            SELECT plot_id, task_name, done, note, update_time
            FROM plot_task_log
            WHERE username = %s AND task_date = %s
            ORDER BY plot_id, id
        """, (username, d))
        out = []
        for r in rows:
            out.append({
                "plot_id": r[0],
                "task_name": r[1] or "",
                "done": bool(r[2]),
                "note": r[3] or "",
                "time": r[4].strftime("%H:%M") if r[4] else "",
            })
        return out

    def today_map(self, username, date=None):
        """
        给列表页用：{plot_id: {"done": bool, "time": "09:12", "done_count": n}}
        · done       —— 有没有勾"整块地今天完成"
        · done_count —— 今天这块地记了几条（含条目级，供以后做进度条）
        """
        out = {}
        for t in self.tasks_of_date(username, date):
            it = out.setdefault(t["plot_id"], {"done": False, "time": "", "done_count": 0})
            if t["done"]:
                it["done_count"] += 1
                it["time"] = t["time"] or it["time"]
                if t["task_name"] == PLOT_TASK:
                    it["done"] = True
        return out


# 与 plot_store 同款：模块级单例，避免每次请求都新建连接配置
_STORE = None


def get_task_store():
    global _STORE
    if _STORE is None:
        _STORE = TaskStore()
    return _STORE
