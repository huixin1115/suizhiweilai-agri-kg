#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据库入口：MySQL（默认） ｜ SQLite（免安装交付版）
==========================================================
**为什么要有这一层**
交付给别人时不能要求对方装 MySQL。项目里每个 store 都是自己 `pymysql.connect(**cfg)`，
这里提供一个统一入口 `connect(cfg)`，按 `config.DB_BACKEND` 返回不同实现：

    DB_BACKEND = "mysql"    → 原样走 pymysql（本机开发用的就是这条）
    DB_BACKEND = "sqlite"   → 走内置 SQLite，单文件、零安装

**SQLite 侧为什么不用改上层 SQL**
`SqliteCursor.execute()` 里把 MySQL 方言就地翻译成 SQLite 能吃的写法，
所以 7 个 store + app.py 里的 SQL **一个字都不用动**。

覆盖的方言（都是本项目实际用到的，逐条核对过）：

| MySQL 写法 | SQLite 写法 |
|---|---|
| `%s` 占位符 | `?` |
| `%%` 转义 | `%` |
| `int ... AUTO_INCREMENT` + `PRIMARY KEY (id)` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| `ENGINE=InnoDB DEFAULT CHARSET=...` | 删除 |
| `COMMENT '...'` | 删除 |
| `UNIQUE KEY n (a,b)` | `UNIQUE (a,b)` |
| `KEY n (a)` | 删除 |
| `ON UPDATE CURRENT_TIMESTAMP` | 删除（SQLite 没有） |
| `ON DUPLICATE KEY UPDATE a = VALUES(a)` | `ON CONFLICT(a,b) DO UPDATE SET a = excluded.a` |
| `NOW()` | `datetime('now','localtime')` |
| `INSERT IGNORE INTO` | `INSERT OR IGNORE INTO` |

