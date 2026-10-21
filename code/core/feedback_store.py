#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
意见反馈（FeedbackStore）
=========================
农户在页面上提意见或报问题，管理端逐条回复 —— 这是从配套版本移植过来的功能，
本项目其它模块（历史记录 / 经济效益 / 待办）都没有这个口子，属于**纯新增**。

    feedback (id, username, content, reply, created_at, replied_at)
      · 农户端只能看到**自己的**反馈与回复；
      · 管理端能看全部、逐条回复（同一条可以反复修改回复，改的是同一行）。

设计与 plot_store / task_store / history_store 完全一致：

* 表在**首次使用时自动创建**（CREATE TABLE IF NOT EXISTS），不依赖手工执行 SQL；
* MySQL 不可用时**整体静默降级**（读返回空、写返回 False），绝不抛异常，
  更不允许影响页面打开 —— 页面上会说明原因；
* 写操作**必须用 `is not None` 判断成功**（照抄 history_store 的规矩，
  0 行也是 falsy 之外的值，别拿 `if r:` 判）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402
from core.db_util import error_text               # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS `feedback` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '提交人',
  `content` text NOT NULL COMMENT '反馈内容',
  `reply` text COMMENT '管理端回复',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '提交时间',
  `replied_at` datetime DEFAULT NULL COMMENT '回复时间',
  PRIMARY KEY (`id`),
  KEY `idx_user_time` (`username`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='意见反馈表'
"""

MAX_CONTENT = 2000
MAX_REPLY = 2000


class FeedbackStore:
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
        """首次使用时自动建表；失败则标记不可用（不抛异常，保证页面能打开）"""
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
            self.error = error_text(self.cfg, e, "意见反馈")
            print("WARN: 意见反馈表不可用（MySQL 未连接或权限不足）：%s" % e)
        return self.available

    def _run(self, sql, params=None, fetch=True):
        """
        统一出口。返回值语义：
            查询失败 -> []      写失败 -> None
        """
        if not self.ensure_table():
            return [] if fetch else None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                if fetch:
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
                conn.commit()
                return True
        except Exception as e:
            print("WARN: 意见反馈操作失败：%s" % e)
            return [] if fetch else None
        finally:
            conn.close()

    # ------------------------------------------------------------ 写
    def add(self, username, content):
        """提交一条反馈。成功 True，数据库不可用/内容为空 False。"""
        txt = (content or "").strip()
        if not txt:
            return False
        r = self._run("INSERT INTO feedback (username, content) VALUES (%s, %s)",
                      ((username or "").strip(), txt[:MAX_CONTENT]), fetch=False)
        return r is not None

    def reply(self, fid, text):
        """管理端回复（同一条可反复修改）。成功 True。"""
        txt = (text or "").strip()
        if not txt:
            return False
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            return False
        r = self._run("UPDATE feedback SET reply = %s, replied_at = NOW() WHERE id = %s",
                      (txt[:MAX_REPLY], fid), fetch=False)
        if r is None:
            return False
        # 更新 0 行（id 不存在）也算失败 —— 否则页面会报"已提交"但什么都没发生
        rows = self._run("SELECT COUNT(*) AS n FROM feedback WHERE id = %s AND reply IS NOT NULL",
                         (fid,))
        return bool(rows and rows[0]["n"])

    # ------------------------------------------------------------ 读
    def mine(self, username, limit=50):
        """某个用户的全部反馈（新的在前）"""
        return self._run("""
            SELECT id, username, content, reply, created_at, replied_at
            FROM feedback WHERE username = %s ORDER BY id DESC LIMIT %s
        """, ((username or "").strip(), int(limit)))

    def all(self, limit=200, status="all"):
        """
        管理端反馈列表（新的在前）

        status：all 全部 / pending 待回复 / replied 已回复

        ⚠ 判定口径必须和 `stats()` 一模一样 —— **reply 是空字符串也算没回复**，
          两处写岔了会出现「列表里 3 条、上面却写着 2 条待回复」这种对不上的情况。
        """
        cond = ""
        if status == "pending":
            cond = " WHERE reply IS NULL OR reply = ''"
        elif status == "replied":
            cond = " WHERE reply IS NOT NULL AND reply <> ''"
        # ⚠ SQL 里真正的占位符要写成 %%s：上面的 `% cond` 会把它还原成 %s
        return self._run(
            "SELECT id, username, content, reply, created_at, replied_at "
            "FROM feedback%s ORDER BY id DESC LIMIT %%s" % cond,
            (int(limit),))

    def stats(self):
        """管理端概览：总数 / 待回复 / 已回复"""
        out = {"total": 0, "pending": 0, "replied": 0}
        rows = self._run("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN reply IS NULL OR reply = '' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN reply IS NULL OR reply = '' THEN 0 ELSE 1 END) AS replied
            FROM feedback
        """)
        if rows:
            for k in out:
                out[k] = int(rows[0].get(k) or 0)
        return out


# 与其它 store 同款：模块级单例，避免每次请求都新建连接配置
_STORE = None


def get_feedback_store():
    global _STORE
    if _STORE is None:
        _STORE = FeedbackStore()
    return _STORE
