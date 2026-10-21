#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管理端 / 用户端与作物·土质权限（UserAdminStore）
=================================================
本模块让系统有了两种角色，并把\"谁能种什么、能用什么土质\"交给管理端决定：

    管理端（admin）   —— 可以添加用户端账号、指定其可种作物与可用土质、
                        重置密码、纳入/解除管理
    用户端（user）    —— 建档时只能选被授权的作物与土质；没被授权过则不受限制

四处表结构全部**首次使用时自动创建 / 自动补列**，不依赖手工执行 SQL：

    user.role        新增列，admin | user（老账号在迁移时统一视为管理端）
    user.admin_id    新增列，记录这个用户端归哪个管理端管
    user.create_time 新增列，管理端列表要显示建号时间
    user_crop_perm   新建表，某个用户端被允许种哪些作物（一行一种）
    user_soil_perm   新建表，某个用户端能用哪些土质（一行一种）

两张授权表结构一模一样，读写共用一套 `perms_of / set_perms / _replace_perms`。
MySQL 不可用时整体安全降级：查询返回空、写入返回失败，
**绝不抛异常打断页面**（与 plot_store / history_store 同一套风格）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                     # noqa: E402

ROLE_ADMIN = "admin"
ROLE_USER = "user"

# 账号表：MySQL 侧原来是手工建的（见 database/hot_content.sql / 部署文档），
# 这里补一句 IF NOT EXISTS —— 老库里有就不动，全新库（尤其是 SQLite 交付版）能自动建好，
# 否则对方第一次跑登录/注册全废（ensure_schema 只补列，不建表）
DDL_USER = """
CREATE TABLE IF NOT EXISTS `user` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '用户名',
  `password` varchar(255) NOT NULL COMMENT '密码哈希（pbkdf2:sha256）',
  `role` varchar(16) NOT NULL DEFAULT 'user' COMMENT 'admin=管理端 user=用户端',
  `admin_id` int DEFAULT NULL COMMENT '归属的管理端用户 id，NULL 表示未归属',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账号表'
"""

DDL_PERM = """
CREATE TABLE IF NOT EXISTS `user_crop_perm` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '被授权的用户端账号',
  `crop` varchar(100) NOT NULL COMMENT '允许种植的作物名',
  `granted_by` varchar(50) DEFAULT '' COMMENT '由哪个管理端授权',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_crop` (`username`, `crop`),
  KEY `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户端可种植作物授权表'
"""

DDL_PERM_SOIL = """
CREATE TABLE IF NOT EXISTS `user_soil_perm` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `username` varchar(50) NOT NULL COMMENT '被授权的用户端账号',
  `soil` varchar(100) NOT NULL COMMENT '允许使用的土质名',
  `granted_by` varchar(50) DEFAULT '' COMMENT '由哪个管理端授权',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_soil` (`username`, `soil`),
  KEY `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户端可用土质授权表'
"""

# 两张授权表结构一致，读写走同一套代码：kind -> (表名, 值列名, 唯一键名)
PERM_TABLES = {
    "crops": ("user_crop_perm", "crop", "uk_user_crop"),
    "soils": ("user_soil_perm", "soil", "uk_user_soil"),
}
PERM_LABEL = {"crops": "作物", "soils": "土质"}

MAX_CROPS = 200          # 单个用户端最多授权多少种作物（防止误点全选铺满）
MAX_SOILS = 60           # 土质一共 30 种，给一倍余量


