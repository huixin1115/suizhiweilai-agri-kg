#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史记录与用户反馈存取（HistoryStore）
=====================================
三张表各自负责一件事：

    qa_history     每次提问/生成处方都留一条（问题 + 图谱答案 + 大模型答案）
    qa_feedback    对某条记录的评价（有帮助 / 没帮助 + 纠错说明）
    user_behavior  行为流水（谁在什么时候问了什么、命中了哪个实体）
    hot_content    热门内容计数（作物 / 病虫害被问了多少次）

设计原则与 `plot_store.py` 一致：

* 表在**首次使用时自动创建**（CREATE TABLE IF NOT EXISTS），不依赖手工执行 SQL；
* `qa_history` / `user_behavior` / `hot_content` 三张表**沿用原项目
  `database/hot_content.sql` 里的字段设计**，只给 qa_history 追加 3 个非破坏性字段
  （plan_type / plan_name / sources），并用容错迁移兼容"先导入了老 SQL"的情况；
* **MySQL 不可用时整体静默降级**：返回空列表 / 0 / None，绝不抛异常，
  更不允许影响"出答案"这条主链路（调用处也用 try 包住）。
"""
import datetime
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402

# ---------------------------------------------------------------- 建表语句
# 前 5 个字段与 database/hot_content.sql 里的原设计完全一致；
# 后 4 个是本项目为「历史记录」页追加的（类型 / 中文名 / 知识溯源 / 缩略图）
DDL_QA_HISTORY = """
CREATE TABLE IF NOT EXISTS `qa_history` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `question` text NOT NULL COMMENT '用户问题',
  `kg_answer` text COMMENT '知识图谱答案',
  `llm_answer` text COMMENT 'LLM润色后的答案',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `plan_type` varchar(32) DEFAULT 'general' COMMENT '类型：pest/fertilizer/farm/planning/general/basic/diagnose',
  `plan_name` varchar(64) DEFAULT '' COMMENT '类型中文名',
  `sources` text COMMENT '知识溯源（每行一条）',
  `image` varchar(255) DEFAULT '' COMMENT '拍照识病的缩略图 URL（选填）',
  PRIMARY KEY (`id`),
  KEY `idx_user_time` (`username`, `create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='问答与处方历史记录表'
"""

DDL_QA_FEEDBACK = """
CREATE TABLE IF NOT EXISTS `qa_feedback` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `history_id` int NOT NULL COMMENT '对应 qa_history.id',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `rating` varchar(16) NOT NULL COMMENT 'helpful / unhelpful',
  `comment` varchar(1000) DEFAULT '' COMMENT '纠错或补充说明',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_history_user` (`history_id`, `username`),
  KEY `idx_user` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回答反馈表'
"""

DDL_USER_BEHAVIOR = """
CREATE TABLE IF NOT EXISTS `user_behavior` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(64) NOT NULL COMMENT '用户名',
  `behavior_type` varchar(32) NOT NULL COMMENT '行为类型：query/click/feedback等',
  `content` text NOT NULL COMMENT '用户查询的内容',
  `entity_info` varchar(128) DEFAULT '' COMMENT '提取的核心实体（作物/病害名）',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '行为时间',
  PRIMARY KEY (`id`),
  KEY `idx_username` (`username`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户行为记录表'
"""

DDL_HOT_CONTENT = """
CREATE TABLE IF NOT EXISTS `hot_content` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `content_type` varchar(32) NOT NULL COMMENT '内容类型：crop/disease/region',
  `content_name` varchar(128) NOT NULL COMMENT '内容名称（作物/病害名）',
  `search_count` int NOT NULL DEFAULT 0 COMMENT '搜索次数',
  `update_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_type_name` (`content_type`, `content_name`),
  KEY `idx_search_count` (`search_count`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='热门内容统计表'
"""

# 给"已经导入过老 SQL"的库补列（MySQL 8 不支持 ADD COLUMN IF NOT EXISTS，
# 所以只能靠错误码 1060 duplicate column 判定"已经有了"，直接跳过）
MIGRATE_QA_HISTORY = [
    "ALTER TABLE `qa_history` ADD COLUMN `plan_type` varchar(32) DEFAULT 'general'",
    "ALTER TABLE `qa_history` ADD COLUMN `plan_name` varchar(64) DEFAULT ''",
    "ALTER TABLE `qa_history` ADD COLUMN `sources` text",
    "ALTER TABLE `qa_history` ADD COLUMN `image` varchar(255) DEFAULT ''",
]

# 几个字段的写入长度上限，防止超长把行撑爆（TEXT 有 64KB 限制）
MAX_QUESTION = 2000
MAX_SOURCES = 20000
MAX_COMMENT = 1000


def valid_day(s):
    """YYYY-MM-DD 合法吗？不合法就当没传（不报错，按"不筛选"处理）"""
    try:
        datetime.date.fromisoformat(str(s or ""))
        return True
    except (ValueError, TypeError):
        return False


def day_clause(date_from="", date_to=""):
    """
    日期区间条件（按 create_time 比较）。返回 (SQL 片段, 参数列表)。

    ⚠ 两个刻意的设计：
      1. date_to 是**含当天**的，这里转成"次日 00:00"做开区间比较 ——
         既能用上 create_time 的索引，又不像 DATE(create_time) 那样包一层把索引弄失效，
         也避开了 `<= 'xx 23:59:59'` 在秒后带小数时漏掉最后一秒的行。
      2. 日期不合法（空串、半个日期、手改 URL）就**整个忽略**：宁可不筛，也不要报错。
    """
    sql, params = "", []
    if valid_day(date_from):
        sql += " AND create_time >= %s"
        params.append(str(date_from) + " 00:00:00")
    if valid_day(date_to):
        nxt = datetime.date.fromisoformat(str(date_to)) + datetime.timedelta(days=1)
        sql += " AND create_time < %s"
        params.append(nxt.isoformat() + " 00:00:00")
    return sql, params


def _norm_q(q):
    """
    问题归一化 —— 差评聚类用。

    「小麦条锈病怎么防治？」「小麦条锈病 怎么防治」「小麦条锈病怎么防治！！」
    在农户嘴里是同一个问题，但字符串并不相等，直接 GROUP BY 会散成三条、
    永远看不出"哪条答案最该改"。这里把**空白和所有标点去掉**再比。

    ⚠ `[^\\w]` 里的 `\\w` 在 Unicode 模式下**含中文**，所以汉字会被保留、
      只有标点与空格被剔除；`_` 属于 `\\w` 要单独去掉（否则下划线也能区分两条）。
    ⚠ 归一化后可能是空串（问题全是标点），调用方要兜底。
    """
    return re.sub(r"[^\w]+", "", (q or "").lower()).replace("_", "")


def _now_text():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


class HistoryStore:
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

    def _error_text(self, e):
        """
        把 pymysql 的原始异常压成一句人话。

        页面上直接铺 `(2003, "Can't connect to MySQL server on 'localhost'
        ([WinError 10061] 由于目标计算机积极拒绝，无法连接。)")` 又长又吓人，
        农户不关心 WinError 编号；**原始异常仍原样打到控制台日志**，排障不受影响。
        """
        txt = str(e)
        code = getattr(e, "args", [None])[0]
        low = txt.lower()
        if code == 2003 or "can't connect" in low or "10061" in txt or "refused" in low:
            return "连不上 MySQL（多半是服务没启动，或地址 / 端口不对）"
        if code == 1045 or "access denied" in low:
            return "MySQL 账号或密码不对"
        if code == 1049 or "unknown database" in low:
            return "MySQL 里没有 `%s` 这个库" % self.cfg.get("database", "")
        if code == 1044:
            return "当前 MySQL 账号没有权限访问 `%s`" % self.cfg.get("database", "")
        return "数据库不可用：%s" % txt[:120]

    def ensure_tables(self):
        """首次使用时自动建表；失败则标记不可用（不抛异常，保证页面能用）"""
        if self._ready or not self.available:
            return self.available
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    for ddl in (DDL_QA_HISTORY, DDL_QA_FEEDBACK,
                                DDL_USER_BEHAVIOR, DDL_HOT_CONTENT):
                        cur.execute(ddl)
                conn.commit()
                self._migrate(conn)
                self._ready = True
            finally:
                conn.close()
        except Exception as e:
            self.available = False
            self.error = self._error_text(e)
            print("WARN: 历史记录不可用（MySQL 未连接或权限不足）：%s" % e)
        return self.available

    @staticmethod
    def _migrate(conn):
        """老库补齐新列：已有该列时忽略 1060，其它错误也不阻断使用"""
        with conn.cursor() as cur:
            for sql in MIGRATE_QA_HISTORY:
                try:
                    cur.execute(sql)
                except Exception as e:
                    if getattr(e, "args", [None])[0] != 1060:
                        print("WARN: 历史表补列失败（可忽略）：%s" % e)
            conn.commit()

    def _run(self, sql, params=None, fetch=True):
        """
        统一出口。返回值语义：
            查询失败 -> []        写失败 -> None
        写操作**必须用 `is not None` 判断成功**：这里最初写的是 `else 0`，
        结果 `if ok is not None` 把"写失败"当成了成功（试出来的坑）。
        """
        if not self.ensure_tables():
            return [] if fetch else None
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall() if fetch else None
                last = cur.lastrowid
                if not fetch:
                    conn.commit()
                return rows if fetch else last
        except Exception as e:
            print("WARN: 历史记录操作失败：%s" % e)
            return [] if fetch else None
        finally:
            conn.close()

    # ------------------------------------------------------------ 写
    def record(self, username, question, kg_answer="", llm_answer="",
               plan_type="general", plan_name="", sources=None, entities=None, image=""):
        """
        记一条历史，返回新记录 id（失败返回 None —— 调用方不要依赖它）
        image：拍照识病的缩略图 URL（选填，只存路径不存二进制，避免把库撑大）
        """
        src_txt = ""
        if sources:
            src_txt = "\n".join(str(s) for s in list(sources)[:80])[:MAX_SOURCES]
        hid = self._run("""
            INSERT INTO qa_history
              (username, question, kg_answer, llm_answer, plan_type, plan_name, sources, image)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (username, (question or "")[:MAX_QUESTION], kg_answer or "", llm_answer or "",
              plan_type or "general", plan_name or "", src_txt, (image or "")[:255]), fetch=False)
        if hid:
            self.log_behavior(username, question, entities, behavior_type="query")
        return hid

    def log_behavior(self, username, content, entities=None, behavior_type="query"):
        """写一条行为流水；entity_info 取识别到的作物/病虫害，供后续统计"""
        info = ""
        if entities:
            for k in ("crop", "pest", "weed", "fertilizer", "region"):
                v = (entities or {}).get(k)
                if v:
                    info = str(v)[:120]
                    break
        ok = self._run("""
            INSERT INTO user_behavior (username, behavior_type, content, entity_info)
            VALUES (%s, %s, %s, %s)
        """, (username, behavior_type, (content or "")[:MAX_QUESTION], info), fetch=False)
        return ok is not None

    def bump_hot(self, entities):
        """
        把这次识别到的作物/病虫害计入热门内容表（有则 +1，无则新建）。
        这是原来那张 hot_content 表的第一个真实用途。
        """
        if not entities:
            return
        pairs = []
        if entities.get("crop"):
            pairs.append(("crop", str(entities["crop"])[:128]))
        for k in ("pest", "weed"):
            if entities.get(k):
                pairs.append(("disease", str(entities[k])[:128]))
        for ctype, cname in pairs:
            self._run("""
                INSERT INTO hot_content (content_type, content_name, search_count)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE search_count = search_count + 1
            """, (ctype, cname), fetch=False)

    def save_feedback(self, username, history_id, rating, comment=""):
        """同一条记录重复提交就覆盖（唯一键 history_id + username）"""
        if rating not in ("helpful", "unhelpful"):
            return False
        ok = self._run("""
            INSERT INTO qa_feedback (history_id, username, rating, comment)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE rating = VALUES(rating), comment = VALUES(comment)
        """, (int(history_id), username, rating, (comment or "")[:MAX_COMMENT]), fetch=False)
        if ok is not None:
            # 反馈也是一条行为，顺手留痕（便于统计"多少条被追评过"）
            self.log_behavior(username, "反馈：%s" % rating, None, behavior_type="feedback")
            return True
        return False

    def delete(self, username, history_id):
        a = self._run("DELETE FROM qa_history WHERE username = %s AND id = %s",
                      (username, history_id), fetch=False)
        b = self._run("DELETE FROM qa_feedback WHERE username = %s AND history_id = %s",
                      (username, history_id), fetch=False)
        return a is not None and b is not None

    def clear(self, username):
        a = self._run("DELETE FROM qa_history WHERE username = %s", (username,), fetch=False)
        b = self._run("DELETE FROM qa_feedback WHERE username = %s", (username,), fetch=False)
        return a is not None and b is not None

    def images(self, username):
        """
        该账号所有记录里挂着的缩略图 URL（只返回非空的）。
        清空历史前先取一份，清完记着把文件也删掉，不然 static/uploads 里会攒一堆孤儿图。
        """
        rows = self._run("SELECT image FROM qa_history WHERE username = %s AND image <> ''",
                         (username,))
        return [r[0] for r in rows if r[0]]

    # ------------------------------------------------------------ 读
    def _filter_clause(self, plan_type="", keyword="", date_from="", date_to="", tbl=""):
        """
        类型 + 关键词 + 日期区间这三样的公共条件，返回 (SQL 片段, 参数列表)。
        从 " AND ..." 开始，直接拼在 `WHERE username = %s` / `WHERE username IN (...)` 后面。

        tbl 是表别名前缀（JOIN 查询里要写成 `h.plan_type`）：
        qa_feedback 与 qa_history **都有 create_time**，不加前缀会报 Column 'create_time' is ambiguous。
        """
        t = tbl or ""
        sql, params = "", []
        if plan_type and plan_type != "all":
            sql += " AND " + t + "plan_type = %s"
            params.append(plan_type)
        if keyword:
            sql += " AND (" + t + "question LIKE %s OR " + t + "kg_answer LIKE %s)"
            like = "%%%s%%" % keyword
            params += [like, like]
        dsql, dparams = day_clause(date_from, date_to)
        # day_clause 生成的是裸列名，这里补上别名前缀
        dsql = dsql.replace("create_time", t + "create_time") if t else dsql
        return sql + dsql, params + dparams

    def list(self, username, limit=50, offset=0, plan_type="", keyword="",
             date_from="", date_to=""):
        sql = ("SELECT id, question, kg_answer, llm_answer, plan_type, plan_name, "
               "sources, create_time, image FROM qa_history WHERE username = %s")
        params = [username]
        if plan_type and plan_type != "all":
            sql += " AND plan_type = %s"
            params.append(plan_type)
        if keyword:
            sql += " AND (question LIKE %s OR kg_answer LIKE %s)"
            like = "%%%s%%" % keyword
            params += [like, like]
        dsql, dparams = day_clause(date_from, date_to)
        sql += dsql
        params += dparams
        sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params += [int(limit), int(offset)]

        rows = self._run(sql, tuple(params))
        items = [self._row(r) for r in rows]
        fb = self.feedback_map(username, [it["id"] for it in items])
        for it in items:
            it["feedback"] = fb.get(it["id"])
        return items

    # ------------------------------------------------ 管理端：跨用户视角
    # 「管理端能看到名下用户端的历史记录」是这里唯一的用途：
    # 读的时候按**一批用户名**取，并且每条记录带上 username；
    # 写（record / save_feedback / delete / clear）仍是单人语义，一个字段都没改。
    @staticmethod
    def _in_list(values):
        """把用户名列表拼成 %s,%s,...（值依然全部走参数化，不拼进 SQL 文本）"""
        return ",".join(["%s"] * len(values))

    def list_multi(self, usernames, limit=50, offset=0, plan_type="", keyword="",
                   date_from="", date_to=""):
        """
        管理端视角的列表：一次取多个用户的记录，**每条带 username**。
        usernames 为空直接返回 []（不查库，避免拼出 `IN ()` 这种非法 SQL）。

        ⚠ 占位符用 `{IN}` 而不是 `%s`：`IN` 列表是**先拼进 SQL 文本**的，
          若用 % 格式化，后面 day_clause 拼进来的 `%s` 会被一起吃掉报 "not enough arguments"。
        """
        users = [u for u in (usernames or []) if u]
        if not users:
            return []
        sql = ("SELECT id, username, question, kg_answer, llm_answer, plan_type, plan_name, "
               "sources, create_time, image FROM qa_history WHERE username IN ({IN})"
               .replace("{IN}", self._in_list(users)))
        params = list(users)
        if plan_type and plan_type != "all":
            sql += " AND plan_type = %s"
            params.append(plan_type)
        if keyword:
            sql += " AND (question LIKE %s OR kg_answer LIKE %s)"
            like = "%%%s%%" % keyword
            params += [like, like]
        dsql, dparams = day_clause(date_from, date_to)
        sql += dsql
        params += dparams
        sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params += [int(limit), int(offset)]

        rows = self._run(sql, tuple(params))
        items = [self._row_multi(r) for r in rows]
        fb = self.feedback_map_any([it["id"] for it in items])
        for it in items:
            it["feedback"] = fb.get(it["id"])
        return items

    @staticmethod
    def _row_multi(r):
        """跨用户查询的行映射：比 _row 多一列 username（放在第 2 列）"""
        it = HistoryStore._row([r[0]] + list(r[2:]))
        it["username"] = r[1] or ""
        return it

    def feedback_map_any(self, ids):
        """
        按 history_id 直接取反馈（不按 username 过滤）。
        一条历史记录只属于一个用户，所以这里不会串号；
        之所以要另开一个方法：原来的 feedback_map 是「我的反馈」语义。
        """
        if not ids:
            return {}
        rows = self._run("""
            SELECT history_id, rating, comment, create_time
            FROM qa_feedback WHERE history_id IN (%s)
        """ % self._in_list(ids), tuple(ids))
        out = {}
        for r in rows:
            out[r[0]] = {"rating": r[1] or "", "comment": r[2] or "",
                         "time": r[3].strftime("%Y-%m-%d %H:%M") if r[3] else ""}
        return out

    def stats_multi(self, usernames, plan_type="", keyword="", date_from="", date_to=""):
        """
        管理端视角的汇总：总数 / 分类计数 / 反馈计数，
        另外多给一个 by_user ——「每个用户端各留下多少条」，前端用它渲染用户下拉。
        **同样按筛选条件统计**（与 stats 保持一致）。
        """
        out = {"total": 0, "by_type": {}, "helpful": 0, "unhelpful": 0, "by_user": {}}
        users = [u for u in (usernames or []) if u]
        if not users:
            return out
        ph = self._in_list(users)
        fc, fp = self._filter_clause(plan_type, keyword, date_from, date_to)
        rows = self._run("SELECT username, plan_type, COUNT(*) FROM qa_history "
                         "WHERE username IN ({IN})".replace("{IN}", ph)
                         + fc + " GROUP BY username, plan_type",
                         tuple(users) + tuple(fp))
        for u, t, n in rows:
            out["by_user"][u] = out["by_user"].get(u, 0) + n
            out["by_type"][t or "general"] = out["by_type"].get(t or "general", 0) + n
            out["total"] += n
        # 同上：反馈 JOIN 回历史记录，才能跟着类型 / 关键词 / 日期一起筛选
        fc2, fp2 = self._filter_clause(plan_type, keyword, date_from, date_to, tbl="h.")
        rows = self._run("SELECT f.rating, COUNT(*) FROM qa_feedback f "
                         "JOIN qa_history h ON h.id = f.history_id "
                         "WHERE f.username IN ({IN})".replace("{IN}", ph)
                         + fc2 + " GROUP BY f.rating",
                         tuple(users) + tuple(fp2))
        for rating, n in rows:
            if rating == "helpful":
                out["helpful"] = n
            elif rating == "unhelpful":
                out["unhelpful"] = n
        return out

    # ------------------------------------------------ 差评汇总（2026-09-28）
    def feedback_review(self, usernames, limit=100, plan_type="", keyword="",
                        date_from="", date_to=""):
        """
        **「哪条答案被标没帮助最多」** —— 评价 → 改进这条闭环的落点。

        ⚠ 一个必须先说清的口径问题：`qa_feedback` 的唯一键是 `(history_id, username)`，
          **一条历史记录最多只有一条评价**，所以按 history_id 分组 count 永远是 1，
          排不出"最多"。真正该聚的是**"同一个问题"**：同一个问法被反复问、
          反复被标没帮助，那才是答案确实有问题、最该先改的地方。

        于是这里一次给出三个口径（前端分三块显示）：

          summary      总评数 / 差评数 / 差评率 / 其中写了纠错说明的条数
          by_question  **按归一化问题聚类**（剔除标点与空格后比对）→ 差评次数倒序
                       count >= 2 的就是"同一个问题被反复差评"，最该改
          by_type      按方案类型：**差评率 = 该类型差评数 / 该类型被评价数**
                       ⚠ 只看差评绝对数会被"问得多的类型"带偏，所以必须给分母
          items        差评明细（新的在前）：问题 / 类型 / 谁问的 / 时间 / 纠错说明

        返回结构：{"summary": {...}, "by_question": [...], "by_type": [...], "items": [...]}
        """
        out = {"summary": {"rated": 0, "helpful": 0, "unhelpful": 0,
                           "rate": 0.0, "with_comment": 0},
               "by_question": [], "by_type": [], "items": []}
        users = [u for u in (usernames or []) if u]
        if not users:
            return out

        ph = self._in_list(users)
        fc, fp = self._filter_clause(plan_type, keyword, date_from, date_to, tbl="h.")
        rows = self._run(
            "SELECT f.history_id, f.username, f.rating, f.comment, f.create_time, "
            "h.question, h.plan_type, h.plan_name "
            "FROM qa_feedback f JOIN qa_history h ON h.id = f.history_id "
            "WHERE f.username IN ({IN})".replace("{IN}", ph) + fc +
            " ORDER BY f.id DESC", tuple(users) + tuple(fp))

        rated = len(rows)
        bad_rows = [r for r in rows if (r[2] or "") == "unhelpful"]
        out["summary"]["rated"] = rated
        out["summary"]["unhelpful"] = len(bad_rows)
        out["summary"]["helpful"] = rated - len(bad_rows)
        out["summary"]["rate"] = (round(len(bad_rows) * 100.0 / rated, 1)
                                  if rated else 0.0)
        out["summary"]["with_comment"] = len([r for r in bad_rows if (r[3] or "").strip()])

        # ---- 按类型：差评率要有分母，否则"病虫害"永远排第一只是因为问得多 ----
        type_rated, type_bad, type_name = {}, {}, {}
        for r in rows:
            t = r[6] or "general"
            type_rated[t] = type_rated.get(t, 0) + 1
            if r[7]:
                type_name[t] = r[7]
        for r in bad_rows:
            t = r[6] or "general"
            type_bad[t] = type_bad.get(t, 0) + 1
        for t, n in sorted(type_bad.items(), key=lambda kv: (-kv[1], kv[0])):
            out["by_type"].append({
                "plan_type": t,
                "plan_name": type_name.get(t) or t,
                "bad": n,
                "rated": type_rated.get(t, 0),
                "rate": round(n * 100.0 / type_rated[t], 1) if type_rated.get(t) else 0.0,
            })

        # ---- 按问题聚类：同一个问法被差评几次 ----
        groups, order = {}, []
        for r in bad_rows:
            hid, who, _rating, comment, ftime, question, ptype, pname = r
            key = _norm_q(question) or (question or "")[:30]
            g = groups.get(key)
            if g is None:
                g = {"key": key, "question": question or "", "count": 0,
                     "ids": [], "users": [], "comments": [], "last": "",
                     "plan_type": ptype or "general", "plan_name": pname or ""}
                groups[key] = g
                order.append(key)
            g["count"] += 1
            if hid not in g["ids"]:
                g["ids"].append(hid)
            if who and who not in g["users"]:
                g["users"].append(who)
            c = (comment or "").strip()
            if c and c not in g["comments"]:
                g["comments"].append(c)
            t = ftime.strftime("%Y-%m-%d %H:%M") if ftime else ""
            if t > g["last"]:
                g["last"] = t
                # 显示用**最近一次**的原文：措辞可能略有出入，取最新的更好认
                g["question"] = question or ""
        # 两次稳定排序：先按最近时间倒序，再按次数倒序（同次数时最近的发生在前）
        by_time = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
        out["by_question"] = sorted(by_time, key=lambda g: g["count"], reverse=True)

        # ---- 差评明细（新的在前）----
        for r in bad_rows[:max(1, int(limit or 100))]:
            out["items"].append({
                "id": r[0],
                "username": r[1] or "",
                "comment": r[3] or "",
                "time": r[4].strftime("%Y-%m-%d %H:%M") if r[4] else "",
                "question": r[5] or "",
                "plan_type": r[6] or "general",
                "plan_name": r[7] or "",
            })
        return out

    def get_in(self, usernames, history_id):
        """
        按 id 取一条记录，**限定在给定用户名范围内**（管理端"看原答案"用）。

        ⚠ 不用 `get()`：那个是按 (username, id) 查单人，管理端要在名下
          一批账号里查，而且**越界的 id 必须查不到**（返回 None），不能只看 id。
        """
        users = [u for u in (usernames or []) if u]
        if not users or not history_id:
            return None
        rows = self._run(
            "SELECT id, username, question, kg_answer, llm_answer, plan_type, "
            "plan_name, sources, create_time, image FROM qa_history "
            "WHERE id = %s AND username IN ({IN})".replace("{IN}", self._in_list(users)),
            tuple([int(history_id)] + users))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "username": r[1] or "", "question": r[2] or "",
                "kg_answer": r[3] or "", "llm_answer": r[4] or "",
                "plan_type": r[5] or "general", "plan_name": r[6] or "",
                "sources": [s for s in (r[7] or "").split("\n") if s.strip()],
                "create_time": r[8].strftime("%Y-%m-%d %H:%M") if r[8] else "",
                "image": r[9] or ""}

    def count_unhelpful(self, usernames):
        """
        侧栏那个橙色数字：**范围内**被标「没帮助」的条数（不做筛选，全时段）。
        ⚠ 单独开一个轻量 COUNT，不要复用 feedback_review —— 后者会把问题、
          纠错说明全拉出来，而侧栏每个管理端页面都要渲染一次。
        """
        users = [u for u in (usernames or []) if u]
        if not users:
            return 0
        rows = self._run(
            "SELECT COUNT(*) FROM qa_feedback f JOIN qa_history h ON h.id = f.history_id "
            "WHERE f.username IN ({IN}) AND f.rating = %s"
            .replace("{IN}", self._in_list(users)),
            tuple(users) + ("unhelpful",))
        return (rows[0][0] if rows else 0) or 0

    # ------------------------------------------------ 图表数据（2026-09-24）
    def daily_counts(self, usernames, days=14, end=None, plan_type="", keyword="",
                     date_from="", date_to=""):
        """
        逐日条数（折线图用）。**缺失的日期会补 0**，前端直接画，不用再处理空洞与补日期。
        返回：[{date: '2026-09-11', label: '09-11', count: 3}, ...]

        默认近 14 天；若传了日期区间，就按区间画（最多 90 天 ——
        防止有人手改 URL 传个 1970 年，一下子返回几万个点把浏览器拖死）。
        """
        users = [u for u in (usernames or []) if u]
        if not users:
            return []
        fc, fp = self._filter_clause(plan_type, keyword, date_from, date_to)
        today = datetime.date.today()
        if valid_day(date_from):
            start = datetime.date.fromisoformat(str(date_from))
            last = datetime.date.fromisoformat(str(date_to)) if valid_day(date_to) else today
            if last < start:
                last = start
            days = max(1, min((last - start).days + 1, 90))
            end = last
        else:
            days = max(1, min(int(days or 14), 90))
            end = end or today
            start = end - datetime.timedelta(days=days - 1)
        rows = self._run("""
            SELECT DATE(create_time) AS d, COUNT(*) FROM qa_history
            WHERE username IN ({IN}) AND create_time >= %s
        """.replace("{IN}", self._in_list(users)) + fc + """
            GROUP BY DATE(create_time)
        """, tuple(users) + (start.isoformat() + " 00:00:00",) + tuple(fp))
        got = {}
        for d, n in rows:
            key = d.isoformat() if hasattr(d, "isoformat") else str(d)
            got[key] = n
        out = []
        for i in range(days):
            day = start + datetime.timedelta(days=i)
            key = day.isoformat()
            out.append({"date": key, "label": day.strftime("%m-%d"), "count": got.get(key, 0)})
        return out

    def questions(self, usernames, limit=800, plan_type="", keyword="",
                  date_from="", date_to=""):
        """
        取问题原文（只取 question 一列，不把答案拉出来）—— 给"问题高频词"词云用。
        按 id 倒序，所以拿到的是最近 limit 条；**同样跟着筛选走**，
        这样选了"拍照识病"之后，词云里出现的就是识病类问题里的词。
        """
        users = [u for u in (usernames or []) if u]
        if not users:
            return []
        fc, fp = self._filter_clause(plan_type, keyword, date_from, date_to)
        rows = self._run("""
            SELECT question FROM qa_history WHERE username IN ({IN})
        """.replace("{IN}", self._in_list(users)) + fc + """
            ORDER BY id DESC LIMIT %s
        """, tuple(users) + tuple(fp) + (int(limit),))
        return [r[0] or "" for r in rows]

    @staticmethod
    def _row(r):
        ct = r[7]
        return {
            "id": r[0],
            "question": r[1] or "",
            "kg_answer": r[2] or "",
            "llm_answer": r[3] or "",
            "plan_type": r[4] or "general",
            "plan_name": r[5] or "",
            "sources": [s for s in (r[6] or "").split("\n") if s.strip()],
            "create_time": ct.strftime("%Y-%m-%d %H:%M") if ct else "",
            # 兼容老查询（列少一个时不给 image，别让整条记录崩掉）
            "image": (r[8] or "") if len(r) > 8 else "",
        }

    def get(self, username, history_id):
        rows = self._run("""
            SELECT id, question, kg_answer, llm_answer, plan_type, plan_name, sources,
                   create_time, image
            FROM qa_history WHERE username = %s AND id = %s
        """, (username, history_id))
        if not rows:
            return None
        item = self._row(rows[0])
        item["feedback"] = self.feedback_map(username, [item["id"]]).get(item["id"])
        return item

    def feedback_map(self, username, ids):
        """批量取反馈状态 -> {history_id: {rating, comment, time}}"""
        if not ids:
            return {}
        marks = ",".join(["%s"] * len(ids))
        rows = self._run("""
            SELECT history_id, rating, comment, create_time
            FROM qa_feedback WHERE username = %%s AND history_id IN (%s)
        """ % marks, tuple([username] + list(ids)))
        out = {}
        for r in rows:
            out[r[0]] = {"rating": r[1] or "", "comment": r[2] or "",
                         "time": r[3].strftime("%Y-%m-%d %H:%M") if r[3] else ""}
        return out

    def count(self, username, plan_type="", keyword="", date_from="", date_to=""):
        """与 list() 同条件的总条数 —— 分页要拿它算总页数（条件必须一模一样，否则翻页会漏）"""
        sql = "SELECT COUNT(*) FROM qa_history WHERE username = %s"
        params = [username]
        if plan_type and plan_type != "all":
            sql += " AND plan_type = %s"
            params.append(plan_type)
        if keyword:
            sql += " AND (question LIKE %s OR kg_answer LIKE %s)"
            like = "%%%s%%" % keyword
            params += [like, like]
        dsql, dparams = day_clause(date_from, date_to)
        sql += dsql
        params += dparams
        rows = self._run(sql, tuple(params))
        return rows[0][0] if rows else 0

    def count_multi(self, usernames, plan_type="", keyword="", date_from="", date_to=""):
        """管理端版本的总条数（与 list_multi 同条件）"""
        users = [u for u in (usernames or []) if u]
        if not users:
            return 0
        sql = ("SELECT COUNT(*) FROM qa_history WHERE username IN ({IN})"
               .replace("{IN}", self._in_list(users)))
        params = list(users)
        if plan_type and plan_type != "all":
            sql += " AND plan_type = %s"
            params.append(plan_type)
        if keyword:
            sql += " AND (question LIKE %s OR kg_answer LIKE %s)"
            like = "%%%s%%" % keyword
            params += [like, like]
        dsql, dparams = day_clause(date_from, date_to)
        sql += dsql
        params += dparams
        rows = self._run(sql, tuple(params))
        return rows[0][0] if rows else 0

    def stats(self, username, plan_type="", keyword="", date_from="", date_to=""):
        """
        历史页顶部那排汇总数字。**跟列表同条件筛选** ——
        原来它读的是"全部"，页面却写着"基于当前筛选"，数字跟列表对不上（2026-09-28 修）。
        """
        out = {"total": 0, "by_type": {}, "helpful": 0, "unhelpful": 0}
        fc, fp = self._filter_clause(plan_type, keyword, date_from, date_to)
        rows = self._run("SELECT plan_type, COUNT(*) FROM qa_history "
                         "WHERE username = %s" + fc + " GROUP BY plan_type",
                         (username,) + tuple(fp))
        for t, n in rows:
            out["by_type"][t or "general"] = n
            out["total"] += n
        # 反馈要 JOIN 回历史记录才能跟着筛选走（qa_feedback 自己没有类型 / 关键词列）
        fc2, fp2 = self._filter_clause(plan_type, keyword, date_from, date_to, tbl="h.")
        rows = self._run("SELECT f.rating, COUNT(*) FROM qa_feedback f "
                         "JOIN qa_history h ON h.id = f.history_id "
                         "WHERE f.username = %s" + fc2 + " GROUP BY f.rating",
                         (username,) + tuple(fp2))
        for rating, n in rows:
            if rating == "helpful":
                out["helpful"] = n
            elif rating == "unhelpful":
                out["unhelpful"] = n
        return out

    def hot(self, limit=8):
        """热门内容：被问得最多的作物 / 病虫害（来自 hot_content 表）"""
        rows = self._run("""
            SELECT content_type, content_name, search_count
            FROM hot_content ORDER BY search_count DESC, update_time DESC LIMIT %s
        """, (int(limit),))
        return [{"type": r[0], "name": r[1], "count": r[2]} for r in rows]


# 与 plot_store 同款：模块级单例，避免每次请求都新建连接配置
_STORE = None


def get_history_store():
    global _STORE
    if _STORE is None:
        _STORE = HistoryStore()
    return _STORE