⚠ `information_schema` 和 `SHOW TABLES` 这两类**查元数据**的语句没法通用翻译
（参数是表名/列名），由调用方按 `backend()` 分支处理 —— 见 `core/user_admin.py`
的 `_has_column` / `_has_table` 与 `scripts/preflight.py` 的 MySQL 自检段。
"""
import datetime
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config  # noqa: E402

# ---------------------------------------------------------------------------
# upsert 的冲突列：SQLite 的 ON CONFLICT 必须明确列出冲突目标，
# 这里按表名映射（源头上取自各 store 建表语句里的 UNIQUE KEY）
# ---------------------------------------------------------------------------
UPSERT_KEYS = {
    "user_plot": ["username", "plot_name"],                     # uk_user_plot
    "plot_task_log": ["username", "plot_id", "task_date", "task_name"],  # uk_task
    "qa_feedback": ["history_id", "username"],                  # uk_history_user
    "hot_content": ["content_type", "content_name"],            # uk_type_name
    "user_crop_perm": ["username", "crop"],                     # uk_user_crop
    "user_soil_perm": ["username", "soil"],                     # uk_user_soil
    "user": ["username"],                                       # 账号名唯一
}

_NOW_RE = re.compile(r"\bNOW\(\)", re.I)
_INSERT_IGNORE_RE = re.compile(r"(\bINSERT\s+)IGNORE(\s+INTO\b)", re.I)
_CREATE_RE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
_UPSERT_RE = re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.I)

# 建表语句里的逐项替换
_DDL_RULES = [
    (re.compile(r"\s+COMMENT\s*=\s*'(?:[^'\\]|\\.)*'", re.I), ""),
    (re.compile(r"\s+COMMENT\s+'(?:[^'\\]|\\.)*'", re.I), ""),
    # id 主键：MySQL 是「int NOT NULL AUTO_INCREMENT + 单独的 PRIMARY KEY (id)」两行，
    # SQLite 必须合成一行，且 AUTOINCREMENT 只能跟在 INTEGER PRIMARY KEY 后面
    (re.compile(r"`id`\s+int\s+NOT\s+NULL\s+AUTO_INCREMENT\s*,", re.I),
     "`id` INTEGER PRIMARY KEY AUTOINCREMENT,"),
    (re.compile(r",?\s*PRIMARY\s+KEY\s*\(\s*`?id`?\s*\)", re.I), ""),
    (re.compile(r"UNIQUE\s+KEY\s+`[^`]+`\s*\(", re.I), "UNIQUE ("),
    (re.compile(r",\s*KEY\s+`[^`]+`\s*\([^)]*\)", re.I), ""),
    (re.compile(r"\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP", re.I), ""),
    (re.compile(r"\)\s*ENGINE\s*=\s*\w+[^;]*", re.I), ")"),
    (re.compile(r"\b(tinyint|smallint|mediumint)\s*\(\s*\d+\s*\)", re.I), "INTEGER"),
    (re.compile(r"\bint\s*\(\s*\d+\s*\)", re.I), "INTEGER"),
]


def backend():
    """当前后端："mysql" / "sqlite"（大小写不敏感，认不出就退回 mysql）"""
    v = str(getattr(config, "DB_BACKEND", "mysql") or "mysql").strip().lower()
    return "sqlite" if v == "sqlite" else "mysql"


def sqlite_path():
    """SQLite 文件路径（不存在时由调用方 create，这里只负责给路径）"""
    p = getattr(config, "SQLITE_PATH", "") or os.path.join(BASE, "output", "app_data.sqlite3")
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            pass
    return p


def to_sqlite(sql):
    """MySQL SQL → SQLite SQL（幂等，重复调用无害）"""
    s = sql
    # 1) 先把 %% 藏起来，避免下面把 %s 换成 ? 时把 % 吃掉，最后再还原
    s = s.replace("%%", "\x00PCT\x00")
    s = s.replace("%s", "?")
    s = _NOW_RE.sub("datetime('now','localtime')", s)
    s = _INSERT_IGNORE_RE.sub(r"\1OR IGNORE\2", s)

    if _CREATE_RE.search(s):
        for pat, rep in _DDL_RULES:
            s = pat.sub(rep, s)

    if _UPSERT_RE.search(s):
        s = _upsert(s)
    return s.replace("\x00PCT\x00", "%")


def _upsert(sql):
    """ON DUPLICATE KEY UPDATE a = VALUES(a), b = b + 1  →  ON CONFLICT(keys) DO UPDATE SET ..."""
    m = re.search(r"\bINSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+`?(\w+)`?", sql, re.I)
    table = m.group(1).lower() if m else ""
    keys = UPSERT_KEYS.get(table)

    parts = re.split(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", sql, maxsplit=1, flags=re.I)
    head = parts[0].rstrip().rstrip(";").rstrip()
    tail = parts[1] if len(parts) > 1 else ""

    if not keys:
        # 认不出表名：退化成"冲突即忽略"，不会写坏数据
        return head

    sets = []
    for part in tail.split(","):
        part = part.strip().rstrip(";").strip()
        am = re.match(r"`?(\w+)`?\s*=", part)
        if not am:
            continue
        col = am.group(1)
        rhs = part[am.end():].strip()
        rhs = re.sub(r"\bVALUES\s*\(\s*`?(\w+)`?\s*\)", r"excluded.\1", rhs, flags=re.I)
        if re.fullmatch(r"`?%s`?" % re.escape(col), rhs):
            rhs = "excluded.%s" % col
        sets.append("%s = %s" % (col, rhs))

    if not sets:
        return head
    return "%s ON CONFLICT(%s) DO UPDATE SET %s" % (head, ", ".join(keys), ", ".join(sets))


# ---------------------------------------------------------------------------
# 日期类型：pymysql 读 DATE/DATETIME 会返回 datetime 对象，而 sqlite3 默认给字符串。
# 上层有 `it.created_at.strftime(...)` 这种调用（feedback.html / econ_store），
# 不转换会直接 500。这里注册 converter + 开 detect_types，把行为对齐。
# ⚠ converter 必须容错：认不出的格式原样返回字符串，别让整页挂掉。
# ---------------------------------------------------------------------------
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")


def _as_text(v):
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def _conv_datetime(v):
    s = _as_text(v)
    for f in _DT_FORMATS:
        try:
            return datetime.datetime.strptime(s, f)
        except ValueError:
            continue
    return s


def _conv_date(v):
    s = _as_text(v)
    try:
        return datetime.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return s


for _t in ("datetime", "timestamp", "DATETIME", "TIMESTAMP"):
    sqlite3.register_converter(_t, _conv_datetime)
for _t in ("date", "DATE"):
    sqlite3.register_converter(_t, _conv_date)

_DETECT = getattr(sqlite3, "PARSE_DECLTYPES", 0) | getattr(sqlite3, "PARSE_COLNAMES", 0)


# ---------------------------------------------------------------------------
# SQLite 连接与游标：接口对齐 pymysql
# ---------------------------------------------------------------------------
class SqliteCursor(object):
    def __init__(self, conn, as_dict=False):
        self._cur = conn.cursor()
        self._as_dict = as_dict

    def _row(self, r):
        if r is None:
            return None
        return dict(r) if self._as_dict else tuple(r)

    def execute(self, sql, params=None):
        s = to_sqlite(sql)
        try:
            if params is None:
                self._cur.execute(s)
            else:
                self._cur.execute(s, tuple(params))
        except Exception as e:
            # 把翻译后的 SQL 带进异常，排障时一眼能看出是翻译错了还是数据问题
            raise type(e)("%s ｜ SQL: %s" % (e, " ".join(s.split())[:300]))
        return self

    # 有些地方用 executemany
    def executemany(self, sql, seq):
        self._cur.executemany(to_sqlite(sql), [tuple(p) for p in seq])
        return self

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        for r in self._cur:
            yield self._row(r)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class SqliteConn(object):
    """接口对齐 pymysql.Connection（cursor / commit / rollback / close / with）"""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, timeout=15, check_same_thread=False,
                                     detect_types=_DETECT)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")     # 读写并发好一些
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass

    def cursor(self, cls=None):
        # 对齐 pymysql：带参数（DictCursor）→ 返回字典行；不带参数 → 返回元组行
        return SqliteCursor(self._conn, as_dict=cls is not None)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def connect(cfg=None):
    """
    按 config.DB_BACKEND 返回数据库连接。

    cfg 只在 MySQL 模式下用得上（就是各 store 的 MYSQL_CONFIG dict）；
    SQLite 模式下忽略它，用 config.SQLITE_PATH。
    """
    if backend() == "sqlite":
        return SqliteConn(sqlite_path())

    import pymysql
    kwargs = dict(cfg or getattr(config, "MYSQL_CONFIG", {}) or {})
    kwargs.setdefault("connect_timeout", 5)
    kwargs.setdefault("charset", "utf8mb4")
    return pymysql.connect(**kwargs)


def backend_label():
    """自检/页面上显示用"""
    if backend() == "sqlite":
        return "SQLite（免安装内置库）"
    c = getattr(config, "MYSQL_CONFIG", {}) or {}
    return "MySQL %s/%s" % (c.get("host", "?"), c.get("database", "?"))
