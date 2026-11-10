#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键启动前的环境自检 + 启动服务
====================================
演示当天最怕的不是功能不行，是"Neo4j 没起 / MySQL 连不上 / 依赖缺了 / 图谱数据被清了"。
这个脚本把这些检查全做一遍，**先给结论再启动**，有问题还会给出具体修复命令。

用法（一般直接双击项目根目录的「一键启动.bat」即可）：

    python scripts/preflight.py               # 自检 + 启动 + 自动开浏览器
    python scripts/preflight.py --check       # 只自检，不启动
    python scripts/preflight.py --no-llm      # 跳过"大模型连通性"那一步（省几秒）
    python scripts/preflight.py --no-browser  # 启动但不自动开浏览器
    python scripts/preflight.py --port 8000   # 换端口（默认 5000，被占用会自动顺延）
    python scripts/preflight.py --lan         # 绑定 0.0.0.0，同一局域网其他设备可访问
    python scripts/preflight.py --debug       # 开 Flask 调试模式（改代码自动重启）
    python scripts/preflight.py --force       # 即使致命项没过也强行启动

退出码：0 = 正常启动并退出；1 = 自检未通过（未启动）；2 = 参数错误
"""
import argparse
import glob
import importlib
import importlib.metadata as md
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(CODE_DIR)
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

# ---------------------------------------------------------------- 输出
# 结论分三档：致命（不启动）/ 警告（继续启动但功能会缺）/ 提示
FATAL, WARN, INFO = "FATAL", "WARN", "INFO"
RESULTS = []          # [(level, 标题, 说明)]
ANSI = False
CONFIG = None         # 由 check_config() 填入；为 None 表示配置不可用，后续检查跳过


def enable_ansi():
    """让 Windows 控制台支持彩色（失败就退化成无色，不影响使用）"""
    global ANSI
    if os.name != "nt":
        ANSI = True
        return
    try:
        import ctypes
        # 窗口标题用 Unicode 接口设置：.bat 里写中文会被 cmd 按本地代码页读歪
        try:
            ctypes.windll.kernel32.SetConsoleTitleW("穗知未来——农业知识图谱智能决策服务系统")
        except Exception:
            pass
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
            ANSI = True
    except Exception:
        ANSI = False


def C(text, color):
    if not ANSI:
        return text
    return {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m",
            "b": "\033[36m", "d": "\033[90m", "B": "\033[1m"}.get(color, "") + text + "\033[0m"


TAG = {
    "OK":   ("[ OK ]", "g"),
    "WARN": ("[WARN]", "y"),
    "FAIL": ("[FAIL]", "r"),
    "SKIP": ("[SKIP]", "d"),
}
# 用纯 ASCII 标记，避免中文控制台字体缺字形（✓ 这类字符在 GBK 下还会直接崩）


def say(mark, title, detail="", indent="      "):
    tag, color = TAG[mark]
    print("%s %s%s" % (C(tag, color), title, ("  " + C(detail, "d")) if detail else ""))
    if mark in ("FAIL", "WARN"):
        RESULTS.append((WARN if mark == "WARN" else FATAL, title, detail))


def head(text):
    print("\n" + C("── " + text + " " + "─" * max(0, 62 - len(text)), "b"))


# ---------------------------------------------------------------- 检查项
def check_python():
    head("1/7  运行环境")
    v = sys.version_info
    if (v.major, v.minor) < (3, 8):
        say("FAIL", "Python 版本过低", "需要 3.8+，当前 %d.%d" % (v.major, v.minor))
    else:
        say("OK", "Python %d.%d.%d" % (v.major, v.minor, v.micro), sys.executable)
    if not os.path.isfile(os.path.join(CODE_DIR, "app.py")):
        say("FAIL", "找不到 app.py", "本脚本应放在 <项目>/code/scripts/ 下")
    return True


# 依赖 -> (import 名, 显示名, 版本要求, 是否必需)
DEPS = [
    ("flask",                 "Flask",               None,           True),
    ("pymysql",               "PyMySQL",             None,           True),
    ("neo4j",                 "neo4j",               None,           True),
    ("requests",              "requests",            None,           True),
    ("PIL",                   "Pillow",              None,           True),
    ("docx",                  "python-docx",         None,           False),
    ("fpdf",                  "fpdf2",               None,           False),
    ("numpy",                 "numpy",               "<2",           True),
    ("sklearn",               "scikit-learn",        None,           False),
    ("torch",                 "torch",               None,           False),
    ("transformers",          "transformers",        None,           False),
    ("sentence_transformers", "sentence-transformers", None,         False),
]


def check_deps():
    head("2/7  Python 依赖")
    missing_hard, missing_soft = [], []
    for mod, name, rule, need in DEPS:
        try:
            ver = md.version(name)
        except Exception:
            ver = ""
        if importlib.util.find_spec(mod) is None:
            say("FAIL" if need else "WARN", "缺少 %s" % name,
                "pip install %s" % name)
            (missing_hard if need else missing_soft).append(name)
            continue
        if not ver:
            try:
                ver = getattr(importlib.import_module(mod), "__version__", "?")
            except Exception:
                ver = "?"
        # numpy 必须 < 2
        if rule == "<2" and ver and not ver.startswith("1."):
            say("FAIL", "numpy 版本不对：%s" % ver,
                "torch 2.0.1 按 numpy 1.x 编译，装 2.x 会让向量检索失效 → pip install \"numpy<2\"")
            missing_hard.append("numpy<2")
            continue
        say("OK", "%-20s %s" % (name, ver))
    if missing_hard or missing_soft:
        cmd = "pip install " + " ".join('"%s"' % x if "<" in x else x
                                        for x in (missing_hard + missing_soft))
        print("      " + C("一键补装：" + cmd, "y"))
    return not missing_hard


def check_config():
    global CONFIG
    head("3/7  配置文件")
    try:
        import config
    except Exception as e:
        say("FAIL", "config.py 导入失败", str(e)[:120])
        return None
    CONFIG = config
    say("OK", "config.py 已加载")
    local = os.path.join(CODE_DIR, "config_local.py")
    if os.path.isfile(local):
        say("OK", "config_local.py 已加载", "私有配置（Key 写在这里，优先级更高）")
    else:
        say("WARN", "没有 config_local.py", "API Key 若写在 config.py 里，注意别误提交")

    cfg = config.resolve_llm()
    if cfg["ready"]:
        say("OK", "大模型：%s" % cfg["label"], "%s ｜ %s" % (cfg["model"], cfg["base_url"]))
        th = (cfg.get("params") or {}).get("thinking", {}).get("type")
        if th == "disabled":
            say("OK", "思维链已关闭", "豆包默认会先输出思维链，关掉后同一问题快约 6 倍")
        say("OK", "超时 %s 秒 / 重试 %s 次"
            % (getattr(config, "LLM_TIMEOUT", "?"), getattr(config, "LLM_RETRIES", "?")))
    else:
        say("WARN", "大模型未配置完整",
            "将降级为「图谱结构化输出」，功能不受影响；补 LLM_API_KEY 即可")

    vis = config.resolve_vision()
    if vis["ready"]:
        say("OK", "视觉模型（拍照识病）", "%s（%s）"
            % (vis["model"], "复用主模型" if vis["source"] == "main" else "专用配置"))
    else:
        say("WARN", "视觉模型不可用", str(vis.get("reason"))[:90])
    return cfg


# ---------------------------------------------------------------- Neo4j 自启
# 演示当天最容易忘的就是"Neo4j 没开"，脚本顺手把它拉起来
# 免安装交付版：Neo4j 与 JRE 一起放在项目根的 runtime/ 下（见 README），
# 先认它们，认不到再找系统里装的 —— 这样同一份代码在本机和交付版都能用
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJ_ROOT = os.path.dirname(os.path.dirname(_SCRIPTS_DIR))
PORTABLE_NEO4J = os.path.join(_PROJ_ROOT, "runtime", "neo4j")
PORTABLE_JRE = os.path.join(_PROJ_ROOT, "runtime", "jre")

NEO4J_ROOTS = [PORTABLE_NEO4J,
               r"D:\neo4j", r"E:\neo4j", r"C:\neo4j", r"D:\neo4j-community",
               r"C:\Program Files\Neo4j", r"D:\Program Files\Neo4j"]


def find_neo4j_bat():
    """在本机常见位置找 neo4j.bat（Neo4j 社区版解压即用，路径各家不同）"""
    for root in NEO4J_ROOTS:
        for p in glob.glob(os.path.join(root, "*", "bin", "neo4j.bat")):
            return os.path.abspath(p)
        p = os.path.join(root, "bin", "neo4j.bat")
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None


def port_open(host, port, timeout=1.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def start_neo4j(bat, wait=75):
    """
    用 start 另开一个窗口跑 `neo4j.bat console`：
    - 不阻塞当前窗口
    - 脚本退出后 Neo4j 仍在跑（下次启动就秒进了）
    """
    env = os.environ.copy()
    if os.path.isdir(PORTABLE_JRE):
        # 交付版自带 JRE：不依赖对方机器装没装 Java
        env["JAVA_HOME"] = PORTABLE_JRE
        print("      使用包内 JRE：%s" % PORTABLE_JRE)
    env["NEO4J_HOME"] = os.path.dirname(os.path.dirname(bat))

    # ⚠ 别用 `start "Neo4j Server" "... neo4j.bat" console` 那套（原来就是这么写的）：
    #   start 依赖"当前进程有可用的控制台窗口"，从脚本/服务/后台进程里调用时会**静默失败** ——
    #   Neo4j 压根没起来，却要干等满 75 秒才报超时，查半天查不出原因。
    #   改成直接 Popen 后台跑 + 日志落文件：任何环境都能起，出问题也有日志可看。
    neo_home = os.path.dirname(os.path.dirname(bat))
    log_path = os.path.join(neo_home, "neo4j-console.log")
    print("      正在启动 Neo4j（后台运行，日志：%s）…" % log_path)
    try:
        logf = open(log_path, "ab")
        subprocess.Popen([bat, "console"], cwd=os.path.dirname(bat), env=env,
                         stdout=logf, stderr=logf, shell=False)
    except Exception as e:
        print("      启动失败：%s" % str(e)[:110])
        return False
    print("      等待端口 7687 就绪（首次启动约 20~40 秒）", end="", flush=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        if port_open("127.0.0.1", 7687):
            print("  就绪（%.0f 秒）" % (time.time() - t0))
            time.sleep(1.5)                 # 端口通了再给一点时间完成初始化
            return True
        print(".", end="", flush=True)
        time.sleep(1.5)
    print("  超时")
    return False


def check_neo4j(auto_start=True):
    head("4/7  Neo4j 知识图谱")
    if CONFIG is None:
        say("SKIP", "配置不可用，跳过")
        return False

    def _connect():
        from core.neo4j_conn import Neo4jConnection
        c = Neo4jConnection(CONFIG.NEO4J_URI, CONFIG.NEO4J_USER, CONFIG.NEO4J_PWD)
        st = c.graph_stats()
        c.close()
        return st

    started = False
    try:
        st = _connect()
    except Exception as first_err:
        # 连不上：先看是不是服务没启动，能自动拉起来就拉起来
        bat = find_neo4j_bat()

        # ⚠ Neo4j 的 Windows 启动脚本（neo4j.bat）处理不了非 ASCII 路径：
        #   它会把 java 路径传成乱码，报 "CreateProcess error=2, 系统找不到指定的文件"，
        #   而且实测 subst 虚拟盘也救不了（bat 内部会把路径展开回去）。
        #   与其让人对着乱码猜，不如在这里说清楚。
        if bat and any(ord(ch) > 127 for ch in bat):
            say("FAIL", "项目所在路径含中文，Neo4j 起不来",
                "把整个文件夹移到纯英文路径下（例如 D:\\agri-kg）再启动")
            print("      " + C("当前 Neo4j 路径：%s" % bat, "d"))
            print("      " + C("说明：这是 Neo4j 的 Windows 脚本自身不支持中文路径，"
                                "与项目代码无关", "d"))
            return False

        if auto_start and bat and not port_open("127.0.0.1", 7687):
            # 这里**不**记 WARN：能自动拉起来就不算问题（拉起来后下面会记一条 OK）。
            # 否则自检汇总里会一直挂着"Neo4j 没在运行"，明明起来了却让人以为没起成功。
            started = start_neo4j(bat)
        if not started:
            say("FAIL", "连不上 Neo4j", str(first_err).replace("\n", " ")[:110])
            if bat:
                print("      " + C("启动命令：\"%s\" console" % bat, "y"))
            print("      " + C("若反复起不来：多半是上次异常退出残留了锁文件"
                                "（store_lock）。执行下面这条会自动清理并重启：", "y"))
            print("      " + C("    python scripts\\fix_neo4j_lock.py", "b"))
            if not bat:
                print("      " + C("（没找到 neo4j.bat，确认 Neo4j 已安装，"
                                    "端口 7687 未被占用）", "d"))
            return False
        try:
            st = _connect()
        except Exception as e:
            say("FAIL", "Neo4j 起来了但仍连不上", str(e).replace("\n", " ")[:110])
            return False
        say("OK", "Neo4j 已由本脚本自动启动")
    else:
        say("OK", "Neo4j 已在运行")

    n, r = st.get("nodes", 0), st.get("relations", 0)
    nl, rl = len(st.get("labels") or []), len(st.get("rel_types") or [])
    if n < 1000:
        say("WARN", "图谱数据偏少：%d 节点 / %d 关系" % (n, r),
            "完整数据应为 1320 节点 / 5693 关系")
        print("      " + C("修复：python scripts/08_import_extended_kg.py", "y"))
    else:
        say("OK", "图谱规模 %d 节点 / %d 关系 / %d 类实体 / %d 类关系"
            % (n, r, nl, rl))
    return True


def check_mysql():
    _sqlite = str(getattr(CONFIG, "DB_BACKEND", "mysql")).lower() == "sqlite"
    head("5/7  %s（登录 + 地块档案）" % ("SQLite 内置库（免安装）" if _sqlite else "MySQL"))
    if CONFIG is None:
        say("SKIP", "配置不可用，跳过")
        return False

    # ---- 免安装交付版：账号/地块/历史都在项目自带的单文件库里，不需要任何外部服务 ----
    if _sqlite:
        path = getattr(CONFIG, "SQLITE_PATH", "")
        if not path or not os.path.isfile(path):
            say("WARN", "SQLite 文件不存在", str(path))
            return False
        try:
            import sqlite3
            c = sqlite3.connect(path)
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            cnt = {}
            for t in ("user", "user_plot", "qa_history", "feedback"):
                if t in names:
                    cnt[t] = c.execute("SELECT COUNT(*) FROM `%s`" % t).fetchone()[0]
            c.close()
        except Exception as e:
            say("FAIL", "SQLite 打不开", str(e)[:90])
            return False
        say("OK", "SQLite 内置库", os.path.basename(path))
        print("      " + C("表 %d 张 ｜ 账号 %s / 地块 %s / 历史 %s / 反馈 %s"
                            % (len(names), cnt.get("user", "-"), cnt.get("user_plot", "-"),
                               cnt.get("qa_history", "-"), cnt.get("feedback", "-")), "d"))
        print("      " + C("（首次启动会自动建表；这个文件删了就是全新空库）", "d"))
        return True

    try:
        import pymysql
        cfg = dict(CONFIG.MYSQL_CONFIG)
        cfg["connect_timeout"] = 4
        conn = pymysql.connect(**cfg)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema=%s", (cfg["database"],))
        tables = cur.fetchone()[0]
        cur.execute("SHOW TABLES LIKE 'user_plot'")
        has_plot = cur.fetchone() is not None
        cur.close()
        conn.close()
    except Exception as e:
        say("WARN", "连不上 MySQL", str(e).replace("\n", " ")[:100])
        print("      " + C("影响：无法登录、无法保存地块档案；"
                            "物候期页会自动降级为「快速推算」（功能仍可用）", "d"))
        print("      " + C("修复：net start mysql80　（服务名可能是 mysql80 / MySQL / MySQL57，"
                            "用 sc query state= all | findstr /i mysql 查）", "y"))
        return False

    say("OK", "MySQL 已连接", "库 %s，共 %d 张表" % (CONFIG.MYSQL_CONFIG["database"], tables))
    if has_plot:
        say("OK", "地块档案表 user_plot 存在")
    else:
        say("WARN", "还没有 user_plot 表", "首次打开物候期页会自动创建，无需手工执行 SQL")
    return True


def check_assets():
    head("6/7  资源与缓存")
    if CONFIG is None:
        say("SKIP", "配置不可用，跳过")
        return False
    embed = os.path.join(CODE_DIR, "models", "bge-small-zh-v1.5")
    if os.path.isdir(embed):
        say("OK", "嵌入模型 bge-small-zh-v1.5")
    else:
        say("WARN", "缺少嵌入模型", "知识问答的向量检索会不可用：%s" % embed)

    vec = os.path.join(CODE_DIR, "output", "kg_vectors.npz")
    if os.path.isfile(vec):
        say("OK", "知识向量缓存", "%.1f MB，命中后约 2 秒加载"
            % (os.path.getsize(vec) / 1024 / 1024))
    else:
        say("WARN", "还没有知识向量缓存",
            "首次打开「知识问答」页需现算约 12 秒，之后会缓存")

    for f, label in [("static/echarts.min.js", "ECharts（图谱探索）"),
                     ("static/vis-network.min.js", "vis-network（经典图谱）")]:
        p = os.path.join(CODE_DIR, f)
        if os.path.isfile(p):
            say("OK", "%-26s %.1f MB" % (label, os.path.getsize(p) / 1024 / 1024))
        else:
            say("WARN", "缺少 %s" % label, "对应图谱页会走 CDN，断网时不可用")

    stages = os.path.join(CODE_DIR, "static", "stages")
    n = len([x for x in os.listdir(stages) if x.endswith(".svg")]) if os.path.isdir(stages) else 0
    if n >= 12:
        say("OK", "生育期示意图 %d 张" % n)
    else:
        say("WARN", "生育期示意图只有 %d 张" % n, "修复：python scripts/gen_stage_icons.py")

    data = os.path.join(CONFIG.DATA_DIR, "qa_pairs.csv")
    if os.path.isfile(data):
        try:
            with open(data, encoding="utf-8") as fh:
                rows = sum(1 for _ in fh) - 1
            say("OK", "问答评测集 qa_pairs.csv", "%d 条" % rows)
        except Exception:
            say("OK", "问答评测集 qa_pairs.csv")
    return True


def check_llm(cfg, enabled):
    head("7/7  大模型连通性")
    if not enabled:
        say("SKIP", "已跳过（--no-llm）")
        return True
    if not cfg or not cfg.get("ready"):
        say("SKIP", "未配置大模型，跳过")
        return True
    try:
        from core import llm_client
        t0 = time.time()
        text, err = llm_client.chat("你是一个连通性测试助手。",
                                    "请只回复四个字：连接成功",
                                    max_tokens=32, timeout=40, retries=1)
        dt = time.time() - t0
    except Exception as e:
        say("WARN", "调用异常", str(e)[:110])
        return False
    if text:
        say("OK", "模型已连通（%.1f 秒）" % dt, "返回：%s" % text[:24].replace("\n", " "))
        return True
    say("WARN", "模型调不通", str(err)[:110])
    print("      " + C("影响：处方会退回「图谱结构化输出」，功能不受影响；"
                        "页面上点「测试连接」可再试", "d"))
    return False


# ---------------------------------------------------------------- 端口 / 启动
def pick_port(prefer):
    for p in [prefer] + list(range(prefer + 1, prefer + 11)):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p, (p != prefer)
        except OSError:
            continue
        finally:
            s.close()
    return None, False


def launch(port, host, open_browser, debug):
    url = "http://127.0.0.1:%d" % port
    print("\n" + C("=" * 66, "b"))
    print(C("  穗知未来——农业知识图谱智能决策服务系统", "B"))
    print("  访问地址：%s" % C(url, "b"))
    if host != "127.0.0.1":
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print("  局域网地址：http://%s:%d" % (ip, port))
        except Exception:
            pass
    print("  停止服务：在本窗口按 Ctrl+C")
    print(C("=" * 66, "b") + "\n")

    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    from app import app as flask_app
    try:
        flask_app.run(host=host, port=port, debug=debug,
                      threaded=True, use_reloader=debug)
    except KeyboardInterrupt:
        pass
    print("\n服务已停止。")


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--check", action="store_true", help="只自检，不启动")
    ap.add_argument("--no-llm", action="store_true", help="跳过大模型连通性测试")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-start-neo4j", action="store_true",
                    help="Neo4j 没运行时不自动拉起")
    ap.add_argument("--lan", action="store_true", help="绑定 0.0.0.0（局域网可访问）")
    ap.add_argument("--debug", action="store_true", help="Flask 调试模式")
    ap.add_argument("--force", action="store_true", help="致命项未过也强行启动")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()

    enable_ansi()
    print(C("\n穗知未来 · 启动前自检", "B"))
    print(C("项目目录：%s" % CODE_DIR, "d"))

    check_python()
    deps_ok = check_deps()
    cfg = check_config()
    # --check 是"只看不动"，所以不自动拉服务；正常启动时才顺手把 Neo4j 带起来
    neo_ok = check_neo4j(auto_start=(not args.no_start_neo4j) and (not args.check))
    check_mysql()
    check_assets()
    check_llm(cfg, not args.no_llm)

    # ---------------- 结论 ----------------
    print("\n" + C("=" * 66, "b"))
    if not RESULTS:
        print(C("  自检通过，所有关键项正常。", "g"))
    else:
        fatals = [r for r in RESULTS if r[0] == FATAL]
        warns = [r for r in RESULTS if r[0] == WARN]
        if fatals:
            print(C("  发现 %d 项致命问题（会导致功能不可用）：" % len(fatals), "r"))
            for _, t, d in fatals:
                print("    · %s%s" % (t, ("　" + d) if d else ""))
        if warns:
            print(C("  发现 %d 项警告（不影响启动，功能可能缺失）：" % len(warns), "y"))
            for _, t, d in warns:
                print("    · %s%s" % (t, ("　" + d) if d else ""))
        if not fatals:
            print(C("  可以启动。", "g"))
    print(C("=" * 66, "b"))

    blocked = [r for r in RESULTS if r[0] == FATAL]
    if args.check:
        print("\n（--check 模式：只自检，不启动）")
        return 1 if blocked else 0
    if blocked and not args.force:
        print(C("\n致命项未通过，已停止启动。修好后再跑一次，", "r")
              + C("或加 --force 强行启动。", "d"))
        return 1

    port, shifted = pick_port(args.port)
    if port is None:
        print(C("\n端口 %d 起连续 10 个都被占用，请加 --port 换个端口段。" % args.port, "r"))
        return 2
    if shifted:
        print(C("\n端口 %d 被占用，自动改用 %d。" % (args.port, port), "y"))

    launch(port, "0.0.0.0" if args.lan else "127.0.0.1",
           not args.no_browser, args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
