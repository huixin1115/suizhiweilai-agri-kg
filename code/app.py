#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from flask import (Flask, request, render_template, redirect, url_for, session,
                   jsonify, send_file, Response, stream_with_context)
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
import sys
import os
import json
import queue
import threading

# 手动添加项目根目录到Python路径，确保能导入core模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 正确导入core模块
from core import db                     # noqa: E402  统一数据库入口（MySQL / SQLite）
from core.rag_search import AgricultureRAG
from core.neo4j_conn import Neo4jConnection
from core.auth import make_token, parse_token, bearer_from_header

app = Flask(__name__)
# 固定密钥。原来用 os.urandom(24)，每次重启都会变：
# 网页端已登录用户会被强制退出，小程序端已签发的 token 也会全部失效
app.secret_key = os.environ.get('APP_SECRET_KEY') or 'agri-kg-2026-flask-secret'


def current_username():
    """
    统一取当前登录用户：网页版读 session，小程序读 Authorization: Bearer <token>
    两种客户端共用同一批接口，无需为小程序单独写一套
    """
    u = session.get('username')
    if u:
        return u
    return parse_token(bearer_from_header(request.headers.get('Authorization')))


# ======================================================================
# 管理端 / 用户端（2026-09 新增）
#   同一个登录口，两种身份：
#     管理端（admin）—— 可添加用户端账号、指定它"能种植什么"、重置密码
#     用户端（user） —— 建档时只能选被授权的作物（没被授权过则不受限制）
#   角色存在 user.role，归属存在 user.admin_id，作物授权存在 user_crop_perm，
#   三处都由 core/user_admin.py 首次使用时自动建好。
# ======================================================================
from core.user_admin import UserAdminStore, ROLE_ADMIN, ROLE_USER   # noqa: E402

global_user_admin = None


def get_user_admin():
    global global_user_admin
    if global_user_admin is None:
        global_user_admin = UserAdminStore()
    return global_user_admin


# 启动即把表结构和老账号迁移做好：登录、注册都要读 role，
# 不能等到第一个请求再建列（不然会撞上\"列不存在\"）。失败只提示，不影响启动。
try:
    get_user_admin().ensure_schema()
except Exception as _e:                                  # pragma: no cover
    print("WARN: 用户/权限表初始化跳过：%s" % _e)

ROLE_LABEL = {ROLE_ADMIN: "管理端", ROLE_USER: "用户端"}


@app.before_request
def _sync_session_role():
    """老会话（升级前登录的）没有 role，这里补一次，避免全站判断落空"""
    if 'username' in session and 'role' not in session:
        try:
            session['role'] = get_user_admin().role_of(session['username'])
        except Exception:
            session['role'] = ROLE_USER


def current_role():
    """当前账号是管理端还是用户端；取不到就按用户端处理（权限只可能被显式授予）"""
    u = current_username()
    if not u:
        return ''
    if session.get('username') == u and session.get('role'):
        return session['role']
    try:
        return get_user_admin().role_of(u)
    except Exception:
        return ROLE_USER


def is_admin():
    return current_role() == ROLE_ADMIN


def _admin_guard():
    """管理端接口的统一鉴权：通过返回 (username, None)，否则返回 (None, 响应)"""
    u = current_username()
    if not u:
        return None, (jsonify({"ok": False, "error": "未登录"}), 403)
    if current_role() != ROLE_ADMIN:
        return None, (jsonify({"ok": False, "error": "需要管理端账号"}), 403)
    return u, None

# 数据库连接统一以 config.py 为准：
# 口令不写死在代码里 —— 走环境变量（MYSQL_PWD / NEO4J_PWD）或 config_local.py，
# 后者已在 .gitignore 中排除，不会进入公开仓库。
import config

MYSQL_CONFIG = config.MYSQL_CONFIG
NEO4J_URI = config.NEO4J_URI
NEO4J_USER = config.NEO4J_USER
NEO4J_PWD = config.NEO4J_PWD

# ========== 关键改动1：定义全局 RAG 实例（初始为 None） ==========
global_rag_instance = None


# ========== 登录注册路由（保持不变） ==========
@app.route('/')
def index():
    return redirect(url_for('login'))


def _pick_role(*values):
    """从表单/URL 里取角色，非法值一律按用户端处理"""
    for v in values:
        v = (v or '').strip()
        if v in (ROLE_ADMIN, ROLE_USER):
            return v
    return ROLE_USER


