#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Neo4j 起不来时的修复脚本
==========================
典型症状：Neo4j 服务起不来，日志里写着

    FileLockException: Unable to obtain lock on file: ...\\data\\databases\\store_lock
    java.nio.file.FileSystemException: ...store_lock: 另一个程序正在使用此文件

原因：上一次 Neo4j 是被**强制关掉**的（关机、任务管理器结束进程、控制台窗口直接叉掉），
      进程残留没释放 store_lock。之后每次启动都会卡在抢锁上，
      表现为「java 进程在跑，但 7687 端口始终不通」。

修复三步（本脚本自动做）：
  1. 结束上次遗留的 Neo4j 窗口及其子进程（只动标题为 "Neo4j Server" 的窗口，不碰别的 java）
  2. 把残留的 store_lock 改名备份（Neo4j 启动时会自动重建，不会丢数据）
  3. 重新启动并等端口就绪

用法：python scripts/fix_neo4j_lock.py
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preflight as pf                                    # noqa: E402

WINDOW_TITLE = "Neo4j Server"       # 与 preflight.start_neo4j 里 start 的标题一致


def kill_neo4j_window():
    """
    只结束我们自己启动的那个 Neo4j 控制台窗口（连同子进程树）。
    不用 taskkill /IM java.exe —— 那会误伤用户其它的 Java 程序。
    """
    cmd = ['taskkill', '/FI', 'WINDOWTITLE eq %s*' % WINDOW_TITLE, '/T', '/F']
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0:
        print("  已结束残留的 Neo4j 窗口（含子进程）")
        return True
    print("  没有找到标题为 %r 的残留窗口（可能已经退干净了）" % WINDOW_TITLE)
    if out:
        print("    taskkill 返回：%s" % out.splitlines()[0][:120])
    return False


def main():
    pf.enable_ansi()
    print(pf.C("\nNeo4j 锁文件修复（store_lock）", "B"))

    bat = pf.find_neo4j_bat()
    if not bat:
        print(pf.C("  没找到 neo4j.bat，无法继续。请确认 Neo4j 已安装。", "r"))
        return 1
    neo_home = os.path.dirname(os.path.dirname(bat))
    lock = os.path.join(neo_home, "data", "databases", "store_lock")
    print("  Neo4j 目录：%s" % neo_home)

    print(pf.C("\n[1/4] 结束残留进程", "b"))
    kill_neo4j_window()
    time.sleep(4)

    print(pf.C("\n[2/4] 清理残留的 store_lock", "b"))
    moved = None
    if not os.path.isfile(lock):
        print("  没有 store_lock 文件，跳过")
    else:
        # 先确认它真的没被占用（能打开就能改名）
        try:
            with open(lock, "r+b"):
                pass
            busy = False
        except Exception:
            busy = True
        if busy:
            print(pf.C("  store_lock 仍被占用 —— 可能有别的进程在用它。", "r"))
            print("  请手动确认没有其它 Neo4j / Java 进程在跑，再重试；")
            print("  或重启电脑后再执行本脚本。")
            return 1
        moved = lock + ".bak_" + time.strftime("%Y%m%d_%H%M%S")
        shutil.move(lock, moved)
        print("  已改名备份为：%s" % os.path.basename(moved))
        print(pf.C("  （Neo4j 启动时会自动重建 store_lock，不会丢数据）", "d"))

    print(pf.C("\n[3/4] 启动 Neo4j", "b"))
    if not pf.start_neo4j(bat):
        print(pf.C("\n  启动失败。请看上面的提示，或查看日志：", "r"))
        print("    %s" % os.path.join(neo_home, "logs", "debug.log"))
        if moved:
            print("  如需还原：把 %s 改回 store_lock" % os.path.basename(moved))
        return 1

    print(pf.C("\n[4/4] 验证图谱数据", "b"))
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import config
        from core.neo4j_conn import Neo4jConnection
        c = Neo4jConnection(config.NEO4J_URI, config.NEO4J_USER, config.NEO4J_PWD)
        st = c.graph_stats()
        c.close()
        print("  图谱规模：%s 节点 / %s 关系 / %s 类实体 / %s 类关系"
              % (st.get("nodes"), st.get("relations"),
                 len(st.get("labels") or []), len(st.get("rel_types") or [])))
        if (st.get("nodes") or 0) < 1000:
            print(pf.C("  数据偏少！完整数据应为 1320 节点 / 5693 关系。"
                       "重跑：python scripts/08_import_extended_kg.py", "y"))
    except Exception as e:
        print(pf.C("  连接上了但查询失败：%s" % str(e)[:120], "y"))

    print(pf.C("\n修复完成，现在可以运行「一键启动.bat」了。\n", "g"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