class UserAdminStore:
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

    def _run(self, sql, params=None, fetch=True, conn=None):
        """
        统一出口。返回值语义：
            查询失败 -> []      写失败 -> None      lastrowid（可为 0）
        写操作要用 `is not None` 判断成功（历史模块踩过 \"失败返回 0 被当成成功\" 的坑）。
        """
        own = conn is None
        if own and not self.ensure_schema():
            return [] if fetch else None
        c = conn or self._connect()
        try:
            with c.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall() if fetch else None
                last = cur.lastrowid
                if not fetch:
                    c.commit()
                return rows if fetch else last
        except Exception as e:
            print("WARN: 用户/权限操作失败：%s" % e)
            try:
                c.rollback()
            except Exception:
                pass
            return [] if fetch else None
        finally:
            if own:
                c.close()

    def _has_column(self, conn, table, col):
        from core import db
        if db.backend() == "sqlite":
            # SQLite 没有 information_schema，用表值函数 pragma_table_info
            rows = self._run(
                "SELECT COUNT(*) FROM pragma_table_info(%s) WHERE name = %s",
                (table, col), conn=conn)
        else:
            rows = self._run(
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                (self.cfg.get("database"), table, col), conn=conn)
        try:
            return int(rows[0][0]) > 0
        except Exception:
            return False

    def _has_table(self, conn, table):
        from core import db
        if db.backend() == "sqlite":
            rows = self._run(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = %s",
                (table,), conn=conn)
        else:
            rows = self._run(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self.cfg.get("database"), table), conn=conn)
        try:
            return int(rows[0][0]) > 0
        except Exception:
            return False

    def ensure_schema(self):
        """
        建表 + 补列。只做一次，失败即标记不可用（页面照常能跑）。
        补列时要注意：**只有在 role 列是新加的时候才把现有账号提升为管理端**，
        否则以后每次启动都会把普通用户又改成管理员。
        """
        if self._ready or not self.available:
            return self.available
        try:
            conn = self._connect()
        except Exception as e:
            self.available = False
            self.error = str(e)
            return False
        try:
            with conn.cursor() as cur:
                cur.execute(DDL_USER)        # 全新库没有 user 表，先建（IF NOT EXISTS）
                cur.execute(DDL_PERM)
                cur.execute(DDL_PERM_SOIL)
            conn.commit()
            fresh_install = not self._has_column(conn, "user", "role")
            if fresh_install:
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE `user` ADD COLUMN `role` varchar(16) "
                        "NOT NULL DEFAULT 'user' COMMENT 'admin=管理端 user=用户端'")
                    # 老账号（迁移前就存在的）本来就是系统的使用者，统一视为管理端，
                    # 免得升级之后连自己都进不去管理端
                    cur.execute("UPDATE `user` SET `role` = %s", (ROLE_ADMIN,))
                    n = cur.rowcount
                conn.commit()
                print("INFO: user 表已补 role 列，原有 %d 个账号已设为管理端" % n)
            if not self._has_column(conn, "user", "admin_id"):
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE `user` ADD COLUMN `admin_id` int DEFAULT NULL "
                        "COMMENT '归属的管理端用户 id，NULL 表示未归属'")
                conn.commit()
            if not self._has_column(conn, "user", "create_time"):
                # 老表里没有建号时间，管理端列表里"哪个账号是什么时候加的"就没法看
                with conn.cursor() as cur:
                    cur.execute(
                        "ALTER TABLE `user` ADD COLUMN `create_time` datetime "
                        "DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'")
                conn.commit()
            self._ready = True
            return True
        except Exception as e:
            self.available = False
            self.error = str(e)
            print("WARN: 用户/权限表初始化失败：%s" % e)
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------ 读
    def get_user(self, username):
        """{'id','username','role','admin_id'}；没有或查不到返回 None"""
        if not username:
            return None
        rows = self._run(
            "SELECT `id`, `username`, `role`, `admin_id` FROM `user` "
            "WHERE `username` = %s", (username,))
        if not rows:
            return None
        r = rows[0]
        role = (r[2] or ROLE_USER)
        return {"id": r[0], "username": r[1],
                "role": ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER,
                "admin_id": r[3]}

    def role_of(self, username):
        """不确定的一律当用户端：管理端权限只可能被显式授予"""
        u = self.get_user(username)
        return u["role"] if u else ROLE_USER

    def is_admin(self, username):
        return self.role_of(username) == ROLE_ADMIN

    def admin_name_of(self, username):
        """这个用户端归哪个管理端管；未归属返回 ''"""
        u = self.get_user(username)
        if not u or not u.get("admin_id"):
            return ""
        rows = self._run("SELECT `username` FROM `user` WHERE `id` = %s",
                         (u["admin_id"],))
        return rows[0][0] if rows else ""

    # ---- 作物 / 土质权限：两张表结构完全一样，用一套通用读写 ----
    def perms_of(self, username, kind):
        """
        该账号被授权的某一类（crops / soils）。
        **返回空列表表示不受限制**（不是"什么都不许选"）。
        """
        table, col, _ = PERM_TABLES[kind]
        rows = self._run(
            "SELECT `%s` FROM `%s` WHERE `username` = %%s "
            "ORDER BY `create_time`, `id`" % (col, table), (username,))
        return [r[0] for r in rows if r[0]]

    def crops_of(self, username):
        return self.perms_of(username, "crops")

    def soils_of(self, username):
        return self.perms_of(username, "soils")

    def perm_summary(self, username):
        """{'crops': [...], 'soils': [...]}，两个都给前端用"""
        return {"crops": self.crops_of(username), "soils": self.soils_of(username)}

    def is_limited(self, username):
        return bool(self.crops_of(username) or self.soils_of(username))

    def managed_users(self, admin_username):
        """
        我管理的用户端列表（含地块数、授权作物、注册时间）
        没在我名下的账号不会出现在这里 —— 归属是数据边界
        """
        me = self.get_user(admin_username)
        if not me or me["role"] != ROLE_ADMIN:
            return []
        rows = self._run(
            "SELECT `username`, `create_time` FROM `user` "
            "WHERE `admin_id` = %s AND `role` = %s ORDER BY `id` DESC",
            (me["id"], ROLE_USER))
        if not rows:
            # 也可能是老库没有 create_time 列（ALTER 没成功），退回不带时间的查询
            rows = [(r[0], "") for r in self._run(
                "SELECT `username` FROM `user` "
                "WHERE `admin_id` = %s AND `role` = %s ORDER BY `id` DESC",
                (me["id"], ROLE_USER))]
        out = []
        for r in rows:
            name = r[0]
            out.append({
                "username": name,
                "created": str(r[1])[:16] if r[1] else "",
                "crops": self.crops_of(name),
                "soils": self.soils_of(name),
                "plot_count": self._count("user_plot", name),
                "ask_count": self._count("qa_history", name),
            })
        return out

    def assignable_users(self, admin_username):
        """
        「可以纳入我管理」的用户端：
          · 还没归属任何管理端的（status=free）
          · 已经归别的管理端的（status=other，纳入即为**转入**）
        不带自己名下已有的账号（那些在上面的列表里）。
        """
        me = self.get_user(admin_username)
        if not me or me["role"] != ROLE_ADMIN:
            return []
        rows = self._run(
            "SELECT `username`, `admin_id` FROM `user` "
            "WHERE `role` = %s AND (`admin_id` IS NULL OR `admin_id` <> %s) "
            "ORDER BY `id`", (ROLE_USER, me["id"]))
        out = []
        for r in rows:
            name, aid = r[0], r[1]
            holder = ""
            if aid:
                h = self._run("SELECT `username` FROM `user` WHERE `id` = %s", (aid,))
                holder = h[0][0] if h else ""
            out.append({"username": name,
                        "status": "other" if holder else "free",
                        "holder": holder})
        return out

    def _count(self, table, username):
        """表可能还没被创建（例如从没问过问题），查不到就当 0"""
        rows = self._run("SELECT COUNT(*) FROM `%s` WHERE `username` = %%s"
                         % table, (username,))
        try:
            return int(rows[0][0])
        except Exception:
            return 0

    def stats(self, admin_username=""):
        total = self._run("SELECT COUNT(*) FROM `user`")
        admins = self._run("SELECT COUNT(*) FROM `user` WHERE `role` = %s",
                           (ROLE_ADMIN,))
        mine = self.managed_users(admin_username) if admin_username else []
        return {
            "total_users": int(total[0][0]) if total else 0,
            "total_admins": int(admins[0][0]) if admins else 0,
            "managed": len(mine),
            "limited": len([u for u in mine if u["crops"]]),
            "soil_limited": len([u for u in mine if u["soils"]]),
        }

    # ------------------------------------------------------------ 写
    def create_user(self, admin_username, username, password, crops=None, soils=None):
        """
        管理端添加一个用户端账号，并直接归属到自己名下。
        crops / soils 是"这个账号能种什么、能用哪些土质"，留空表示不限制。
        返回 (ok, message)。
        """
        username = (username or "").strip()
        password = (password or "").strip()
        if not username or not password:
            return False, "账号和密码都不能为空"
        if len(username) < 3:
            return False, "账号至少 3 位"
        if len(password) < 6:
            return False, "密码至少 6 位"
        me = self.get_user(admin_username)
        if not me or me["role"] != ROLE_ADMIN:
            return False, "只有管理端可以添加用户端账号"
        if username == admin_username:
            return False, "不能添加自己"
        if not self.ensure_schema():
            return False, "数据库不可用，无法添加账号：%s" % self.error
        if self.get_user(username):
            return False, "账号「%s」已存在" % username

        from werkzeug.security import generate_password_hash
        pwd = generate_password_hash(password, method="pbkdf2:sha256")
        conn = self._connect()
        try:
            ok = self._run(
                "INSERT INTO `user` (`username`, `password`, `role`, `admin_id`) "
                "VALUES (%s, %s, %s, %s)",
                (username, pwd, ROLE_USER, me["id"]), fetch=False, conn=conn)
            if ok is None:
                return False, "写入失败，请检查数据库"
            self._replace_perms(conn, username, "crops", self._clean_list(crops),
                                admin_username)
            self._replace_perms(conn, username, "soils", self._clean_list(soils),
                                admin_username)
            extra = []
            if self._clean_list(crops):
                extra.append("可种 %d 种作物" % len(self._clean_list(crops)))
            if self._clean_list(soils):
                extra.append("指定 %d 种土质" % len(self._clean_list(soils)))
            return True, ("已创建用户端「%s」，并归到「%s」名下%s"
                          % (username, admin_username,
                             ("（" + "，".join(extra) + "）") if extra else ""))
        except Exception as e:
            return False, "创建失败：%s" % e
        finally:
            conn.close()

    def attach(self, admin_username, username):
        """
        把别处的用户端纳入本管理端：
          · 未归属的 → 直接收进来
          · 已归其他管理端的 → **转入**本管理端（原管理端随之失去管理权）
        多乡镇/多负责人交接时用得着；转入后原有的作物与土质授权保持不变。
        """
        username = (username or "").strip()
        me = self.get_user(admin_username)
        if not me or me["role"] != ROLE_ADMIN:
            return False, "只有管理端可以纳入账号"
        target = self.get_user(username)
        if not target:
            return False, "账号「%s」不存在" % username
        if target["role"] == ROLE_ADMIN:
            return False, "管理端账号不需要纳入管理"
        if target["admin_id"] == me["id"]:
            return False, "「%s」已经在你名下" % username
        old_holder = self.admin_name_of(username)
        r = self._run("UPDATE `user` SET `admin_id` = %s WHERE `username` = %s",
                      (me["id"], username), fetch=False)
        if r is None:
            return False, "纳入失败，请检查数据库"
        if old_holder:
            return True, "已把「%s」从「%s」名下转入你的管理（原有授权保持不变）" % (
                username, old_holder)
        return True, "已把「%s」纳入你的管理，现在可以给它指定可种作物与土质" % username

    def _replace_perms(self, conn, username, kind, values, granted_by):
        """整表替换某一类授权（在同一个事务里删+插）"""
        table, col, _ = PERM_TABLES[kind]
        r = self._run("DELETE FROM `%s` WHERE `username` = %%s" % table,
                      (username,), fetch=False, conn=conn)
        if r is None:
            return False
        for v in values:
            self._run(
                "INSERT IGNORE INTO `%s` (`username`, `%s`, `granted_by`) "
                "VALUES (%%s, %%s, %%s)" % (table, col),
                (username, v, granted_by), fetch=False, conn=conn)
        return True

    def set_perms(self, admin_username, username, crops=None, soils=None):
        """
        重设授权；传空列表 = 取消该类限制（恢复为不限）。
        **传 None 表示这一类不动** —— 前端只改土质时不会把作物权限抹掉。
        """
        ok, msg = self._check_my_user(admin_username, username)
        if not ok:
            return False, msg
        jobs = []
        if crops is not None:
            c = self._clean_list(crops)
            if len(c) > MAX_CROPS:
                return False, "最多只能授权 %d 种作物" % MAX_CROPS
            jobs.append(("crops", c))
        if soils is not None:
            s = self._clean_list(soils)
            if len(s) > MAX_SOILS:
                return False, "最多只能授权 %d 种土质" % MAX_SOILS
            jobs.append(("soils", s))
        if not jobs:
            return False, "没有要修改的内容"

        conn = self._connect()
        try:
            for kind, values in jobs:
                if not self._replace_perms(conn, username, kind, values, admin_username):
                    return False, "写入失败，请检查数据库"
        finally:
            conn.close()

        parts = []
        for kind, values in jobs:
            label = PERM_LABEL[kind]
            if not values:
                parts.append("取消%s限制" % label)
            else:
                parts.append("%s %d 种（%s）" % (
                    label, len(values),
                    "、".join(values[:5]) + ("…" if len(values) > 5 else "")))
        return True, "已更新「%s」：%s" % (username, "；".join(parts))

    def set_crops(self, admin_username, username, crops):
        """兼容旧调用：只改作物"""
        return self.set_perms(admin_username, username, crops=crops)

    def set_soils(self, admin_username, username, soils):
        """只改土质"""
        return self.set_perms(admin_username, username, soils=soils)

    def reset_password(self, admin_username, username, new_password):
        ok, msg = self._check_my_user(admin_username, username)
        if not ok:
            return False, msg
        new_password = (new_password or "").strip()
        if len(new_password) < 6:
            return False, "新密码至少 6 位"
        from werkzeug.security import generate_password_hash
        pwd = generate_password_hash(new_password, method="pbkdf2:sha256")
        r = self._run("UPDATE `user` SET `password` = %s WHERE `username` = %s",
                      (pwd, username), fetch=False)
        if r is None:
            return False, "重置失败，请检查数据库"
        return True, "已重置「%s」的密码" % username

    def detach(self, admin_username, username):
        """解除管理：账号保留、作物与土质限制一并取消，之后不再归我管"""
        ok, msg = self._check_my_user(admin_username, username)
        if not ok:
            return False, msg
        r1 = self._run("UPDATE `user` SET `admin_id` = NULL WHERE `username` = %s",
                       (username,), fetch=False)
        if r1 is None:
            return False, "操作失败，请检查数据库"
        for kind in PERM_TABLES:
            self._run("DELETE FROM `%s` WHERE `username` = %%s" % PERM_TABLES[kind][0],
                      (username,), fetch=False)
        return True, "已解除对「%s」的管理（账号保留，作物与土质限制一并取消）" % username

    def delete_user(self, admin_username, username):
        """
        删除用户端账号：连同它的地块、历史、反馈、行为流水、授权一起清掉。
        只能删自己名下的 —— 归属既是分组也是数据边界。
        """
        ok, msg = self._check_my_user(admin_username, username)
        if not ok:
            return False, msg
        conn = self._connect()
        try:
            for sql in ("DELETE FROM `user_crop_perm` WHERE `username` = %s",
                        "DELETE FROM `user_soil_perm` WHERE `username` = %s",
                        "DELETE FROM `user_plot` WHERE `username` = %s",
                        "DELETE FROM `qa_feedback` WHERE `username` = %s",
                        "DELETE FROM `qa_history` WHERE `username` = %s",
                        "DELETE FROM `user_behavior` WHERE `username` = %s"):
                self._run(sql, (username,), fetch=False, conn=conn)
            r = self._run("DELETE FROM `user` WHERE `username` = %s AND `admin_id` = %s",
                          (username, self.get_user(admin_username)["id"]),
                          fetch=False, conn=conn)
            if r is None:
                return False, "删除失败，请检查数据库"
        finally:
            conn.close()
        return True, "已删除用户端「%s」及其名下的地块与历史记录" % username

    # ------------------------------------------------------------ 内部
    def _clean_list(self, values):
        """去空、去重、去首尾空格，保持传入顺序（作物与土质共用）"""
        out, seen = [], set()
        for c in (values or []):
            c = str(c or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    def _check_my_user(self, admin_username, username):
        """确认 username 确实是这个管理端名下的用户端（防越权操作别人的账号）"""
        me = self.get_user(admin_username)
        if not me or me["role"] != ROLE_ADMIN:
            return False, "只有管理端可以执行该操作"
        target = self.get_user(username)
        if not target:
            return False, "账号「%s」不存在" % username
        if target["role"] == ROLE_ADMIN:
            return False, "管理端账号不能被这样管理"
        if target["admin_id"] != me["id"]:
            return False, "「%s」不在你名下，无权操作" % username
        return True, ""


if __name__ == "__main__":
    s = UserAdminStore()
    print("可用:", s.available, "| 初始化:", s.ensure_schema())
    print("账号清单:")
    for r in s._run("SELECT `id`,`username`,`role`,`admin_id` FROM `user`"):
        print("   ", r)
    print("作物授权:")
    for r in s._run("SELECT `username`,`crop`,`granted_by` FROM `user_crop_perm`"):
        print("   ", r)
    print("土质授权:")
    for r in s._run("SELECT `username`,`soil`,`granted_by` FROM `user_soil_perm`"):
        print("   ", r)