@app.route('/login', methods=['GET', 'POST'])
def login():
    # 角色用于切换登录入口：选错入口（例如拿用户端账号从管理端进）会明确提示
    role_in = _pick_role(request.form.get('role'), request.args.get('role'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            return render_template('login.html', role=role_in,
                                   error='用户名和密码不能为空！')

        conn = None
        try:
            conn = db.connect(MYSQL_CONFIG)
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute('SELECT * FROM user WHERE username = %s', (username,))
                user = cursor.fetchone()

            if not user:
                return render_template('login.html', role=role_in,
                                       error='用户名不存在！')
            real_role = (user.get('role') or ROLE_USER)
            real_role = ROLE_ADMIN if real_role == ROLE_ADMIN else ROLE_USER
            if real_role != role_in:
                # 顺带给一个\"一键换到正确入口\"的链接，别让用户自己找
                return render_template(
                    'login.html', role=role_in, switch_to=real_role,
                    error='这个账号是%s账号，请点上方的「%s」再登录。'
                          % (ROLE_LABEL[real_role], ROLE_LABEL[real_role]))
            if not check_password_hash(user['password'], password):
                return render_template('login.html', role=role_in, error='密码错误！')

            session['username'] = username
            session['role'] = real_role
            return redirect(url_for('success'))
        except Exception as e:
            return render_template('login.html', role=role_in, error=f'登录失败：{str(e)}')
        finally:
            if conn:
                conn.close()

    success_msg = request.args.get('success', '')
    return render_template('login.html', role=role_in, success=success_msg)


@app.route('/register', methods=['GET', 'POST'])
def register():
    role_in = _pick_role(request.form.get('role'), request.args.get('role'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        # 新增：简单的长度校验，提升体验
        if not username or not password:
            return render_template('register.html', role=role_in,
                                   error='用户名和密码不能为空！')
        if len(username) < 3 or len(password) < 6:
            return render_template('register.html', role=role_in,
                                   error='用户名至少3位，密码至少6位！')
        hashed_pwd = generate_password_hash(password, method='pbkdf2:sha256')
        conn = None  # 提前定义conn，避免finally判断报错
        try:
            conn = db.connect(MYSQL_CONFIG)
            with conn.cursor() as cursor:
                # 角色随注册页所选入口写入；自己注册的用户端不带归属（admin_id 为 NULL），
                # 之后可由管理端「添加用户端」收纳，或直接由管理端创建
                cursor.execute(
                    'INSERT INTO `user` (`username`, `password`, `role`) VALUES (%s, %s, %s)',
                    (username, hashed_pwd, role_in)
                )
            # 核心修复：提交事务，将数据真正写入数据库
            conn.commit()
            return redirect(url_for('login', success='注册成功，请登录！', role=role_in))
        except pymysql.IntegrityError:
            return render_template('register.html', role=role_in, error='用户名已存在！')
        except Exception as e:
            # 异常时回滚事务，避免脏数据
            if conn:
                conn.rollback()
            return render_template('register.html', role=role_in,
                                   error=f'注册失败：{str(e)}')
        finally:
            if conn:
                conn.close()
    return render_template('register.html', role=role_in)


# ========== 核心功能路由 ==========
@app.route('/success')
def success():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('success.html', username=session['username'])


@app.route('/qa-page')
def qa_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('qa.html', username=session['username'])


@app.route('/qa', methods=['POST'])
def qa():
    if 'username' not in session:
        return jsonify({"answer": "请先登录"}), 403

    user_question = request.form.get('question', '').strip()
    if not user_question:
        return jsonify({"answer": "请输入有效的问题"}), 400

    try:
        # ========== 关键改动2：复用全局 RAG 实例（仅初始化一次） ==========
        global global_rag_instance
        # 不存在、或连接已被关掉（"Driver closed"）时才重新初始化
        if (global_rag_instance is None
                or not _conn_alive(getattr(global_rag_instance, "neo4j_conn", None))):
            global_rag_instance = AgricultureRAG(NEO4J_URI, NEO4J_USER, NEO4J_PWD)

        # 执行搜索（不再关闭连接）
        # search_rich 按「知识图谱 → 本地知识库 → 大模型直出」三级逐级兜底取证据，
        # 命中哪一级就只用哪一级的证据交给大模型组织语言，并把溯源、追问建议一并带回来
        res = global_rag_instance.search_rich(user_question)

        # ========== 移除：不再在请求中关闭连接 ==========
        # rag.close()  # 这行代码是导致连接提前关闭的核心原因，直接删除

        # 返回结果（顺手记一条历史，失败不影响回答）
        hid = _record_history(session['username'], user_question, res,
                              plan_type='basic', plan_name='基础图谱问答')
        return jsonify({"answer": res.get("answer") or "",
                        "sources": res.get("sources") or [],
                        "entities": res.get("entities") or {},
                        "followups": res.get("followups") or [],
                        "llm_used": bool(res.get("llm_used")),
                        # 三级路由结果，前端据此显示"这条答案来自哪一级"
                        "route": res.get("route") or "",
                        "route_name": res.get("route_name") or "",
                        "history_id": hid})
    except Exception as e:
        print(f"RAG 查询错误: {e}")
        return jsonify({"answer": "查询失败，请重试"}), 500


# ======================================================================
# 流式问答（SSE）
#   原来 /qa 与 /advisory 都是"等大模型把整篇写完再一次性返回"，实测平均 22.7 秒
#   全程白屏。改成边生成边显示：检索一结束先把类型/实体/溯源推给前端，正文再逐段到。
# ======================================================================
def _sse(obj):
    """一帧 SSE 数据。⚠ ensure_ascii=False，中文才不会被转成 \\uXXXX"""
    return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)


def _sse_response(user, question, producer):
    """
    把一个"事件生成器"包成 SSE 响应（/api/qa/stream 与 /api/advice 共用）。

    producer(question) -> 产出事件 dict：meta / delta / done

    ⚠ 为什么用"后台线程 + 队列"，而不是直接在 gen() 里 yield 生成器：
      · 检索 + 等首字可能几十秒没有任何数据，中间不发心跳容易被代理/浏览器掐断
      · 写历史要读 session，**子线程没有请求上下文**，所以让子线程只管生成，
        把结果传回主线程再落库
    """
    def worker(q):
        try:
            for ev in producer(question):
                q.put(ev)
        except Exception as e:
            print("流式生成失败:", e)
            q.put({"event": "error", "message": "生成失败：%s" % e})
        finally:
            q.put(None)                      # 结束哨兵

    def gen():
        q = queue.Queue()
        threading.Thread(target=worker, args=(q,), daemon=True).start()
        yield ": 已连接\n\n"                 # 让浏览器立刻拿到响应头
        while True:
            try:
                ev = q.get(timeout=8)
            except queue.Empty:
                yield ": ping\n\n"           # 心跳，别让空闲连接被中间层掐掉
                continue
            if ev is None:
                break
            if ev.get("event") == "done":
                # 历史落库放在主线程（session 可用）；失败只打日志，不影响答案
                hid = _record_history(user, question, ev)
                if hid:
                    yield _sse({"event": "history", "history_id": hid})
                yield _sse(ev)
                break
            yield _sse(ev)

    # ⚠ mimetype 里不要再写 charset：Flask 会给 text/* 自动补上，写了会出现
    #   "text/event-stream; charset=utf-8; charset=utf-8" 这种重复
    return Response(stream_with_context(gen()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route('/api/qa/stream', methods=['POST'])
def api_qa_stream():
    """
    流式问答：question + mode(advisor|basic)。

    事件顺序：meta（检索结果：类型/实体/溯源/追问）→ delta（0~N 段正文）
             → history（落库 id，可能没有）→ done（完整结果）

    ⚠ 前端一律以 done 里的 answer 做最终渲染：中途断流时后端会把答案退回成
      图谱原文（llm_used=False），只有 done 是准的。
    ⚠ 老接口 /qa 与 /advisory 保持不动 —— 流式失败时前端回退用它们。
    """
    if 'username' not in session:
        return jsonify({"error": "请先登录"}), 403
    question = (request.form.get('question') or '').strip()
    mode = (request.form.get('mode') or 'advisor').strip()
    if not question:
        return jsonify({"error": "请输入问题"}), 400

    user = session['username']

    if mode == 'basic':
        def producer(q):
            global global_rag_instance
            if (global_rag_instance is None
                    or not _conn_alive(getattr(global_rag_instance, "neo4j_conn", None))):
                global_rag_instance = AgricultureRAG(NEO4J_URI, NEO4J_USER, NEO4J_PWD)
            return global_rag_instance.search_rich_stream(q)
    else:
        def producer(q):
            return get_advisor().advise_stream(q, mode="auto")

    return _sse_response(user, question, producer)


# ========== 修复知识图谱页面路由（彻底解决 name 'neo4j_conn' is not defined） ==========
@app.route('/kg-display')
def kg_display():
    if 'username' not in session:
        return redirect(url_for('login'))

    try:
        # 直接导入并使用，避免名称冲突
        from core.neo4j_conn import Neo4jConnection

        # 创建Neo4j连接
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PWD)
        # 获取所有作物列表
        crops = conn.get_all_crops()
        # 关闭连接（这个连接是独立的，不影响RAG的全局连接）
        conn.close()

        print(f"INFO: 知识图谱页面加载 {len(crops)} 个作物")
        return render_template('kg_display.html', all_crops=crops, username=session['username'])
    except Exception as e:
        print(f"获取作物列表失败: {e}")
        # 返回空列表，避免前端报错
        return render_template('kg_display.html', all_crops=[], username=session['username'])


@app.route('/kg-data/<crop_name>')
def kg_data(crop_name):
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403

    try:
        from core.neo4j_conn import Neo4jConnection
        conn = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PWD)
        # 核心修复：调用get_crop_kg()获取作物关系数据
        data = conn.get_crop_kg(crop_name)
        conn.close()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/logout')
def logout():
    """
    登出只清会话 —— **不再关闭 Neo4j 连接**。

    原来这里会 close 掉 RAG / 顾问的全局连接，但它们是**进程级共享的单例**：
    任何一个账号登出，就把所有人的连接都拆了；更麻烦的是物候引擎还抱着顾问的连接
    （`PhenologyEngine(get_advisor().conn)`），于是登出一次之后
    作物下拉、物候推算、管理端的作物列表全部报 "Driver closed"，只能重启服务。
    改成：连接常驻复用（进程退出时由系统回收），单例自身也会自愈，见 _conn_alive()。
    """
    session.pop('username', None)
    session.pop('role', None)
    return redirect(url_for('login'))


# ======================================================================
# 知识图谱扩展能力（2026-09 新增）
#   数据：1320 节点 / 5693 关系，13 类实体、22 类关系
#   能力：四类处方生成（防治建议 / 水肥处方 / 农事方案 / 生产规划）
#         + 图谱检索、子图下钻、统计大屏
# ======================================================================
try:
    import config
    NEO4J_URI = config.NEO4J_URI
    NEO4J_USER = config.NEO4J_USER
    NEO4J_PWD = config.NEO4J_PWD
except Exception as _e:          # 配置缺失时沿用文件内原有配置
    print("WARN: config.py 未加载，使用内置连接配置:", _e)

from core.agri_advisor import AgriAdvisor   # noqa: E402

global_advisor = None


def _conn_alive(conn):
    """
    Neo4j 连接是否还能用。
    被 close() 过的 driver 再发查询会抛 "Driver closed"；这里探一下，
    好让下面的单例自愈 —— 否则一次误关会让图谱相关功能整站瘫到重启为止。
    """
    if conn is None:
        return False
    try:
        conn._run("RETURN 1 AS ok")
        return True
    except Exception as e:
        print("WARN: Neo4j 连接不可用（%s），将重建" % str(e)[:80])
        return False


def get_advisor():
    """懒加载顾问实例：模型/连接只初始化一次（连接失效时自动重建）"""
    global global_advisor
    if global_advisor is None or not _conn_alive(getattr(global_advisor, "conn", None)):
        global_advisor = AgriAdvisor(NEO4J_URI, NEO4J_USER, NEO4J_PWD)
    return global_advisor


# ======================================================================
# 四类处方：各自独立成页（不再用「智能决策顾问」一个入口猜意图）
#   病虫害防治建议 / 水肥调控处方 / 农事管理方案 / 生产规划报告
# ======================================================================
ADVICE_MODES = {
    'pest': {
        'title': '病虫害防治建议',
        'subtitle': '绿色防控顺序：农业防治 → 生物防治 → 物理防治 → 化学农药，含安全间隔期',
        'placeholder': '选择作物（可再指定病虫害），点「生成病虫害防治建议」',
    },
    'fertilizer': {
        'title': '水肥调控处方',
        'subtitle': '需肥结构 → 推荐肥料 → 基肥 / 追肥 / 叶面施肥分期用量',
        'placeholder': '选择作物，点「生成水肥调控处方」',
    },
    'farm': {
        'title': '农事管理方案',
        'subtitle': '全生育期时间轴 + 各阶段关键农事操作',
        'placeholder': '选择作物，点「生成农事管理方案」',
    },
    'planning': {
        'title': '生产规划报告',
        'subtitle': '按土壤、气候条件筛选适种作物，附茬口与风险提示',
        'placeholder': '填写若干条件（pH / 气温 / 降水 / 产区 / 土壤 / 季节），点「生成生产规划报告」',
    },
}


@app.route('/advice/<mode>')
def advice_page(mode):
    """四类处方各自的页面"""
    if not current_username():
        return redirect(url_for('login'))
    cfg = ADVICE_MODES.get(mode)
    if not cfg:
        return redirect(url_for('success'))
    return render_template('advice.html', username=current_username(), mode=mode, **cfg)


@app.route('/api/advice/parse', methods=['POST'])
def api_advice_parse():
    """
    处方页搜索框：把一句自然语言解析成「作物 / 病虫害 / 意图」。

    **只解析，不生成** —— 生成仍走原来的 /api/advice，这样搜索出来的结果
    与手工选下拉生成的完全一致（同一套事实聚合、同一套提示词、同样会落历史）。
    解析复用顾问引擎的 extract() / detect_intent()，不另写一套规则，
    免得"搜索"和"知识问答"对同一句话给出不同判断。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "请先登录"}), 403
    q = (request.form.get('q') or '').strip()
    if not q:
        return jsonify({"ok": False, "message": "请输入要查的作物或病虫害"}), 400
    try:
        adv = get_advisor()
        ent = adv.extract(q)
        intent = adv.detect_intent(q)
        # 图谱里"病虫害"是一类，只是名字带不带"病"字；疾病优先更具体
        pest = ent.get('disease') or ent.get('pest') or ent.get('weed')
        return jsonify({"ok": True, "q": q, "crop": ent.get('crop'), "pest": pest,
                        "stage": ent.get('stage'), "season": ent.get('season'),
                        "region": ent.get('region'), "soil": ent.get('soil'),
                        "intent": intent})
    except Exception as e:
        print("WARN: 处方搜索解析失败：%s" % e)
        return jsonify({"ok": False, "message": "识别失败，请从下面的下拉里直接选"})


@app.route('/api/advice', methods=['POST'])
def api_advice():
    """
    四类处方的统一入口：前端只传结构化参数，服务端负责组装问题并调用顾问引擎。
    参数：mode（pest|fertilizer|farm|planning）+ 各模式自己的字段
    """
    if not current_username():
        return jsonify({"error": "请先登录"}), 403
    d = request.get_json(silent=True) or request.form
    mode = (d.get('mode') or '').strip()
    if mode not in ADVICE_MODES:
        return jsonify({"error": "未知的处方类型"}), 400

    crop = (d.get('crop') or '').strip()
    question = ''

    if mode == 'pest':
        pest = (d.get('pest') or '').strip()
        if not crop:
            return jsonify({"error": "请选择作物"}), 400
        if pest:
            # 病名常自带作物（如「小麦条锈病」），避免拼成"小麦小麦条锈病"
            head = pest if (pest.startswith(crop) or crop in pest) else (crop + pest)
            question = '%s怎么防治' % head
        else:
            question = '%s有哪些病虫害，怎么防治' % crop
    elif mode == 'fertilizer':
        if not crop:
            return jsonify({"error": "请选择作物"}), 400
        question = '%s怎么施肥' % crop
    elif mode == 'farm':
        if not crop:
            return jsonify({"error": "请选择作物"}), 400
        question = '%s的农事管理方案' % crop
    else:  # planning
        bits = []
        ph = str(d.get('ph') or '').strip()
        temp = str(d.get('temp') or '').strip()
        rain = str(d.get('rain') or '').strip()
        region = str(d.get('region') or '').strip()
        soil = str(d.get('soil') or '').strip()
        season = str(d.get('season') or '').strip()
        if ph:
            bits.append('土壤 pH %s' % ph)
        if temp:
            bits.append('年均温 %s℃' % temp)
        if rain:
            bits.append('年降水 %smm' % rain)
        if region:
            bits.append(region)
        if soil:
            bits.append(soil)
        if season:
            bits.append('%s季' % season)
        if not bits:
            return jsonify({"error": "请至少填写一个条件"}), 400
        question = '%s 适合种什么' % '、'.join(bits)

    # 前端带 stream=1 走 SSE（四类处方页同样是"边生成边显示"），不带就还是老的一次性返回
    if str(d.get('stream') or request.args.get('stream') or '').strip() == '1':
        return _sse_response(current_username(), question,
                             lambda q: get_advisor().advise_stream(q, mode=mode))

    try:
        res = get_advisor().advise(question, mode=mode)
    except Exception as e:
        print("处方生成失败:", e)
        return jsonify({"error": "生成失败：%s" % e}), 500
    res['question'] = question
    # 处方也进历史（四类处方页走的就是这个入口）
    res['history_id'] = _record_history(current_username(), question, res)
    return jsonify(res)


@app.route('/api/kg/crops')
def api_kg_crops():
    """全部作物（含类别），供四类处方的下拉选择"""
    if not current_username():
        return jsonify({"error": "未登录"}), 403
    try:
        rows = get_advisor().conn._run("""
            MATCH (c:作物) RETURN c.name AS name, c.`类别` AS category,
                   c.`亚类` AS sub ORDER BY (c.`类别` IS NULL), category, name
        """)
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/pests')
def api_kg_pests():
    """某作物关联的病虫害与杂草（供病虫害防治建议页联动）"""
    if not current_username():
        return jsonify({"error": "未登录"}), 403
    crop = (request.args.get('crop') or '').strip()
    if not crop:
        return jsonify([])
    try:
        rows = get_advisor().conn._run("""
            MATCH (c {name:$crop})-[:REL]->(p)
            WHERE p:病虫害 OR p:杂草
            RETURN DISTINCT p.name AS name, labels(p)[0] AS label,
                   p.`危害等级` AS level, p.`类型` AS kind
            ORDER BY (p.`危害等级` IS NULL), p.`危害等级`, name
        """, crop=crop)
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/regions')
def api_kg_regions():
    """产区列表（生产规划报告的条件输入用）"""
    if not current_username():
        return jsonify({"error": "未登录"}), 403
    try:
        return jsonify(get_advisor().conn._run(
            "MATCH (r:地域) RETURN r.name AS name ORDER BY name"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/llm/status')
def api_llm_status():
    """
    当前大模型接入状态（不返回密钥）
    用于前端展示与现场排障：确认接口地址 / 模型名 / Key 是否就绪
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        return jsonify(get_advisor().llm_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/llm/test', methods=['POST'])
def api_llm_test():
    """连通性自检：发一条最小请求，确认接口地址 / Key / 模型名三者都对得上"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        advisor = get_advisor()
        status = advisor.llm_status()
        if not status["ready"]:
            return jsonify({"ok": False, "status": status,
                            "message": "尚未配置接口地址 / API Key / 模型名称，当前使用图谱结构化输出"})
        out = advisor._llm_api("你是一个连通性测试助手。",
                               "请只回复四个字：连接成功", max_tokens=32)
        if out:
            return jsonify({"ok": True, "status": status, "reply": out,
                            "message": "连接成功"})
        return jsonify({"ok": False, "status": status,
                        "message": "调用失败，请查看后端控制台日志（HTTP 状态码与原始返回已打印）"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route('/advisory', methods=['POST'])
def advisory():
    """
    处方生成接口
    表单参数：question（自然语言）、mode（auto/pest/fertilizer/farm/planning）
    返回：answer / plan_name / sources / llm_used
    """
    if 'username' not in session:
        return jsonify({"answer": "请先登录"}), 403
    question = (request.form.get('question') or '').strip()
    mode = (request.form.get('mode') or 'auto').strip()
    if not question:
        return jsonify({"answer": "请输入问题"}), 400
    try:
        result = get_advisor().advise(question, mode=mode)
        # 落历史 + 行为流水 + 热门计数（失败不影响返回，见 _record_history）
        result['history_id'] = _record_history(session['username'], question, result)
        return jsonify(result)
    except Exception as e:
        print("处方生成失败:", e)
        return jsonify({"answer": "生成失败：%s" % e, "sources": []}), 500


def _history_scope():
    """
    历史记录的可见范围（归属即边界）：

      · 用户端 —— 只有自己
      · 管理端 —— 自己 + **名下**用户端；别的管理端的用户一个都看不到

    返回 (范围内的用户名列表, 是否管理端视角)
    """
    me = session['username']
    if not is_admin():
        return [me], False
    scope = set([me])
    try:
        for u in get_user_admin().managed_users(me):
            name = (u or {}).get('username')
            if name:
                scope.add(name)
    except Exception as e:
        print("WARN: 读取名下用户端失败（历史记录按只读自己处理）：%s" % e)
    return sorted(scope), True


@app.route('/history-page')
def history_page():
    """
    历史记录页：我提过的问题 / 生成过的处方 + 我对答案的反馈。
    管理端登录时换成「名下用户端」的视角 —— 每张卡片上标明这条是谁问的。
    """
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('history.html', username=session['username'],
                           is_admin=is_admin())


def _history_filters():
    """
    列表 / 统计 / 导出三处共用的筛选参数：类型 / 关键词 / 起止日期。

    ⚠ 两个保护：
      · 关键词截断到 60 字 —— 不然有人传个几 MB 的字符串进来做 LIKE 匹配，数据库先扛不住
      · 起止日期写反了就**自动换过来**，别返回一个空列表让人以为"记录没了"
    """
    kw = (request.args.get('q') or '').strip()[:60]
    df = (request.args.get('from') or '').strip()
    dt = (request.args.get('to') or '').strip()
    if not valid_day(df):
        df = ""
    if not valid_day(dt):
        dt = ""
    if df and dt and df > dt:
        df, dt = dt, df
    return (request.args.get('type') or 'all').strip(), kw, df, dt


def _history_photo_path(url):
    """
    把 `qa_history.image` 里存的缩略图 URL（形如 `/static/uploads/diag/2026-09-28/xx.jpg`）
    翻译成**本地文件绝对路径**，导出 Word / PDF 时才能把照片嵌进文档。

    ⚠ 三道闸，缺一不可：
      · 只认站内 `/static/` 开头的相对路径 —— 外链 `http(s)://` 不当本地文件读
      · `realpath` 之后必须还落在 static 目录里 —— 挡住 `../../` 目录穿越
      · 文件真不存在（记录删了照片也跟着删了）返回空串，由导出侧降级成一行说明，
        绝不因为一张图让整份导出失败
    """
    u = (url or "").strip()
    if not u or u.lower().startswith(("http://", "https://", "data:")):
        return ""
    rel = u.split("?", 1)[0].split("#", 1)[0].replace("\\", "/").lstrip("/")
    if not rel.startswith("static/"):
        return ""
    rel = rel[len("static/"):]
    try:
        base = os.path.abspath(app.static_folder or "")
        p = os.path.abspath(os.path.join(base, rel))
    except Exception:
        return ""
    if not (p == base or p.startswith(base + os.sep) or p.startswith(base + "/")):
        return ""
    return p if os.path.isfile(p) else ""


@app.route('/api/history')
def api_history_list():
    """
    历史记录列表（分页）：?type= 类型，?q= 关键词，?from=&to= 日期区间（YYYY-MM-DD，含当天），
    ?page= 页码（从 1 开始），?size= 每页条数（5~100，默认 20）
    管理端可再带 ?user= 只看某一个用户端（不在自己名下的值静默忽略，不会越权）

    返回 total / page / pages 三个字段给前端画分页器。
    ⚠ 页码越界会自动夹回最后一页：筛完只剩 1 页却还停在第 3 页时，
      否则会一直显示"没有记录"，看着像功能坏了。
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    user = session['username']
    ptype, kw, df, dt = _history_filters()
    try:
        page = max(1, int(request.args.get('page') or 1))
    except ValueError:
        page = 1
    try:
        size = min(100, max(5, int(request.args.get('size') or 20)))
    except ValueError:
        size = 20
    try:
        all_scope, admin_view = _history_scope()
        want = (request.args.get('user') or '').strip()
        scope = [want] if (admin_view and want in all_scope) else all_scope
        st = get_history_store()
        if admin_view:
            total = st.count_multi(scope, plan_type=ptype, keyword=kw,
                                   date_from=df, date_to=dt)
            stats = st.stats_multi(scope, plan_type=ptype, keyword=kw,
                                   date_from=df, date_to=dt)
        else:
            total = st.count(user, plan_type=ptype, keyword=kw, date_from=df, date_to=dt)
            stats = st.stats(user, plan_type=ptype, keyword=kw, date_from=df, date_to=dt)
        pages = max(1, (total + size - 1) // size)
        if page > pages:
            page = pages
        offset = (page - 1) * size
        if admin_view:
            items = st.list_multi(scope, limit=size, offset=offset, plan_type=ptype,
                                  keyword=kw, date_from=df, date_to=dt)
        else:
            items = st.list(user, limit=size, offset=offset, plan_type=ptype,
                            keyword=kw, date_from=df, date_to=dt)
        # 不带筛选的总条数：决定"清空历史"按钮要不要出现
        # （清空清的是全部，不能因为当前筛选没结果就把按钮藏了）
        filtered = (ptype and ptype != 'all') or kw or df or dt
        total_all = total if not filtered else (
            st.count_multi(scope) if admin_view else st.count(user))
        return jsonify({"available": st.available, "error": st.error,
                        "items": items, "count": len(items),
                        "total": total, "total_all": total_all,
                        "page": page, "pages": pages, "size": size,
                        "stats": stats,
                        "is_admin": admin_view,
                        # 下拉里列出名下全部用户端（含还没问过问题的那些）
                        "scope_users": all_scope if admin_view else [],
                        "current_user": user,
                        "hot": st.hot(8)})
    except Exception as e:
        return jsonify({"error": str(e), "items": [], "available": False}), 500


@app.route('/api/history/<int:hid>')
def api_history_detail(hid):
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    item = get_history_store().get(session['username'], hid)
    if not item:
        return jsonify({"error": "记录不存在"}), 404
    return jsonify(item)


def _history_owned(st, username, hid):
    """
    这条历史记录是不是当前用户自己的？返回 (状态, 记录)。

    状态取 "ok" / "missing" / "down"；记录在 ok 时是那条 dict（删记录时要用它拿缩略图 URL）。

    为什么要分 missing 与 down：`HistoryStore.get()` 在**数据库连不上**时也返回 None，
    一律当 404 处理就会把"库挂了"说成"记录不存在"，提示词误导人。
    判断顺序也讲究：先 get() 一次（它会顺带做懒探测、把 available 置为 False），再看 available。
    """
    item = st.get(username, hid)
    if item is not None:
        return "ok", item
    return ("down" if not st.available else "missing"), None


@app.route('/api/history/<int:hid>', methods=['DELETE'])
def api_history_delete(hid):
    """
    删除自己的一条记录（拍照识病的缩略图文件也一并删掉）。

    ⚠ 先查归属再删：`HistoryStore.delete()` 用 `lastrowid is not None` 判成功，
    而一条都没删到时 lastrowid 是 **0**（`0 is not None` 为真），
    所以「删别人的记录」原本会返回 200 ok —— 数据是安全的（WHERE 里带着 username），
    但响应在骗人。这里改成与 `/api/econ/delete` 一致的写法，越权就是一个干净的 404。
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    st = get_history_store()
    own, item = _history_owned(st, session['username'], hid)
    if own == "down":
        return jsonify({"ok": False, "message": "数据库不可用，暂时删不掉"}), 503
    if own != "ok":
        return jsonify({"ok": False, "message": "记录不存在或不属于你"}), 404
    if not st.delete(session['username'], hid):
        return jsonify({"ok": False, "message": "删除失败（数据库不可用）"}), 503
    remove_thumb(item.get("image"))      # 文件删不掉也不该让接口失败
    return jsonify({"ok": True})


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    """清空本人全部记录（连缩略图文件一起清理）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    st = get_history_store()
    try:
        thumbs = st.images(session['username'])      # 先取一份，删完行就查不到了
    except Exception as e:
        print("WARN: 读取待清理的缩略图失败（跳过文件清理）：%s" % e)
        thumbs = []
    if not st.clear(session['username']):
        return jsonify({"ok": False, "message": "清空失败（数据库不可用）"}), 503
    removed = sum(1 for u in thumbs if remove_thumb(u))
    return jsonify({"ok": True, "images_removed": removed})


@app.route('/api/history/<int:hid>/feedback', methods=['POST'])
def api_history_feedback(hid):
    """
    给某条记录提交反馈
    表单/JSON：rating = helpful | unhelpful，comment = 可选纠错说明
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    data = request.get_json(silent=True) or request.form
    rating = (data.get('rating') or '').strip()
    comment = (data.get('comment') or '').strip()
    if rating not in ('helpful', 'unhelpful'):
        return jsonify({"ok": False, "message": "rating 只能是 helpful 或 unhelpful"}), 400
    # 只允许对"自己那条记录"反馈；数据库连不上时别伪装成"记录不存在"
    st = get_history_store()
    own, _item = _history_owned(st, session['username'], hid)
    if own == "down":
        return jsonify({"ok": False, "message": "数据库不可用，反馈暂时存不下来"}), 503
    if own != "ok":
        return jsonify({"ok": False, "message": "记录不存在"}), 404
    ok = st.save_feedback(session['username'], hid, rating, comment)
    if not ok:
        return jsonify({"ok": False, "message": "反馈保存失败（数据库不可用）"}), 503
    return jsonify({"ok": True, "rating": rating})


@app.route('/admin-page')
def admin_page():
    """管理端控制台：添加用户端、指定可种植作物、重置密码、解除管理"""
    if 'username' not in session:
        return redirect(url_for('login'))
    if not is_admin():
        return render_template('admin_denied.html', username=session['username'])
    return render_template('admin.html', username=session['username'])


@app.route('/api/admin/overview')
def api_admin_overview():
    u, err = _admin_guard()
    if err:
        return err
    ua = get_user_admin()
    if not ua.available:
        return jsonify({"ok": False, "available": False, "error": ua.error,
                        "users": [], "stats": {}})
    return jsonify({"ok": True, "available": True,
                    "stats": ua.stats(u), "users": ua.managed_users(u),
                    "assignable": ua.assignable_users(u)})


@app.route('/api/admin/user/plots')
def api_admin_user_plots():
    """
    管理端查看某个用户端的**地块档案**（只读）：
    管理员在「用户端管理」里点「查看地块」时调这里。

    · 带 ?user=<用户名>；**只能查自己名下的账号**（归属即边界，越权返 403）
    · 返回结构与 `/api/plot/list` 一致（每个地块带物候摘要 summary），
      前端因此可以复用同一套"地块卡片"的画法
    """
    u, err = _admin_guard()
    if err:
        return err
    target = (request.args.get('user') or '').strip()
    if not target:
        return jsonify({"ok": False, "plots": [], "message": "缺少 user 参数"}), 400
    ua = get_user_admin()
    mine = [x.get('username') for x in ua.managed_users(u)]
    if target not in mine:
        return jsonify({"ok": False, "plots": [],
                        "message": "「%s」不在你名下，无权查看" % target}), 403
    store = get_plot_store()
    if not store.available:
        return jsonify({"ok": False, "available": False, "plots": [],
                        "message": "数据库不可用，暂时读不到地块档案：%s"
                                   % (store.error or '')})
    try:
        plots = store.list_plots(target)
        eng = get_phenology()
        for p in plots:
            try:
                p["summary"] = eng.summary_line(p["crop"], p["sow_date"],
                                                soil=p.get("soil") or None,
                                                ph=p.get("ph"))
            except Exception as e:
                p["summary"] = {"status": "error", "text": str(e), "risk_count": 0}
        return jsonify({"ok": True, "available": True, "username": target,
                        "count": len(plots), "plots": plots})
    except Exception as e:
        return jsonify({"ok": False, "plots": [], "message": str(e)}), 500


@app.route('/api/admin/soils')
def api_admin_soils():
    """供管理端勾选的土质全集（来自图谱「土壤」节点）"""
    u, err = _admin_guard()
    if err:
        return err
    try:
        rows = get_phenology().list_soils()
        names = [r.get("name") for r in rows if r.get("name")]
        if not names:
            return jsonify({"ok": False, "soils": [],
                            "error": "图谱里没读到土壤节点"})
        return jsonify({"ok": True, "soils": names})
    except Exception as e:
        return jsonify({"ok": False, "soils": [],
                        "error": "读取土质列表失败：%s" % str(e)[:120]})


@app.route('/api/admin/user/perms', methods=['POST'])
def api_admin_user_perms():
    """
    一次改两类授权：crops / soils。
    **只传其中一类，另一类保持不动**（前端只改土质时不会把作物权限抹掉）。
    """
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    kwargs = {}
    if 'crops' in d:
        kwargs['crops'] = d.get('crops') or []
    if 'soils' in d:
        kwargs['soils'] = d.get('soils') or []
    return _admin_user_op(get_user_admin().set_perms, u,
                          (d.get('username') or '').strip(), **kwargs)


@app.route('/api/admin/crops')
def api_admin_crops():
    """
    供管理端勾选的作物全集 —— 用「有生育期数据」的那批，
    因为作物权限最终作用在物候期建档上，给了别的作物反而选不出来

    失败时返回 ok=false + 中文原因（而不是一个空列表），
    页面才能告诉管理员"到底是图谱没连上还是真没数据"。
    """
    u, err = _admin_guard()
    if err:
        return err
    try:
        rows = get_phenology().list_crops()
        names = [r.get("name") for r in rows if r.get("name")]
        if not names:
            return jsonify({"ok": False, "crops": [],
                            "error": "图谱里没读到带生育期数据的作物"})
        return jsonify({"ok": True, "crops": names})
    except Exception as e:
        return jsonify({"ok": False, "crops": [],
                        "error": "读取作物列表失败：%s" % str(e)[:120]})


@app.route('/api/my/permission')
def api_my_permission():
    """当前账号的角色与授权（物候期页用它显示提示条）"""
    u = current_username()
    if not u:
        return jsonify({"error": "未登录"}), 403
    ua = get_user_admin()
    role = current_role()
    if role == ROLE_ADMIN:
        crops, soils = [], []
    else:
        crops, soils = ua.crops_of(u), ua.soils_of(u)
    try:
        crop_total = len([r for r in get_phenology().list_crops() if r.get("name")])
        soil_total = len([r for r in get_phenology().list_soils() if r.get("name")])
    except Exception:
        crop_total = soil_total = 0
    return jsonify({
        "role": role, "role_label": ROLE_LABEL.get(role, "用户端"),
        "username": u,
        "limited": bool(crops or soils),
        "crops": crops, "soils": soils,
        "crop_limited": bool(crops), "soil_limited": bool(soils),
        "crop_total": crop_total, "soil_total": soil_total,
        "admin": ua.admin_name_of(u),
    })


def _admin_user_op(fn, *args, **kwargs):
    """把 (ok, message) 形式的 store 操作统一成接口响应"""
    try:
        ok, msg = fn(*args, **kwargs)
    except Exception as e:
        ok, msg = False, "操作异常：%s" % e
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route('/api/admin/user/create', methods=['POST'])
def api_admin_user_create():
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().create_user, u,
                          (d.get('username') or '').strip(),
                          (d.get('password') or '').strip(),
                          d.get('crops') or [], d.get('soils') or [])


@app.route('/api/admin/user/unassigned')
def api_admin_user_unassigned():
    """还没归属任何管理端的用户端账号（供「纳入管理」下拉）"""
    u, err = _admin_guard()
    if err:
        return err
    try:
        return jsonify({"ok": True, "users": get_user_admin().unassigned_users()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "users": []}), 500


@app.route('/api/admin/user/attach', methods=['POST'])
def api_admin_user_attach():
    """把已注册、还未归属的账号纳入本管理端"""
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().attach, u,
                          (d.get('username') or '').strip())


@app.route('/api/admin/user/crops', methods=['POST'])
def api_admin_user_crops():
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().set_crops, u,
                          (d.get('username') or '').strip(), d.get('crops') or [])


@app.route('/api/admin/user/password', methods=['POST'])
def api_admin_user_password():
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().reset_password, u,
                          (d.get('username') or '').strip(),
                          (d.get('password') or '').strip())


@app.route('/api/admin/user/detach', methods=['POST'])
def api_admin_user_detach():
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().detach, u,
                          (d.get('username') or '').strip())


@app.route('/api/admin/user/delete', methods=['POST'])
def api_admin_user_delete():
    u, err = _admin_guard()
    if err:
        return err
    d = request.get_json(silent=True) or request.form
    return _admin_user_op(get_user_admin().delete_user, u,
                          (d.get('username') or '').strip())


@app.route('/api/kg/stats')
def api_kg_stats():
    """图谱规模统计"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        return jsonify(get_advisor().conn.graph_stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/search')
def api_kg_search():
    """实体检索联想"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    q = (request.args.get('q') or '').strip()
    try:
        return jsonify(get_advisor().conn.search_entities(q, limit=20))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/subgraph')
def api_kg_subgraph():
    """以实体为中心取子图（供图谱下钻）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    name = (request.args.get('name') or '').strip()
    depth = int(request.args.get('depth') or 1)
    try:
        return jsonify(get_advisor().conn.get_subgraph(name, depth=depth, limit=200))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/profile/<path:name>')
def api_kg_profile(name):
    """实体画像：属性 + 关系分组"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        return jsonify(get_advisor().conn.get_entity_profile(name))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/kg/dataset')
def api_kg_dataset():
    """整图数据（供前端一次性加载）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        with open(config.KG_JSON, encoding='utf-8') as f:
            return app.response_class(f.read(), mimetype='application/json')
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/todo-page')
def todo_page():
    """今日待办：把各块地的当日提醒汇总排序（原工作台首屏那一块，现独立成页）"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('todo.html', username=session['username'])


# 说明：「图谱探索」页已下线（入口摘除，templates/kg_explorer.html 与
# 它用到的 /api/kg/search、/api/kg/subgraph、/api/kg/profile 均保留，
# 需要恢复时把下面这段路由注释放开、并在 _nav.html 里加回入口即可）
# @app.route('/kg-explorer')
# def kg_explorer():
#     """交互式图谱探索页：右侧检索 → 左侧图谱自动定位并展开"""
#     if 'username' not in session:
#         return redirect(url_for('login'))
#     return render_template('kg_explorer.html', username=session['username'])


@app.route('/api/kg/suggest')
def api_kg_suggest():
    """条件筛选：pH / 气温 / 降水 / 产区 / 土壤 / 季节 → 适种作物"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403

    def num(key):
        v = request.args.get(key)
        try:
            return float(v) if v not in (None, '') else None
        except ValueError:
            return None

    try:
        rows = get_advisor().conn.suggest_crops(
            ph=num('ph'), temp=num('temp'), rain=num('rain'),
            region=(request.args.get('region') or '').strip() or None,
            soil=(request.args.get('soil') or '').strip() or None,
            season=(request.args.get('season') or '').strip() or None,
            limit=int(request.args.get('limit') or 10))
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
# 物候期自动提醒（2026-09 新增）
#   思路：作物 + 播种日期 → 推算今天处于哪个生育期 → 输出今日该干什么
#   数据：图谱里每个作物挂着 5~7 个生育期节点（带起止日、农事操作）
# ======================================================================
from core.phenology import PhenologyEngine       # noqa: E402
from core.plot_store import PlotStore, parse_ph   # noqa: E402

global_phenology = None
global_plot_store = None


def get_phenology():
    """物候引擎：同样在连接失效时自动重建（它借用的是顾问的连接）"""
    global global_phenology
    if global_phenology is None or not _conn_alive(getattr(global_phenology, "conn", None)):
        global_phenology = PhenologyEngine(get_advisor().conn)
    return global_phenology


def get_plot_store():
    global global_plot_store
    if global_plot_store is None:
        global_plot_store = PlotStore()
    return global_plot_store


# ======================================================================
# 地块待办完成记录（2026-09-24 新增）
#   "今天这块地的事干了没有" —— 勾选按天记（plot_task_log），
#   MySQL 不可用时静默降级：读写都当作"没有记录"，不影响待办页浏览。
# ======================================================================
from core.task_store import TaskStore, PLOT_TASK, today_str, valid_date   # noqa: E402
from core.feedback_store import FeedbackStore    # noqa: E402

global_task_store = None
global_feedback_store = None


def get_task_store():
    global global_task_store
    if global_task_store is None:
        global_task_store = TaskStore()
    return global_task_store


# ======================================================================
# 农作物经济效益测算（2026-09-26 新增）
#   "种这个能卖多少钱、要投多少钱、最后剩多少"：
#   输入 亩产 / 收购价 / 七项亩成本，算亩均与总额的收入·成本·净收益、
#   投入产出比与保本价（保本收购价、保本亩产），几个作物摆一起比。
# ======================================================================
from core.econ_store import (EconStore,          # noqa: E402
                             compute as econ_compute,
                             summarize as econ_summarize,
                             COST_FIELDS as ECON_COST_FIELDS)
from core import econ_defaults                   # noqa: E402

global_econ_store = None


def get_econ_store():
    global global_econ_store
    if global_econ_store is None:
        global_econ_store = EconStore()
    return global_econ_store


# ======================================================================
# 历史记录与用户反馈（2026-09 新增）
#   每次提问/生成处方都落一条历史（qa_history），并留下行为流水（user_behavior），
#   识别到的作物/病虫害计入热度（hot_content）；
#   用户可以对任何一条记录点"有帮助/没帮助"并写纠错说明（qa_feedback）。
#   四张表都在首次使用时自动创建，MySQL 不可用时整块功能静默降级。
# ======================================================================
from core.history_store import HistoryStore, valid_day      # noqa: E402

global_history_store = None


def get_history_store():
    global global_history_store
    if global_history_store is None:
        global_history_store = HistoryStore()
    return global_history_store


def _record_history(username, question, result, plan_type=None, plan_name=None, kg_answer=None,
                    image=""):
    """
    把一次问答 / 处方 / 拍照识病落进历史（同时记行为流水与热门计数）。
    四个入口（/qa、/advisory、/api/advice、/api/diagnose）都调它，避免四份重复代码。

    image：拍照识病的缩略图 URL（选填，只存路径不存二进制）
    整个函数用 try 包住：**记不上历史绝不能影响用户看到答案**，
    失败只打日志并返回 None（前端拿到 None 就不显示反馈区）。
    """
    try:
        hs = get_history_store()
        hid = hs.record(
            username, question,
            kg_answer=(kg_answer if kg_answer is not None else result.get('structured', '')),
            llm_answer=(result.get('answer') or '') if result.get('llm_used') else '',
            plan_type=plan_type or result.get('plan_type', 'general'),
            plan_name=plan_name or result.get('plan_name', ''),
            sources=result.get('sources'),
            entities=result.get('entities'),
            image=image)
        hs.bump_hot(result.get('entities'))
        return hid
    except Exception as e:
        print("WARN: 写历史失败（不影响本次回答）：%s" % e)
        return None


@app.route('/phenology-page')
def phenology_page():
    """物候期提醒页：地块档案 + 今日农事提醒"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('phenology.html', username=session['username'])


@app.route('/api/phenology/crops')
def api_phenology_crops():
    """
    可用于物候推算的作物（有生育期数据的）
    如果该账号被管理端限定了可种植范围，这里只返回被授权的那些 ——
    这是\"管理端决定用户端能种什么\"最直接的落点
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        rows = get_phenology().list_crops()
        if not is_admin():
            allowed = get_user_admin().crops_of(session['username'])
            if allowed:
                aset = set(allowed)
                rows = [r for r in rows if (r.get("name") or "") in aset]
        return jsonify(rows)
    except Exception as e:
        # 把"图谱连不上"和大模型/其它异常区分开，前端才能给出有用的提示
        msg = str(e)
        low = msg.lower()
        if "closed" in low or "unavailable" in low or "refused" in low or "connect" in low:
            msg = "知识图谱（Neo4j）未连接：%s" % msg
        return jsonify({"error": msg}), 500


@app.route('/api/soil/list')
def api_soil_list():
    """
    土质下拉列表（含质地/酸碱度/肥力/改良剂摘要）
    被管理端限定了可用土质的账号，这里只返回被授权的那些
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        rows = get_phenology().list_soils()
        if not is_admin():
            allowed = get_user_admin().soils_of(session['username'])
            if allowed:
                aset = set(allowed)
                rows = [r for r in rows if (r.get("name") or "") in aset]
        return jsonify(rows)
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "closed" in low or "unavailable" in low or "refused" in low or "connect" in low:
            msg = "知识图谱（Neo4j）未连接：%s" % msg
        return jsonify({"error": msg}), 500


@app.route('/api/phenology', methods=['POST'])
def api_phenology():
    """
    单次推算：作物 + 播种日期 (+ 土质 + 土壤 pH) → 当前物候 + 土质建议 + 今日农事 + 施肥 + 病虫害风险
    参数（表单或 JSON）：crop、sow_date(YYYY-MM-DD)、soil(可选)、ph(可选，农户手填)
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    data = request.get_json(silent=True) or request.form
    crop = (data.get('crop') or '').strip()
    sow = (data.get('sow_date') or '').strip()
    soil = (data.get('soil') or '').strip() or None
    ph, ph_err = parse_ph(data.get('ph'))
    if ph_err:
        return jsonify({"error": ph_err}), 400
    if not crop or not sow:
        return jsonify({"error": "请提供作物与播种日期"}), 400
    try:
        return jsonify(get_phenology().reminder(crop, sow, soil=soil, ph=ph))
    except ValueError:
        return jsonify({"error": "播种日期格式应为 YYYY-MM-DD"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/phenology/stage', methods=['POST'])
def api_phenology_stage():
    """
    任意一个阶段的详情（时间轴点击查看用）
    参数：crop、index（阶段下标，0 起）、sow_date（可选，用于标出当前阶段）、
         soil（可选）、ph（可选，农户手填的土壤 pH）
    """
    if not current_username():
        return jsonify({"error": "未登录"}), 403
    data = request.get_json(silent=True) or request.form
    crop = (data.get('crop') or '').strip()
    if not crop:
        return jsonify({"error": "缺少作物"}), 400
    ph, ph_err = parse_ph(data.get('ph'))
    if ph_err:
        return jsonify({"error": ph_err}), 400
    try:
        detail = get_phenology().stage_detail(
            crop, data.get('index'), sow_date=(data.get('sow_date') or '').strip() or None,
            soil=(data.get('soil') or '').strip() or None, ph=ph)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not detail:
        return jsonify({"error": "没有找到该阶段"}), 404
    return jsonify(detail)


@app.route('/api/plot/list')
def api_plot_list():
    """我的地块 + 每个地块的今日提醒摘要（含土质匹配度）+ 今日完成状态"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    store, eng = get_plot_store(), get_phenology()
    try:
        plots = store.list_plots(session['username'])
        # 今日完成状态：读不到（MySQL 不可用）就当作"都还没完成"，不影响浏览
        try:
            tmap = get_task_store().today_map(session['username'])
        except Exception as e:
            print("WARN: 读取待办完成状态失败（按未完成处理）：%s" % e)
            tmap = {}
        today = today_str()
        for p in plots:
            try:
                p["summary"] = eng.summary_line(p["crop"], p["sow_date"],
                                                soil=p.get("soil") or None,
                                                ph=p.get("ph"))
            except Exception as e:
                p["summary"] = {"status": "error", "text": str(e), "risk_count": 0}
            t = tmap.get(p["id"]) or {}
            p["today"] = {"date": today,
                          "done": bool(t.get("done")),
                          "time": t.get("time", ""),
                          "done_count": t.get("done_count", 0)}
        # 每个地块带上最近一次经济效益测算（地块档案侧的回显）
        _attach_plot_econ(plots, session['username'])
        return jsonify({"available": store.available, "error": store.error,
                        "today": today, "plots": plots})
    except Exception as e:
        return jsonify({"available": False, "error": str(e), "plots": []}), 200


@app.route('/api/plot/task', methods=['POST'])
def api_plot_task():
    """
    勾选 / 取消勾选「今天这块地的事已完成」（按天记）。

    JSON：plot_id（必填）、done（默认 true）、task_name（默认 __plot__）、
         date（默认今天）、note（选填）
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    user = session['username']
    d = request.get_json(silent=True) or request.form
    try:
        pid = int(d.get('plot_id'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "缺少 plot_id"}), 400
    raw = d.get('done')
    done = True if raw is None else str(raw).strip().lower() not in ('0', 'false', 'no', '')
    name = (d.get('task_name') or PLOT_TASK).strip() or PLOT_TASK
    date = (d.get('date') or '').strip() or today_str()
    if not valid_date(date):
        return jsonify({"ok": False, "message": "日期格式应为 YYYY-MM-DD"}), 400
    # 只能勾自己名下的地块（归属边界）
    if pid not in [p['id'] for p in get_plot_store().list_plots(user)]:
        return jsonify({"ok": False, "message": "地块不存在或不属于你"}), 404
    st = get_task_store()
    if not st.set_task(user, pid, name, done, date, d.get('note') or ''):
        return jsonify({"ok": False,
                        "message": "数据库不可用，完成状态暂时记不下来"}), 503
    t = st.today_map(user, date).get(pid) or {}
    return jsonify({"ok": True, "plot_id": pid, "date": date, "task_name": name,
                    "done": done,
                    "today": {"date": date, "done": bool(t.get('done')),
                              "time": t.get('time', ''),
                              "done_count": t.get('done_count', 0)}})


@app.route('/api/plot/tasks')
def api_plot_tasks():
    """
    某一天的完成记录（?date=YYYY-MM-DD，默认今天）。
    今日待办页只用到勾选状态，这个接口是留给"完成率/历史回看"的。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    user = session['username']
    date = (request.args.get('date') or '').strip() or today_str()
    if not valid_date(date):
        return jsonify({"ok": False, "message": "日期格式应为 YYYY-MM-DD"}), 400
    st = get_task_store()
    items = st.tasks_of_date(user, date)
    return jsonify({"ok": True, "available": st.available, "error": st.error,
                    "date": date, "count": len(items), "items": items})


# ======================================================================
# 物候提醒「宫格」+ 今日待办 + 独立详情页（2026-09-27 从配套版本移植）
#   物候页右侧铺「我的地块」「全部作物」两组九宫格，点格子进独立整页详情；
#   今日待办按"每块地当前生育期该干什么"出卡片。
#   ⚠ 与配套版本的区别：**勾选状态存 MySQL**（plot_task_log，按条目记），
#     所以每条农事都带 done 标记，不依赖浏览器本地存储。
# ======================================================================
def _default_base_sow():
    """作物宫格的默认基准播种日期：默认 40 天前，让大多作物落在生育中期"""
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=40)).isoformat()


@app.route('/api/grid/plots')
def api_grid_plots():
    """我的地块 → 宫格数据（每格带当前生育期与实拍图 URL）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    store = get_plot_store()
    try:
        # ⚠ get_phenology() 必须在 try 里：Neo4j 没启动时它在构造阶段就抛，
        #   放在外面这一整条接口就是 500，页面只能干瞪眼"加载失败"
        eng = get_phenology()
        plots = store.list_plots(session['username'])
        return jsonify({"available": store.available,
                        "reason": "" if store.available else "db",
                        "error": store.error, "cells": eng.plot_cells(plots)})
    except Exception as e:
        print("WARN: 地块宫格推算失败（图谱连不上？）：%s" % e)
        return jsonify({"available": False, "reason": "graph", "cells": [],
                        "error": "连不上知识图谱（Neo4j 没启动？），暂时算不出地块状态"}), 200


@app.route('/api/grid/crops')
def api_grid_crops():
    """可浏览的作物生育期宫格：只列每种作物的各个生育期。
    不再用假想的"基准播种日期"推算当前阶段 —— 没种过的作物不存在"现在处于哪个时期"。

    ⚠ 被管理端限定了可种植范围的账号，这里**只返回被授权的作物** ——
      跟左侧「作物」下拉（/api/phenology/crops）用同一套规则，两处不能各行其是：
      否则下拉里选不到、宫格里却看得见，用户会以为授权失灵。
      返回里带 `limited`，前端据此把标题从"全部作物"改成"可种植作物"。"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    from urllib.parse import quote as _q
    from core.phenology import stage_image_url
    try:
        eng = get_phenology()          # 同样要在 try 里（Neo4j 没起时构造就抛）

        # 授权表为空 = 未设限（沿用全站既有语义，不在这里制造"什么都不显示"）
        allowed = []
        if not is_admin():
            allowed = get_user_admin().crops_of(session['username'])
        aset = set(allowed)

        out = []
        for r in eng.list_crops():
            name = r["name"]
            if aset and name not in aset:
                continue
            stages = eng.get_stages(name) or []
            img, slist = None, []
            for s in stages:
                slist.append({"name": s.get("name"), "short": s.get("short"),
                              "days": s.get("days"),
                              "start": s.get("start"), "end": s.get("end")})
                if img is None:
                    img = stage_image_url(name, s.get("name") or "")
            out.append({
                "crop": name,
                "season": eng.season(name),
                "regions": r.get("regions") or 0,
                "stage_count": len(slist) or (r.get("stages") or 0),
                "total_days": r.get("total_days"),
                "stages": slist,
                "image": img,
                "url": "/crop/" + _q(name),
            })
        return jsonify({"crops": out, "limited": bool(aset),
                        "allowed": len(aset) or None})
    except Exception as e:
        print("WARN: 作物宫格读取失败（图谱连不上？）：%s" % e)
        return jsonify({"crops": [], "available": False, "reason": "graph",
                        "error": "连不上知识图谱（Neo4j 没启动？），暂时列不出作物生育期"}), 200


@app.route('/api/today/todos')
def api_today_todos():
    """
    今日待办：每块地当前生育期该干的农事。一次请求把全部地块算完。

    每条农事带 done（今天的勾选状态，来自 plot_task_log）；
    all_done = 这条地今天的活全干完了（含"整块地勾过"），前端据此归到「今日已完成」。
    紧急 = 即将进入下一阶段，或当期有危害等级为「重」的病虫害。
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    user = session['username']
    store = get_plot_store()
    empty = {"plots": 0, "tasks": 0, "urgent": 0, "done": 0}
    if not store.available:
        return jsonify({"available": False, "error": store.error or "数据库不可用",
                        "items": [], "counts": empty})

    # 今天的勾选记录：整块地级（__plot__）与条目级都要
    date = today_str()
    plot_done, done_names = {}, {}
    try:
        for t in get_task_store().tasks_of_date(user, date):
            if not t["done"]:
                continue
            if t["task_name"] == PLOT_TASK:
                plot_done[t["plot_id"]] = True        # 整块地勾过 = 其条目都算完成
            else:
                done_names.setdefault(t["plot_id"], set()).add(t["task_name"])
    except Exception as e:
        print("WARN: 读取待办勾选状态失败（当作都没勾）：%s" % e)

    try:
        eng = get_phenology()
        items, tasks, urgent, done_plots = [], 0, 0, 0
        for p in store.list_plots(user):
            pid = p["id"]
            url = "/plot/%s" % pid
            try:
                d = eng.reminder(p["crop"], p["sow_date"],
                                 soil=(p.get("soil") or None), ph=p.get("ph"))
            except Exception as e:                    # 单块地出错不影响其它
                items.append({"plot_id": pid, "plot_name": p["plot_name"],
                              "crop": p["crop"], "url": url, "status": "unknown",
                              "stage": "", "todo": [], "risks": [],
                              "urgent": False, "error": str(e)})
                continue

            whole = bool(plot_done.get(pid))
            marked = done_names.get(pid) or set()
            todo = []
            for t in (d.get("todo") or []):
                nm = t.get("name") or ""
                todo.append({"name": nm, "timing": t.get("timing") or "",
                             "effect": t.get("effect") or "",
                             "done": whole or (nm in marked)})
            risks = d.get("risks") or []
            is_urgent = bool(d.get("next_soon")) or any(r.get("level") == "重" for r in risks)
            ndone = sum(1 for t in todo if t["done"])
            all_done = whole or (bool(todo) and ndone == len(todo))
            tasks += len(todo)
            if all_done:
                done_plots += 1
            elif is_urgent:
                urgent += 1

            items.append({
                "plot_id": pid,
                "plot_name": p["plot_name"],
                "crop": p["crop"],
                "area_mu": p.get("area_mu"),
                "region": p.get("region") or "",
                "url": url,
                "status": d.get("status"),
                "stage": (d.get("stage") or {}).get("short", ""),
                "stage_day": d.get("stage_day"),
                "stage_days": (d.get("stage") or {}).get("days"),
                "message": d.get("message") or "",
                "todo": todo,
                "done_count": ndone,
                "all_done": bool(all_done),
                "risks": [{"name": r.get("name"), "level": r.get("level") or "",
                           "label": r.get("label") or ""} for r in risks[:4]],
                "next_soon": bool(d.get("next_soon")),
                "next_stage": (d.get("next_stage") or {}).get("short") or "",
                "days_to_next": d.get("days_to_next"),
                "urgent": bool(is_urgent and todo and not all_done),
            })

        return jsonify({
            "available": True,
            "date": date,
            "items": items,
            "counts": {"plots": len(items), "tasks": tasks,
                       "urgent": urgent, "done": done_plots},
        })
    except Exception as e:
        print("WARN: 今日待办推算失败：%s" % e)
        return jsonify({"available": False, "reason": "graph", "items": [],
                        "error": "连不上知识图谱（Neo4j 没启动？），今天算不出待办",
                        "counts": empty}), 200


@app.route('/plot/<int:pid>')
def plot_detail_page(pid):
    """地块详情页：独立整页展示该地块今日提醒"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('stage_detail.html', username=session['username'],
                           mode='plot', pid=pid, crop='')


@app.route('/crop/<path:name>')
def crop_detail_page(name):
    """作物详情页：按指定播种日期整页展示该作物的今日提醒"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('stage_detail.html', username=session['username'],
                           mode='crop', crop=name, pid=0)


@app.route('/api/plot/<int:pid>/detail')
def api_plot_detail(pid):
    """某地块的完整提醒（只能看自己名下的地块）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    store = get_plot_store()
    try:
        eng = get_phenology()
        plots = store.list_plots(session['username'])
        p = next((x for x in plots if x['id'] == pid), None)
        if not p:
            return jsonify({"error": "地块不存在或不属于当前账号"}), 404
        data = eng.reminder(p['crop'], p['sow_date'],
                            soil=(p.get('soil') or None), ph=p.get('ph'))
        data['crop'] = p['crop']
        data['plot'] = {"id": p['id'], "plot_name": p['plot_name'],
                        "area_mu": p.get('area_mu'), "region": p.get('region') or '',
                        "soil": p.get('soil') or '', "ph": p.get('ph')}
        # 地块详情页也回显最近一次测算结果
        try:
            emap = get_econ_store().latest_by_plot(session['username'], [p['id']])
            data['econ'] = emap.get(p['id'])
        except Exception as e:
            print("WARN: 读取地块测算摘要失败：%s" % e)
            data['econ'] = None
        data.update(eng.hero(p['crop'], data))
        return jsonify(data)
    except Exception as e:
        print("WARN: 地块详情推算失败：%s" % e)
        return jsonify({"error": "推算失败：连不上知识图谱（Neo4j 没启动？）或数据异常"}), 200


@app.route('/api/crop/detail')
def api_crop_detail():
    """某作物按指定播种日期的完整提醒 + 详情页大图（供 /crop/<name> 详情页用）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    crop = (request.args.get('crop') or '').strip()
    sow = (request.args.get('sow') or '').strip() or _default_base_sow()
    soil = (request.args.get('soil') or '').strip() or None
    ph, ph_err = parse_ph(request.args.get('ph'))
    if ph_err:
        return jsonify({"error": ph_err}), 400
    if not crop:
        return jsonify({"error": "缺少作物名"}), 400
    try:
        eng = get_phenology()
        data = eng.reminder(crop, sow, soil=soil, ph=ph)
        data['crop'] = crop
        data.update(eng.hero(crop, data))
        return jsonify(data)
    except ValueError:
        return jsonify({"error": "播种日期格式应为 YYYY-MM-DD"}), 400
    except Exception as e:
        print("WARN: 作物详情推算失败：%s" % e)
        return jsonify({"error": "推算失败：连不上知识图谱（Neo4j 没启动？）或数据异常"}), 200


@app.route('/api/stage/detail')
def api_stage_detail():
    """指定生育期的资料（详情页时间轴点方块时调用）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    crop = (request.args.get('crop') or '').strip()
    stage = (request.args.get('stage') or '').strip()
    soil = (request.args.get('soil') or '').strip() or None
    cur = (request.args.get('cur') or '').strip() or None
    ph, ph_err = parse_ph(request.args.get('ph'))
    if ph_err:
        return jsonify({"error": ph_err}), 400
    if not crop or not stage:
        return jsonify({"error": "缺少作物名或生育期"}), 400
    try:
        return jsonify(get_phenology().stage_pack(crop, stage, soil=soil,
                                                  cur_index=cur, ph=ph))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/todo')
def todo_alias():
    """导航里用的是 /todo-page，配套版本的地址是 /todo —— 两个都留着"""
    return redirect(url_for('todo_page'))


# ======================================================================
# 意见反馈 + 我的地块（2026-09-28 从配套版本接上）
#   这两个是那批"已拷进来但没路由"的模板里**不冲突、纯新增**的部分：
#     · /my-plots   只读地块列表，与物候提醒页共用 user_plot，不会出现两份数据
#     · /feedback   农户提意见 → 管理端逐条回复（feedback 表首次使用自动建）
#   剩下几个（/admin/users、/admin/plots、/admin/home、/advisory-page）
#   属于配套版本那套"管理端分配地块"的旧模型，与本项目的自建地块模型并存会打架，
#   没有一起接 —— 详见当轮说明。
# ======================================================================
def get_feedback_store():
    global global_feedback_store
    if global_feedback_store is None:
        global_feedback_store = FeedbackStore()
    return global_feedback_store


# 反馈列表的三种口径（待回复 = 未读：侧栏红数字、页面筛选共用同一套判定）
FEEDBACK_STATUS = ("all", "pending", "replied")


def _norm_status(v):
    """把 URL / 表单里传来的 status 收敛到三种合法值，非法值一律当 all"""
    s = (v or "").strip().lower()
    return s if s in FEEDBACK_STATUS else "all"


@app.context_processor
def _inject_feedback_unread():
    """
    侧栏两个数字（**只有管理端才查库**，用户端页面一次都不查）：

      fb_pending  反馈管理 · **红色** = 待回复条数（页面筛选也用同一口径）
      bad_total   答案差评 · **橙色** = 名下范围内被标「没帮助」的条数

    ⚠ 任何异常都吞掉并返回 0 —— 一个侧栏数字不该让整页打不开。
    """
    if not (session.get('username') and is_admin()):
        return {'fb_pending': 0, 'bad_total': 0}
    try:
        n = int((get_feedback_store().stats() or {}).get('pending') or 0)
    except Exception as e:
        print("WARN: 读取待回复数量失败（侧栏不显示未读数字）：%s" % e)
        n = 0
    bad = 0
    try:
        # ⚠ 走 count_unhelpful 这条轻量 COUNT，不要调 feedback_review
        #   （后者会把问题与纠错说明全拉出来，而侧栏每渲染一个页面都要跑一次）
        scope, _ = _history_scope()
        bad = int(get_history_store().count_unhelpful(scope) or 0)
    except Exception as e:
        print("WARN: 读取差评条数失败（侧栏不显示差评数字）：%s" % e)
    return {'fb_pending': n, 'bad_total': bad}


@app.route('/my-plots')
def my_plots_page():
    """我的地块（只读列表）：与物候提醒页共用 user_plot，保证两边永远一致"""
    if 'username' not in session:
        return redirect(url_for('login'))
    plots = []
    try:
        store = get_plot_store()
        if store.available:
            for r in store.list_plots(session['username']):
                r['location'] = r.get('region') or ''    # 模板里这个字段叫 location
                plots.append(r)
            _attach_plot_econ(plots, session['username'])
        else:
            print("WARN: 我的地块读不到（%s）" % (store.error or "MySQL 不可用"))
    except Exception as e:
        print("WARN: 读取地块列表失败：%s" % e)
    return render_template('my_plots.html', username=session['username'], plots=plots)


@app.route('/home')
def home_alias():
    """配套版本的首页地址是 /home —— 302 到工作台，老链接不至于 404"""
    return redirect(url_for('success'))


@app.route('/feedback', methods=['GET', 'POST'])
def feedback():
    """
    意见反馈：登录用户提交，并在下方看到自己的全部反馈与回复。
    ⚠ 数据库不可用时**页面照常打开**，只是提交会失败并说明原因（与其它模块同款降级）
    ⚠ 函数名必须是 `feedback`：拷贝过来的 feedback.html 里写的是 url_for('feedback')，
       改名成 feedback_page 会直接 BuildError → 500
    """
    if 'username' not in session:
        return redirect(url_for('login'))
    user = session['username']
    st = get_feedback_store()
    msg = err = ''
    if request.method == 'POST':
        content = (request.form.get('content') or '').strip()
        if not content:
            err = '反馈内容不能为空'
        elif st.add(user, content):
            msg = '反馈已提交，感谢你的意见'
        else:
            err = '提交失败：' + (st.error or '数据库不可用')
    items = st.mine(user)
    if not st.available:
        err = err or ('暂时读不到反馈记录：%s' % st.error)
    return render_template('feedback.html', username=user, items=items, msg=msg, err=err)


@app.route('/admin/feedback')
def admin_feedback_page():
    """
    管理端：查看全部反馈并逐条回复。?status= 筛选：all（默认）/ pending 待回复 / replied 已回复

    ⚠ tab 上的三个数字来自 `stats()`（**全表口径**），列表才是筛选后的 ——
      切到「待回复」时「全部 / 已回复」的数字不能跟着变，否则点一下就跳不准了。
    """
    if 'username' not in session:
        return redirect(url_for('login'))
    if not is_admin():
        return render_template('admin_denied.html', username=session['username'])
    status = _norm_status(request.args.get('status'))
    st = get_feedback_store()
    return render_template('admin_feedback.html', username=session['username'],
                           items=st.all(status=status), stats=st.stats(),
                           status=status,
                           available=st.available, store_error=st.error,
                           msg=request.args.get('msg', ''), err=request.args.get('err', ''))


@app.route('/admin/feedback/reply', methods=['POST'])
def admin_feedback_reply():
    if 'username' not in session:
        return redirect(url_for('login'))
    if not is_admin():
        return redirect(url_for('login'))
    # 回完跳回原来那个 tab（在「待回复」里回复，别把人甩回「全部」）
    status = _norm_status(request.form.get('status'))
    reply = (request.form.get('reply') or '').strip()
    if not reply:
        return redirect(url_for('admin_feedback_page', status=status,
                                err='回复内容不能为空'))
    if not get_feedback_store().reply(request.form.get('fid'), reply):
        return redirect(url_for('admin_feedback_page', status=status,
                                err='回复失败：' + (get_feedback_store().error
                                                   or '数据库不可用，或这条反馈已不存在')))
    return redirect(url_for('admin_feedback_page', status=status, msg='回复已提交'))


# ======================================================================
# 答案差评汇总（2026-09-28）——“评价 → 改进”这条闭环真正落上的地方
#   农户点「没帮助」写的纠错说明，以前只躺在 qa_feedback 里没人看。
#   这里把它按「同一个问题被反复差评」聚出来：谁最该先改，一眼看得见。
# ======================================================================
@app.route('/admin/bad-answers')
def admin_bad_answers():
    """
    管理端：哪条答案被标没帮助最多。?type= 方案类型，?days=30|90|all（默认 all）

    ⚠ 只有管理端能进（用户端只该看自己的记录，没有"汇总"这回事）；
      统计范围是 `_history_scope()` —— 自己 + 名下用户端，别的人一个都不算。
    """
    if 'username' not in session:
        return redirect(url_for('login'))
    if not is_admin():
        return render_template('admin_denied.html', username=session['username'])

    ptype = (request.args.get('type') or 'all').strip()
    days = (request.args.get('days') or 'all').strip()
    date_from = ''
    if days in ('30', '90', '365'):
        date_from = (_dt.date.today() -
                     _dt.timedelta(days=int(days))).isoformat()

    scope, _ = _history_scope()
    st = get_history_store()
    data = None
    err = ''
    try:
        data = st.feedback_review(scope, limit=200, plan_type=ptype,
                                  date_from=date_from)
    except Exception as e:
        print("WARN: 读取差评汇总失败：%s" % e)
        err = '暂时读不到评价数据：%s' % e
    if not st.available:
        err = err or ('暂时读不到评价数据：%s' % (st.error or '数据库不可用'))
    return render_template('admin_bad_answers.html', username=session['username'],
                           data=data, ptype=ptype, days=days, err=err,
                           available=bool(data is not None))


@app.route('/api/admin/bad-answer/<int:hid>')
def api_admin_bad_answer(hid):
    """
    差评明细里「看原答案」按钮用：取这一条记录的完整问答。

    ⚠ 走 `get_in(scope, id)`：**越界的 id 查不到**（别人的记录不会泄），
      返回 404 而不是 403 —— 不暴露"这条存在但你看不了"。
    """
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    if not is_admin():
        return jsonify({"error": "需要管理端账号"}), 403
    scope, _ = _history_scope()
    try:
        item = get_history_store().get_in(scope, hid)
    except Exception as e:
        print("WARN: 读取差评原答案失败：%s" % e)
        return jsonify({"error": "读取失败：%s" % e}), 500
    if not item:
        return jsonify({"error": "这条记录不存在或不在你的管理范围内"}), 404
    return jsonify(item)


# ======================================================================
# 经济效益测算：页面 + 三个接口（2026-09-26 新增）
#   "种这个能卖多少钱、要投多少钱、最后剩多少" —— 农户选种什么用得上。
# ======================================================================
@app.route('/econ-page')
def econ_page():
    """经济效益测算页：填一组投入产出，自动算收益，几个作物摆一起比"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('econ.html', username=session['username'])


def _econ_payload(username):
    """列表 + 汇总：保存/删除后都返回同一份，前端整体重渲染即可"""
    st = get_econ_store()
    items = st.list_econ(username)
    return {"ok": True, "available": st.available, "error": st.error,
            "items": items, "summary": econ_summarize(items),
            "cost_fields": [{"key": k, "label": v} for k, v in ECON_COST_FIELDS]}


@app.route('/api/econ/list')
def api_econ_list():
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    return jsonify(_econ_payload(session['username']))


@app.route('/api/econ/cost-defaults')
def api_econ_cost_defaults():
    """
    七项投入成本的**参考值**（元/亩），选了作物后前端自动填。

    整包发给前端（按作物 + 按类别 + 兜底三层），选作物时本地查表即可，
    不用每换一个作物就发一次请求。数据本身与用户无关，也不碰数据库 ——
    后端只负责把 core/econ_defaults.py 的那几张表吐出来。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    return jsonify(econ_defaults.payload())


def _resolve_plot(username, raw_id):
    """
    把前端传来的地块 id 换成 (plot_id, plot_name)。

    ⚠ 归属边界：**不在自己名下的地块一律当没传**（静默忽略，不报错也不越权写入）。
    地块不存在（比如刚被删掉）同样返回 (None, '') —— 存一个指向空地块的 id 没有任何意义。
    """
    try:
        pid = int(raw_id)
    except (TypeError, ValueError):
        return None, ""
    if pid <= 0:
        return None, ""
    try:
        for p in get_plot_store().list_plots(username):
            if p.get("id") == pid:
                return pid, p.get("plot_name") or ""
    except Exception as e:
        print("WARN: 校验地块归属失败（按未关联处理）：%s" % e)
    return None, ""


def _attach_plot_econ(plots, username):
    """
    给地块列表补上「最近一次经济效益测算」—— 这就是"测算结果回写到地块档案"。

    ⚠ 刻意不把测算结论写进 user_plot 表：那样会出现两份真相，
      改了测算之后地块档案里的旧数字不会跟着变。这里按 plot_id 反查，
      永远读的是最新那条。数据库不可用时静默降级（econ 留空）。
    """
    if not plots:
        return plots
    try:
        emap = get_econ_store().latest_by_plot(username, [p.get("id") for p in plots])
    except Exception as e:
        print("WARN: 读取地块测算摘要失败（地块档案不显示测算结果）：%s" % e)
        emap = {}
    for p in plots:
        p["econ"] = emap.get(p.get("id"))
    return plots


@app.route('/api/econ/save', methods=['POST'])
def api_econ_save():
    """新增或更新一条测算（带 id 即更新）。JSON 与表单都接受。可带 plot_id 关联地块。"""
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    d = request.get_json(silent=True) or request.form
    eid = d.get('id')
    try:
        eid = int(eid) if eid not in (None, '', '0') else None
    except (TypeError, ValueError):
        eid = None
    # 地块归属在服务端定：前端传什么都行，不是自己的地就当没关联（不报错，避免打断填写）
    pid, pname = _resolve_plot(session['username'], d.get('plot_id'))
    try:
        d = dict(d)                       # request.form 是只读的，先复制
    except TypeError:
        pass
    d['plot_id'] = pid
    d['plot_name'] = pname
    ok, msg, rid = get_econ_store().save(session['username'], d, eid)
    if not ok:
        return jsonify({"ok": False, "message": msg}), (503 if "数据库" in msg else 400)
    payload = _econ_payload(session['username'])
    payload.update({"message": msg, "id": rid})
    return jsonify(payload)


@app.route('/api/econ/delete', methods=['POST'])
def api_econ_delete():
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    d = request.get_json(silent=True) or request.form
    try:
        eid = int(d.get('id'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "缺少 id"}), 400
    st = get_econ_store()
    # 归属边界：不是自己的记录直接当作不存在
    if st.get(session['username'], eid) is None:
        return jsonify({"ok": False, "message": "记录不存在或不属于你"}), 404
    if not st.delete(session['username'], eid):
        return jsonify({"ok": False, "message": "数据库不可用，暂时删不掉"}), 503
    return jsonify(_econ_payload(session['username']))


@app.route('/api/plot/save', methods=['POST'])
def api_plot_save():
    """新增/更新地块"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    data = request.get_json(silent=True) or request.form
    plot_name = (data.get('plot_name') or '').strip() or '未命名地块'
    crop = (data.get('crop') or '').strip()
    sow = (data.get('sow_date') or '').strip()
    if not crop or not sow:
        return jsonify({"error": "作物与播种日期为必填"}), 400
    # 播种日期先校验格式：页面用的是 <input type="date">，正常只会送 YYYY-MM-DD；
    # 但接口谁都能调，脏日期会一路走到 MySQL 才报 1292（而且原来的写法还会谎报"已保存"）
    if not valid_date(sow):
        return jsonify({"ok": False, "message": "播种日期格式应为 YYYY-MM-DD"}), 200
    # 管理端限定了范围时，后端也要拦一道（前端下拉已过滤，这里防绕过）
    if not is_admin():
        ua = get_user_admin()
        allowed = ua.crops_of(session['username'])
        if allowed and crop not in allowed:
            return jsonify({"ok": False, "message":
                            "你的账号未被授权种植「%s」。当前可种：%s。"
                            "如需增加，请联系管理员在你的账号里添加。"
                            % (crop, "、".join(allowed))}), 200
        soil = (data.get('soil') or '').strip()
        soils_allowed = ua.soils_of(session['username'])
        if soil and soils_allowed and soil not in soils_allowed:
            return jsonify({"ok": False, "message":
                            "你的账号未授权使用土质「%s」。当前可用：%s。"
                            "如需增加，请联系管理员在你的账号里添加。"
                            % (soil, "、".join(soils_allowed))}), 200
    area = data.get('area_mu') or None
    try:
        area = float(area) if area not in (None, '') else None
    except (TypeError, ValueError):
        area = None
    # 土壤 pH：农户自己填的（选填），越界或非数字直接拦下 —— 手填数字最容易写成 65
    ph_val, ph_err = parse_ph(data.get('ph'))
    if ph_err:
        return jsonify({"ok": False, "message": ph_err}), 200
    try:
        store = get_plot_store()
        if not store.available:
            return jsonify({"ok": False, "message": "数据库不可用，无法保存地块档案：%s"
                            % store.error}), 200
        # ⚠ 要看返回值：原来不检查，数据库写失败（日期非法 / 连不上）也回"已保存"，
        #   前端于是清空表单 + 刷新列表，农户以为存上了，其实一条都没有
        if not store.save_plot(session['username'], plot_name, crop, sow, area,
                               (data.get('region') or '').strip(),
                               (data.get('soil') or '').strip(),
                               (data.get('note') or '').strip(),
                               ph_val):
            return jsonify({"ok": False, "message": "保存失败：%s"
                            % (store.error or "数据库不可用或写入被拒绝")}), 200
        return jsonify({"ok": True, "message": "已保存"})
    except Exception as e:
        print("WARN: 保存地块失败：%s" % e)
        return jsonify({"ok": False, "message": "保存失败：%s" % e}), 500


@app.route('/api/plot/delete', methods=['POST'])
def api_plot_delete():
    """删除地块（同样要看真实结果：删不掉就别回 ok）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    data = request.get_json(silent=True) or request.form
    pid = data.get('id')
    try:
        store = get_plot_store()
        if not store.delete_plot(session['username'], pid):
            err = store.error or "数据库不可用"
            # error 本身已经说明原因时不再叠"删除失败："前缀，避免出现"删除失败：地块不存在或已被删除"
            msg = err if ("不存在" in err or "已删除" in err) else "删除失败：%s" % err
            return jsonify({"ok": False, "message": msg}), 200
        # cleaned / unlinked 只用于提示与排查：
        # 删地块时连带清掉多少条勾选记录、解绑多少条测算（测算记录本身保留）
        return jsonify({"ok": True,
                        "cleaned": getattr(store, "last_task_cleaned", 0),
                        "unlinked": getattr(store, "last_econ_unlinked", 0)})
    except Exception as e:
        print("WARN: 删除地块失败：%s" % e)
        return jsonify({"ok": False, "message": str(e)}), 500


# ======================================================================
# 拍照识别病虫害（2026-09 新增）
#   上传照片 → 视觉大模型识别 → 回图谱对齐 → 直接给防治方案
#   原图只在内存中处理、不落盘；另存一张压缩缩略图给历史记录回看（save_thumb）
# ======================================================================
from core.vision import DiagnosisEngine, save_thumb, remove_thumb   # noqa: E402

global_diagnosis = None

try:
    app.config['MAX_CONTENT_LENGTH'] = int(
        getattr(__import__('config'), 'VISION_MAX_UPLOAD_MB', 8)) * 1024 * 1024
except Exception:
    app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024


def get_diagnosis():
    global global_diagnosis
    if global_diagnosis is None:
        global_diagnosis = DiagnosisEngine(get_advisor())
    return global_diagnosis


@app.route('/diagnose-page')
def diagnose_page():
    """拍照识病页"""
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('diagnose.html', username=session['username'])


@app.route('/api/vision/status')
def api_vision_status():
    """视觉模型接入状态（不含密钥）"""
    if 'username' not in session:
        return jsonify({"error": "未登录"}), 403
    try:
        return jsonify(get_diagnosis().status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/diagnose', methods=['POST'])
def api_diagnose():
    """
    拍照识别接口
    表单字段：image（文件，可选）、crop（作物，可选）、description（症状描述，可选）
    **原图不落盘**，只在内存中压缩后送模型；但会存一张压缩后的缩略图，
    让「历史记录」能回看"上次拍的是什么"（见 core/vision.py::save_thumb）。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "请先登录"}), 403

    crop = (request.form.get('crop') or '').strip()
    desc = (request.form.get('description') or '').strip()
    f = request.files.get('image')
    raw = None
    if f and f.filename:
        raw = f.read()
        limit = int(getattr(__import__('config'), 'VISION_MAX_UPLOAD_MB', 8)) * 1024 * 1024
        if len(raw) > limit:
            return jsonify({"ok": False,
                            "message": "图片超过 %d MB，请压缩后再传" % (limit // 1024 // 1024)}), 413
    if not raw and not desc:
        return jsonify({"ok": False, "message": "请上传照片，或填写症状描述"}), 400
    try:
        res = get_diagnosis().diagnose(raw, crop_hint=crop, description=desc)

        # ---- 落历史：拍照识病也是一条要留痕的记录 ----
        # 缩略图先存盘（失败就返回空串，历史照记、识别结果照返回）
        thumb = save_thumb(raw) if raw else ""
        res["history_id"] = _record_history(
            session['username'], _diagnose_question(crop, desc), _diagnose_history(res),
            plan_type='diagnose', plan_name='拍照识病',
            kg_answer=_diagnose_kg_summary(res), image=thumb)
        res["image"] = thumb
        return jsonify(res)
    except Exception as e:
        print("拍照识别失败:", e)
        return jsonify({"ok": False, "message": "识别失败：%s" % e}), 500


def _diagnose_question(crop, desc):
    """历史列表里那行标题：有症状描述就用农户的原话，否则说清是哪块地/哪种作物拍的"""
    if desc:
        return desc
    return "拍照识别%s" % ("：%s" % crop if crop else "（未填作物）")


def _diagnose_history(res):
    """
    把诊断结果整理成 _record_history 认得的形状：
    大模型的诊断与建议放进 answer，供历史卡片正文展示。
    （不直接改 res 本体，免得把 answer/llm_used 这两个字段混进接口返回）
    """
    matched = res.get("matched") or {}
    raw = res.get("raw") or {}
    entities = {}
    crop = res.get("crop") or raw.get("crop") or ""
    if crop and crop != "不确定":
        entities["crop"] = crop
    pest = (matched.get("name") or raw.get("problem") or "").strip()
    if pest and pest not in ("无", "无法判断"):
        entities["pest"] = pest
    out = dict(res)
    out["answer"] = res.get("advice") or ""
    out["llm_used"] = True if (res.get("advice") or "").strip() else False
    out["entities"] = entities
    return out


def _diagnose_kg_summary(res):
    """
    历史卡片的「知识图谱答案」一栏：拍这张照片对齐到了哪个图谱节点、还有哪些候选。
    识别过程本身就是一次"图谱对齐"，把它写清楚，农户才知道这条结论是从哪儿来的。
    """
    if not res.get("ok"):
        return res.get("message") or "识别未完成"
    lines = []
    raw = res.get("raw") or {}
    matched = res.get("matched") or {}
    if res.get("mode") == "text":
        lines.append("按文字描述匹配图谱知识（未上传照片）")
    else:
        if raw.get("crop"):
            lines.append("照片里的作物：%s" % raw["crop"])
        if raw.get("category"):
            lines.append("类别：%s（置信度：%s）" % (raw.get("category"), raw.get("confidence") or "-"))
        if raw.get("evidence"):
            lines.append("可见症状：%s" % raw["evidence"])
    if matched:
        score = matched.get("score")
        lines.append("图谱对齐：%s%s" % (matched.get("name"),
                                   ("（相似度 %s）" % score) if score is not None else ""))
    else:
        lines.append("图谱对齐：未匹配到明确节点%s" % (
            ("，候选：" + "、".join(c.get("name") for c in (res.get("candidates") or [])[:3]))
            if res.get("candidates") else ""))
    if (res.get("sources") or []):
        lines.append("引用图谱关系 %d 条" % len(res["sources"]))
    return "\n".join(lines)


@app.errorhandler(413)
def _too_large(e):
    return jsonify({"ok": False, "message": "图片太大，请压缩到 8MB 以内"}), 413


# ======================================================================
# 方案导出（Word / PDF）
#   前端把当前展示的方案内容 POST 过来，后端就地生成文件返回下载
#   文件在内存中生成，不落盘
# ======================================================================
from core import exporter as _exporter        # noqa: E402
import datetime as _dt                        # noqa: E402


@app.route('/api/export/<fmt>', methods=['POST'])
def api_export(fmt):
    """
    导出方案为 Word / PDF
    fmt: docx | pdf
    表单/JSON 字段：title（方案名）、markdown（正文）、sources（溯源，数组或换行文本）、crop（可选）
    """
    user = current_username()
    if not user:
        return jsonify({"ok": False, "message": "请先登录"}), 403
    if fmt not in ("docx", "pdf"):
        return jsonify({"ok": False, "message": "只支持 docx 与 pdf"}), 400

    data = request.get_json(silent=True) or request.form
    title = (data.get('title') or '').strip() or '农事方案'
    markdown = (data.get('markdown') or '').strip()
    crop = (data.get('crop') or '').strip()
    if not markdown:
        return jsonify({"ok": False, "message": "没有可导出的内容"}), 400

    raw_sources = data.get('sources') or []
    if isinstance(raw_sources, str):
        raw_sources = [x.strip() for x in raw_sources.split("\n") if x.strip()]
    sources = [str(x) for x in raw_sources][:60]

    now = _dt.datetime.now()
    meta = []
    if crop:
        meta.append("适用作物：%s" % crop)
    meta.append("生成时间：%s" % now.strftime("%Y-%m-%d %H:%M"))
    meta.append("生成账号：%s" % user)
    meta.append("知识来源：农作物知识图谱（Neo4j 本地库）")

    try:
        if fmt == "docx":
            buf = _exporter.export_docx(title, meta, markdown, sources)
            mime = ("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document")
        else:
            buf = _exporter.export_pdf(title, meta, markdown, sources)
            mime = "application/pdf"
    except Exception as e:
        print("导出失败:", e)
        return jsonify({"ok": False, "message": "导出失败：%s" % e}), 500

    safe = "".join(c for c in title if c not in '\\/:*?"<>|').strip() or "农事方案"
    filename = "%s_%s.%s" % (safe, now.strftime("%Y%m%d"), fmt)
    return send_file(buf, mimetype=mime, as_attachment=True, download_name=filename)


# ======================================================================
# 历史记录：导出 + 图表统计（2026-09-24 新增）
#   · 导出复用 exporter.py 那套（Word 用 python-docx、PDF 用 fpdf2），
#     内容按「类型筛选 / 关键词 / 用户端」走，与列表页看到的完全一致；
#   · 统计给历史页的四张图供数据：柱状（类型或用户端）、折线（近 14 天）、
#     饼图（类型占比）、词云（问题高频词 —— 拿图谱里的作物/病虫害名去问题里数）。
# ======================================================================

# 词云里的"农事意图词"：图谱实体名之外的常见问法
_INTENT_WORDS = [
    "防治", "用药", "打药", "施肥", "追肥", "基肥", "水肥", "灌溉", "浇水", "排水",
    "播种", "育苗", "移栽", "定植", "整地", "轮作", "茬口", "倒伏", "防寒", "抗旱",
    "土壤", "土质", "酸碱度", "气候", "气温", "降水", "产区", "生育期", "成熟", "收获",
    "病害", "虫害", "杂草", "安全间隔期", "品种", "产量", "品质", "贮藏",
]

_HISTORY_TERMS = None


def history_terms():
    """
    词云用的词表 = 图谱里的作物名 + 病虫害名 + 上面的意图词。
    首次调用时读一次图谱并缓存；读不到就只用意图词兜底（词云照出，不报错）。
    按长度倒序，让「小麦条锈病」先于「小麦」命中。
    """
    global _HISTORY_TERMS
    if _HISTORY_TERMS is None:
        terms = set(_INTENT_WORDS)
        try:
            conn = get_advisor().conn
            for lab in ("作物", "病虫害"):
                rows = conn._run("MATCH (n:`%s`) RETURN n.name AS name LIMIT 400" % lab)
                for r in rows:
                    name = str(r.get("name") or "").strip()
                    if len(name) >= 2:          # 单字词太容易误伤，丢掉
                        terms.add(name)
        except Exception as e:
            print("WARN: 词云词表读取失败（只用意图词兜底）：%s" % e)
        _HISTORY_TERMS = sorted(terms, key=len, reverse=True)
    return _HISTORY_TERMS


def _count_terms(texts, terms, top=36):
    """
    在问题文本里数词。**命中后把该词从文本里挖掉**，
    避免"小麦条锈病"同时被算成「小麦条锈病」和「小麦」两票。
    """
    hits = {}
    for t in texts:
        s = str(t or "")
        if not s:
            continue
        for w in terms:
            if w and w in s:
                hits[w] = hits.get(w, 0) + 1
                s = s.replace(w, " ")
    items = [{"name": k, "value": v} for k, v in hits.items()]
    items.sort(key=lambda x: (-x["value"], x["name"]))
    return items[:top]


@app.route('/api/history/stats')
def api_history_stats():
    """
    历史记录的图表数据：按类型 / 近 14 天趋势 / 按用户端（管理端）/ 问题高频词。
    可见范围与列表一致（管理端 = 自己 + 名下用户端）。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    all_scope, admin_view = _history_scope()
    want = (request.args.get('user') or '').strip()
    scope = [want] if (admin_view and want in all_scope) else all_scope
    st = get_history_store()
    ptype, kw, df, dt = _history_filters()
    try:
        # 统计跟列表同条件：原来这里**完全没读** type/q（页面却写着"基于当前筛选"），
        # 数字一直是全量，跟列表对不上
        s = st.stats_multi(scope, plan_type=ptype, keyword=kw, date_from=df, date_to=dt)
        daily = st.daily_counts(scope, days=14, plan_type=ptype, keyword=kw,
                                date_from=df, date_to=dt)
        words = _count_terms(st.questions(scope, limit=800, plan_type=ptype, keyword=kw,
                                          date_from=df, date_to=dt),
                             history_terms(), top=36)
        return jsonify({"ok": True, "available": st.available, "error": st.error,
                        "is_admin": admin_view,
                        "scope_users": all_scope if admin_view else [],
                        "by_type": s.get("by_type", {}),
                        "by_user": s.get("by_user", {}) if admin_view else {},
                        "total": s.get("total", 0),
                        "helpful": s.get("helpful", 0),
                        "unhelpful": s.get("unhelpful", 0),
                        "daily": daily, "words": words})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e), "by_type": {}, "by_user": {},
                        "daily": [], "words": []}), 500


@app.route('/api/history/export')
def api_history_export():
    """
    把历史记录导出成 Word / PDF：`?fmt=docx|pdf`（默认 docx），
    筛选参数与列表一致：`?type=`（类型）、`?q=`（关键词）、`?from=&to=`（日期区间）、
    `?user=`（管理端指定用户端）。
    管理端导出的是**名下用户端**的记录，每条都标着是谁问的。

    ⚠ 导出不走分页（一次取筛选后的全部，上限 5000 条）—— 导出的就是要完整内容，
      分页导出没有意义；上限只是为了防止不带筛选直接导出把内存打满。

    拍照识病的记录会**把照片一起嵌进文档**（Word 与 PDF 都支持）：
      `qa_history.image` 存的是缩略图 URL，这里经 `_history_photo_path()` 翻译成本地
      绝对路径再交给 `exporter`（markdown 里写成 `![图注](路径)`）。
      照片文件已被删除时降级为一行说明，不会让整份导出失败。
    """
    if 'username' not in session:
        return jsonify({"ok": False, "message": "未登录"}), 403
    fmt = (request.args.get('fmt') or 'docx').strip().lower()
    if fmt not in ("docx", "pdf"):
        return jsonify({"ok": False, "message": "只支持 docx 与 pdf"}), 400
    ptype, kw, df, dt = _history_filters()
    all_scope, admin_view = _history_scope()
    want = (request.args.get('user') or '').strip()
    scope = [want] if (admin_view and want in all_scope) else all_scope
    st = get_history_store()
    try:
        if admin_view:
            items = st.list_multi(scope, limit=5000, plan_type=ptype, keyword=kw,
                                  date_from=df, date_to=dt)
        else:
            items = st.list(session['username'], limit=5000, plan_type=ptype, keyword=kw,
                            date_from=df, date_to=dt)
    except Exception as e:
        return jsonify({"ok": False, "message": "读取记录失败：%s" % e}), 500
    if not items:
        return jsonify({"ok": False, "message": "当前筛选下没有可导出的记录"}), 400

    lines = []
    photo_n = 0
    for i, it in enumerate(items, 1):
        who = ""
        if admin_view and it.get("username"):
            who = "　·　用户端 %s" % it["username"]
        lines.append("## %d. %s｜%s%s" % (i, it.get("plan_name") or "记录",
                                          it.get("create_time") or "", who))
        lines.append("")
        lines.append("**问题：** %s" % (it.get("question") or "（无）"))
        lines.append("")
        # 拍照识病：把当时上传的照片嵌进文档（放在问题后面、答案前面，看的人先见照片）
        photo = _history_photo_path(it.get("image"))
        if photo:
            photo_n += 1
            lines.append("![识病照片（当时拍照上传的原图缩略图）](%s)" % photo)
            lines.append("")
        elif it.get("image"):
            lines.append("（识病照片文件已不存在）")
            lines.append("")
        answer = (it.get("llm_answer") or it.get("kg_answer") or "").strip() or "（无答案内容）"
        lines.append(answer)
        srcs = it.get("sources") or []
        if srcs:
            lines.append("")
            lines.append("**知识溯源（%d 条）**" % len(srcs))
            for s in srcs[:40]:
                lines.append("- %s" % s)
        fb = it.get("feedback") or {}
        if fb.get("rating"):
            lines.append("")
            lines.append("**我的评价：** %s%s" % (
                "有帮助" if fb["rating"] == "helpful" else "没帮助",
                ("　" + fb["comment"]) if fb.get("comment") else ""))
        lines.append("")
    markdown = "\n".join(lines)

    now = _dt.datetime.now()
    title = "名下用户端历史记录" if admin_view else "我的历史记录"
    meta = ["导出时间：%s" % now.strftime("%Y-%m-%d %H:%M"),
            "导出账号：%s" % session['username'],
            "记录条数：%d 条" % len(items)]
    filt = "类型 %s" % (ptype if ptype and ptype != "all" else "全部")
    if kw:
        filt += "　关键词「%s」" % kw
    if df or dt:
        filt += "　日期 %s ~ %s" % (df or "不限", dt or "不限")
    if admin_view and len(scope) == 1:
        filt += "　仅用户端 %s" % scope[0]
    meta.append("筛选条件：%s" % filt)
    if photo_n:
        meta.append("含识病照片：%d 张（拍照识病记录自带的原图缩略图）" % photo_n)
    meta.append("知识来源：农作物知识图谱（Neo4j 本地库）")

    try:
        if fmt == "docx":
            buf = _exporter.export_docx(title, meta, markdown, [])
            mime = ("application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document")
        else:
            buf = _exporter.export_pdf(title, meta, markdown, [])
            mime = "application/pdf"
    except Exception as e:
        print("历史记录导出失败:", e)
        return jsonify({"ok": False, "message": "导出失败：%s" % e}), 500

    filename = "%s_%s.%s" % (title, now.strftime("%Y%m%d"), fmt)
    return send_file(buf, mimetype=mime, as_attachment=True, download_name=filename)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)