# -*- coding: utf-8 -*-
"""
一键自检 selfcheck.py
=====================
逐个访问全部页面与接口，把状态码和后端报错打出来，用来判断"到底是哪一环断了"。

用法（在 code 目录下）：
    python selfcheck.py            # 全量（会真调大模型，慢，几分钟到十几分钟）
    python selfcheck.py --fast     # 快速模式：跳过所有会调用大模型的项（秒级）
    python selfcheck.py --fast -v  # 快速模式 + 打印每项耗时

输出全是 200 就说明全链路通了；出现 500 时冒号后面会跟着后端原始报错，
对照报错一般能直接定位到 Neo4j / MySQL / 模型配置哪一处没对上。

注意：接上大模型之后，/advisory 与 /qa 每一项都要等模型返回（每项 20~60 秒），
      所以日常改动自检请用 --fast，只有排查模型相关问题时才跑全量。
"""
import sys, os, json, traceback, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

FAST = "--fast" in sys.argv          # 跳过会真调大模型的项
VERBOSE = "-v" in sys.argv           # 打印每项耗时

c = appmod.app.test_client()

def show(title, resp):
    body = resp.get_data(as_text=True)
    print("[%s] %s  len=%d" % (resp.status_code, title, len(body)))
    if resp.status_code >= 400 or "Traceback" in body:
        print("   >>>", body[:500].replace("\n", " "))

def timed(title, fn):
    """跑一项并可选打印耗时（LLM 开启后，耗时是判断'模型有没有被调用'的重要信号）"""
    t = time.time()
    r = fn()
    show(title, r)
    if VERBOSE:
        print("        └ 耗时 %.1fs" % (time.time() - t))
    return r

def skipped(title, why="会调用大模型，--fast 模式跳过"):
    print("[skip] %s  （%s）" % (title, why))

# 1. 注册 + 登录
c.post("/register", data={"username": "smokeuser", "password": "123456"})
r = c.post("/login", data={"username": "smokeuser", "password": "123456"}, follow_redirects=False)
print("login ->", r.status_code, r.headers.get("Location"))

pages = ["/success", "/kg-display", "/qa-page", "/advisory-page",
         "/phenology-page", "/diagnose-page", "/econ-page", "/todo"]
for p in pages:
    show("PAGE " + p, c.get(p))

apis = ["/api/kg/stats", "/api/llm/status", "/api/vision/status",
        "/api/phenology/crops", "/api/soil/list", "/api/plot/list",
        # 注意：/api/kg/subgraph 与 /api/kg/profile 是【经典图谱 kg-display】在用，
        #      图谱探索页虽然已下线，这两个接口不能跟着删
        "/api/kg/search?q=小麦", "/api/kg/subgraph?name=小麦&depth=1",
        "/api/kg/suggest?ph=6.5&temp=18",
        "/api/econ/crops", "/api/econ/ref?crop=水稻", "/api/econ/plans",
        "/api/econ/plots", "/api/overview",
        "/api/grid/plots", "/api/grid/crops", "/api/today/todos"]
for p in apis:
    show("API  " + p, c.get(p))

show("POST /api/econ/compute", c.post("/api/econ/compute", json={
    "base_area": 10,
    "plans": [{"crop": "水稻"}, {"crop": "玉米"}, {"crop": "番茄"}]}))

# ---- 下面这四项会真调大模型（接上模型后每项 20~60 秒）----
if FAST:
    for t in ("POST /advisory(防治)", "POST /advisory(施肥)",
              "POST /advisory(农事)", "POST /advisory(规划)"):
        skipped(t)
else:
    timed("POST /advisory(防治)", lambda: c.post("/advisory", data={"question": "小麦条锈病怎么防治", "mode": "auto"}))
    timed("POST /advisory(施肥)", lambda: c.post("/advisory", data={"question": "番茄怎么施肥", "mode": "auto"}))
    timed("POST /advisory(农事)", lambda: c.post("/advisory", data={"question": "水稻的农事管理方案", "mode": "auto"}))
    timed("POST /advisory(规划)", lambda: c.post("/advisory", data={"question": "pH 6.5、年均温 18℃ 适合种什么", "mode": "auto"}))

show("POST /api/phenology", c.post("/api/phenology", json={"crop": "小麦", "sow_date": "2026-02-01"}))
show("POST /api/plot/save", c.post("/api/plot/save", json={"plot_name": "测试地块", "crop": "小麦", "sow_date": "2026-02-01"}))
show("GET  /api/plot/list", c.get("/api/plot/list"))

if FAST:
    for t in ("POST /qa(防治)", "POST /qa(土壤)", "POST /qa(产区)"):
        skipped(t, "首次要加载 bge 向量库且会调用大模型，--fast 模式跳过")
else:
    print("…… 下面测试知识问答（首次会加载 bge 向量库，约 20~30 秒）……")
    timed("POST /qa(防治)", lambda: c.post("/qa", data={"question": "玉米大斑病怎么防治"}))
    timed("POST /qa(土壤)", lambda: c.post("/qa", data={"question": "水稻的适宜土壤"}))
    timed("POST /qa(产区)", lambda: c.post("/qa", data={"question": "河南适合种什么"}))

print("DONE" + ("（--fast：已跳过会调用大模型的项，模型相关是否正常请单独验证）" if FAST else ""))

