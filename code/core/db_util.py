#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库异常 → 人话（给页面上显示用）
====================================
页面上直接铺 pymysql 的原始异常：

    (2003, "Can't connect to MySQL server on 'localhost'
     ([WinError 10061] 由于目标计算机积极拒绝，无法连接。)")

又长又吓人，农户不关心 WinError 编号。这里统一压成一句人话。
**原始异常仍然原样打到控制台日志**（各 store 里 print 的就是原始 e），排障不受影响。

原本只有 `core/history_store.py` 里自己有一份，2026-09-28 抽出来共用，
plot_store / task_store / feedback_store 也照这个口径显示。
"""
CODE_HELP = {
    2003: "连不上 MySQL（多半是服务没启动，或地址 / 端口不对）",
    1045: "MySQL 账号或密码不对",
    1049: "MySQL 里没有这个库",
    1044: "当前 MySQL 账号没有权限访问这个库",
    2006: "MySQL 连接已断开（服务被重启过？）",
    1054: "数据表结构与代码对不上（缺字段）",
    1062: "数据重复（主键或唯一键冲突）",
    1146: "数据表不存在",
    1292: "填的值不合法（日期格式不对？）",
    1406: "内容太长，超过字段长度限制",
}


def error_text(cfg, e, extra=""):
    """
    cfg  ：该 store 用的 mysql 配置 dict（只有报错里要带库名时才用得到，可为 None）
    e    ：原始异常
    extra：附加说明（如"反馈记录"“地块档案""），拼在兜底文案里
    """
    txt = str(e)
    code = getattr(e, "args", [None])[0]
    if isinstance(code, str):
        code = None
    low = txt.lower()
    if code == 2003 or "can't connect" in low or "10061" in txt or "refused" in low:
        return CODE_HELP[2003]
    if code == 1045 or "access denied" in low:
        return CODE_HELP[1045]
    if code == 1049 or "unknown database" in low:
        db = (cfg or {}).get("database") or (cfg or {}).get("db") or ""
        return "MySQL 里没有 `%s` 这个库" % db if db else CODE_HELP[1049]
    if code == 1044:
        db = (cfg or {}).get("database") or (cfg or {}).get("db") or ""
        return "当前 MySQL 账号没有权限访问 `%s`" % db if db else CODE_HELP[1044]
    if code in CODE_HELP:
        return CODE_HELP[code]
    return ("%s失败：%s" % (extra, txt[:120])) if extra else ("数据库不可用：%s" % txt[:120])
