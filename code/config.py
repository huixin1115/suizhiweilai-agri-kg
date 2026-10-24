#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一配置入口
=============
所有连接信息、大模型密钥集中在这里，避免散落在各文件。

关于大模型
----------
本系统通过 **OpenAI 兼容接口**（POST {base_url}/chat/completions）调用大模型，
因此豆包（火山方舟）、DeepSeek、腾讯云模型路由 CMR、WorkBuddy 自定义模型、
Ollama / vLLM 本地服务都可以直接使用 —— 只要它兼容 OpenAI 协议。

**当前默认接入：豆包（火山方舟 Ark）**
    接口地址  https://ark.cn-beijing.volces.com/api/v3
    模型名    doubao-seed-2-1-pro-260915（原生多模态，图文都能处理）
    Key       在火山方舟控制台「API Key 管理」创建，形如 ark-xxxxxxxx

注意：豆包 Seed 系列默认会先吐一大段「思维链」再给正文，实测同一问题
27.9 秒 → 关掉思维链后 4.4 秒，且 max_tokens 不再被思维链吃掉。
所以 doubao 这个预置里带了 `params = {"thinking": {"type": "disabled"}}`，
调用方会把它合并进请求体。别的提供方不认识这个字段，所以做成"按提供方预置"。

在 WorkBuddy 里接模型时的对应关系（系统设置 → 模型 → 添加模型）：

    WorkBuddy 表单字段           →   本文件变量
    ------------------------------------------------------
    接口地址 / Base URL          →   LLM_BASE_URL
    API Key                      →   LLM_API_KEY
    模型名称 / Model ID          →   LLM_MODEL

注意：WorkBuddy 客户端本身不生产模型，它是模型调度器 —— 填在它「自定义模型」
里的那个接口地址和 Key，就是可以直接拿来给本项目用的东西。

敏感信息填写方式（任选其一，后者优先级更高）：
1. 直接改本文件
2. 新建同目录 config_local.py（推荐，不会被误提交）：
       LLM_PROVIDER = "doubao"
       LLM_API_KEY  = "ark-xxxxxxxx"
3. 环境变量：LLM_PROVIDER / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
"""
import os

# ------------------------------------------------------------------ Neo4j
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PWD = os.environ.get("NEO4J_PWD", "123456")

# ------------------------------------------------------------------ MySQL
MYSQL_CONFIG = {
    "host": os.environ.get("MYSQL_HOST", "localhost"),
    "user": os.environ.get("MYSQL_USER", "root"),
    # 口令不写默认值：公开仓库里不能出现数据库口令。
    # 本机开发时把它放进 config_local.py（该文件已在 .gitignore 中），
    # 或用环境变量 MYSQL_PWD 传入。
    "password": os.environ.get("MYSQL_PWD", ""),
    "database": os.environ.get("MYSQL_DB", "login_system"),
}

# ------------------------------------------------------------------ 存储后端
# mysql（默认，本机开发用）｜ sqlite（免安装交付版：单文件、零安装、不用起服务）
# ⚠ 交付给别人时把这里改成 "sqlite"（或写进 config_local.py），对方就不用装 MySQL 了
DB_BACKEND = os.environ.get("DB_BACKEND", "mysql")
SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "app_data.sqlite3"),
)


# ==================================================================
#  大模型接入（OpenAI 兼容）
# ==================================================================
# 首选提供方：doubao | deepseek | workbuddy | cmr | ollama | custom | off
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "doubao")

# 各提供方预置参数（URL 末段由代码统一补 /chat/completions）
PROVIDERS = {
    # 豆包 —— 火山方舟（Volcengine Ark），OpenAI 兼容
    #   Base URL 固定为 https://ark.cn-beijing.volces.com/api/v3
    #   模型名既可填「模型 ID」（如 doubao-seed-2-1-pro-260915），
    #   也可填自建的「推理接入点 ID」（形如 ep-2024xxxx-xxxxx）
    "doubao": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-2-1-pro-260915",
        # ⚠ 关键：豆包 Seed 系列默认会先输出一大段「思维链」再给正文。
        #   实测同一个问题：默认 27.9 秒 → 关掉后 4.4 秒（快 6 倍），
        #   而且 max_tokens 不再被思维链吃掉（短回答场景 60 秒 → 9.6 秒）。
        #   别的提供方不认识这个字段，所以做成「按提供方预置的附加请求参数」。
        "params": {"thinking": {"type": "disabled"}},
    },
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "params": {},
    },
    # 腾讯云「模型路由 CMR」—— 腾讯云官方推荐在 WorkBuddy 中接入的方式
    # 接口地址形如 https://<你的路由域名>/v1
    "cmr": {
        "label": "腾讯云模型路由 CMR",
        "base_url": "",
        "model": "ModelRouter/auto",
        "params": {},
    },
    # 在 WorkBuddy 里添加的「自定义模型」—— 把表单里的三项照搬过来
    "workbuddy": {
        "label": "WorkBuddy 自定义模型",
        "base_url": "",
        "model": "",
        "params": {},
    },
    # 本地推理服务（Ollama / vLLM / LM Studio 等）
    "ollama": {
        "label": "本地模型服务",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "params": {},
    },
    "custom": {
        "label": "其他 OpenAI 兼容服务",
        "base_url": "",
        "model": "",
        "params": {},
    },
    "off": {
        "label": "不调用大模型（纯图谱结构化输出）",
        "base_url": "",
        "model": "",
        "params": {},
    },
}

# 显式指定时优先，留空则取 LLM_PROVIDER 对应预置值
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# 附加请求参数：会与 PROVIDERS[provider]["params"] 合并后一起发出去。
# 例：LLM_EXTRA_PARAMS = {"thinking": {"type": "enabled"}}
LLM_EXTRA_PARAMS = {}

# 调用参数
# 超时给 90 秒：实测豆包偶发 23 秒、甚至 60 秒无响应（多数只要 2~4 秒），
# 之前设 60 秒会把这批慢请求直接判失败 → 降级成图谱结构化输出。
LLM_TIMEOUT = 90
# 失败自动重试次数：配合上面的波动，重试一次基本就能拿到结果
LLM_RETRIES = 2
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 1400

# ---- 空小节补全（图谱没覆盖到的条目，改由大模型用通用农技知识补齐）----
# 主答案仍然只允许使用图谱事实；生成完之后扫一遍，凡是写着「图谱暂无此项数据」
# 的小节（例如防治建议里的"物理防治""生物防治"），再发一次请求让大模型补齐，
# 并以「补充：通用农技建议（大模型生成，非本图谱数据）」单独成节接在末尾。
# 这样既补上了内容，也不会让人分不清哪句来自图谱、哪句来自模型。
# 关掉它（False）就回到"没有就写一行「图谱暂无此项数据」"的老行为。
ENABLE_LLM_SUPPLEMENT = True
# 补全那一次请求的 token 上限：比主答案小，只补缺的几条，不需要长篇大论
SUPPLEMENT_MAX_TOKENS = 900

# ==================================================================
#  视觉模型（拍照识别病虫害用）
# ==================================================================
# 说明：**纯文本模型看不了图**（例如 deepseek-chat），所以拍照识病需要一个
#       支持视觉（多模态）的模型。留空即视为未启用，页面会提示改用
#       「文字描述症状」的方式，功能不会报错。
#
# 当前用的豆包 doubao-seed-2-1-pro 本身就是原生多模态 —— 图文同一个模型搞定，
# 所以下面三项留空也能用：resolve_vision() 会自动复用主模型（见 VISION_HINTS）。
#
# 如果想换成别的视觉模型（都兼容 OpenAI 协议，填三项即可）：
#   通义千问 VL   Base URL https://dashscope.aliyuncs.com/compatible-mode/v1
#                 模型名   qwen-vl-max
#   智谱 GLM-4V   Base URL https://open.bigmodel.cn/api/paas/v4
#                 模型名   glm-4v-plus
#   OpenAI        Base URL https://api.openai.com/v1    模型名 gpt-4o
LLM_VISION_BASE_URL = os.environ.get("LLM_VISION_BASE_URL", "")
LLM_VISION_API_KEY = os.environ.get("LLM_VISION_API_KEY", "")
LLM_VISION_MODEL = os.environ.get("LLM_VISION_MODEL", "")

# 视觉识别参数
VISION_MAX_EDGE = 1280         # 上传图片长边压缩到该像素，降低 token 消耗
VISION_JPEG_QUALITY = 85
VISION_MAX_UPLOAD_MB = 8       # 单张图片大小上限

# ---- 向后兼容：旧字段仍可用（之前填的是 DEEPSEEK_API_KEY）----
DEEPSEEK_API_KEY = ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT = LLM_TIMEOUT
DEEPSEEK_TEMPERATURE = LLM_TEMPERATURE

# 本地小模型（无外部 API 时的兜底，CPU 上较慢，演示建议保持 False）
ENABLE_LOCAL_LLM = False
LOCAL_LLM_DIR = "Qwen3.5-0.5B-Chat"
EMBED_MODEL_DIR = "bge-small-zh-v1.5"

# ------------------------------------------------------------------ 路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "agri_kg_ext")
OUT_DIR = os.path.join(BASE_DIR, "output")
KG_JSON = os.path.join(DATA_DIR, "agri_kg.json")
QA_CSV = os.path.join(DATA_DIR, "qa_pairs.csv")

# ==================================================================
#  本地知识库（检索链路的**第二级**）
# ==================================================================
# 知识问答与决策处方的答案按三级逐级兜底：
#     ① 知识图谱   —— Neo4j 结构化查询，命中就转成三元组，交给豆包整理话术
#     ② 本地知识库 —— 原始语料切块 + bge 向量召回，命中就交给豆包整理话术
#     ③ 大模型     —— 前两级都没有，豆包直接生成，并在答案里标注来源
#
# ⚠ 第 ② 级的数据源是 raw_data/raw_agri_data.txt（建图谱前采集的原始农业文本），
#   不是图谱三元组的向量副本 —— 后者的数据源就是图谱本身，图谱查空时它多半也
#   召不回新东西，等于没有第二级。
ENABLE_LOCAL_KB = True
LOCAL_KB_FILE = os.path.join(BASE_DIR, "raw_data", "raw_agri_data.txt")
LOCAL_KB_VEC_CACHE = os.path.join(OUT_DIR, "local_kb_vectors.npz")
LOCAL_KB_CHUNK = 260          # 每个文本块的目标字数
LOCAL_KB_OVERLAP = 1          # 相邻块重叠的句子数，防止一句话被切断
LOCAL_KB_TOP_K = 5            # 最多召回几段
LOCAL_KB_THRESHOLD = 0.58     # 相似度下限：低于此值视为"没命中"，交给大模型兜底
                              # （实测：语料里真有答案 0.66~0.69，纯蹭到实体 0.51~0.54）

# 第 ① 级（知识图谱）的**命中门槛**：图谱事实与问题的相关度低于此值，
# 就当图谱"没有"，转去查本地知识库。
# 定这个数的依据：只要问句里出现任一实体名（"葡萄园能用无人机授粉吗"里的"葡萄"），
# 图谱聚合器就能铺出两三千字事实 —— 不加门槛的话每一问都算命中，
# 第 ②③ 级永远触发不了，答案还会跑偏。
# 实测最相关块分数：图谱真有答案 0.649~0.844；只是蹭到实体名 0.480~0.560。
GRAPH_HIT_THRESHOLD = 0.60

# 覆盖：本地私有配置优先
try:
    from config_local import *          # noqa: F401,F403
    print("INFO: 已加载 config_local.py 私有配置")
except ImportError:
    pass


# ==================================================================
#  解析最终生效的模型参数
# ==================================================================
def resolve_llm():
    """
    返回当前生效的模型配置：
    {provider, label, base_url, model, api_key, params, ready}
    优先级：LLM_* 显式变量 > provider 预置 > 旧版 DEEPSEEK_* 字段
    params 是附加请求参数（如豆包关思维链的 thinking），调用方需合并进请求体。
    """
    p = (os.environ.get("LLM_PROVIDER") or LLM_PROVIDER or "doubao").strip()
    preset = PROVIDERS.get(p, PROVIDERS["custom"])

    base_url = (os.environ.get("LLM_BASE_URL") or LLM_BASE_URL or preset["base_url"] or "").strip()
    model = (os.environ.get("LLM_MODEL") or LLM_MODEL or preset["model"] or "").strip()
    key = (os.environ.get("LLM_API_KEY") or LLM_API_KEY or "").strip()

    # 兼容旧配置：只填了 DEEPSEEK_API_KEY 的情况
    if not key:
        key = (os.environ.get("DEEPSEEK_API_KEY") or globals().get("DEEPSEEK_API_KEY") or "").strip()
        if key and not base_url:
            base_url = globals().get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
            model = model or (globals().get("DEEPSEEK_MODEL") or "deepseek-chat")

    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]

    # 附加请求参数：预置 → 配置覆盖
    params = dict(preset.get("params") or {})
    params.update(globals().get("LLM_EXTRA_PARAMS") or {})

    return {
        "provider": p,
        "label": preset["label"],
        "base_url": base_url,
        "model": model,
        "api_key": key,
        "params": params,
        "ready": bool(base_url and model and key and p != "off"),
    }


# 视觉模型名里出现这些字样，说明它"看得见"，可以直接复用主模型配置
# （豆包 Seed 系列原生多模态，所以 doubao 也在其中）
VISION_HINTS = ("doubao-seed", "doubao-vision", "vl", "vision", "gpt-4o",
                "gpt-4-turbo", "gpt-4.1", "gemini", "claude-3", "claude-4",
                "glm-4v", "qwen-vl", "internvl", "llava", "step-1v",
                "moonshot-v1-vision", "ernie-4", "hunyuan-vision")


def resolve_vision():
    """
    解析视觉模型配置（拍照识病用）
    优先级：LLM_VISION_* 专用配置 > （主模型本身支持视觉时）复用主模型 > 不可用
    """
    vb = (os.environ.get("LLM_VISION_BASE_URL") or LLM_VISION_BASE_URL or "").strip()
    vk = (os.environ.get("LLM_VISION_API_KEY") or LLM_VISION_API_KEY or "").strip()
    vm = (os.environ.get("LLM_VISION_MODEL") or LLM_VISION_MODEL or "").strip()
    if vb and vk and vm:
        base_url = vb.rstrip("/")
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        main = resolve_llm()
        return {"base_url": base_url, "model": vm, "api_key": vk,
                "params": dict(main.get("params") or {}),
                "ready": True, "source": "vision", "reason": ""}

    main = resolve_llm()
    low = main["model"].lower()
    if main["ready"] and any(h in low for h in VISION_HINTS):
        return {"base_url": main["base_url"], "model": main["model"],
                "api_key": main["api_key"], "params": dict(main.get("params") or {}),
                "ready": True, "source": "main", "reason": ""}

    reason = "未配置视觉模型"
    if main["ready"]:
        reason = ("当前主模型「%s」不支持图片输入（纯文本模型无法看图），"
                  "请在 config.py / config_local.py 中单独配置 LLM_VISION_* 三项"
                  % main["model"])
    return {"base_url": "", "model": "", "api_key": "", "params": {},
            "ready": False, "source": "none", "reason": reason}


def deepseek_ready():
    """向后兼容旧调用点"""
    return resolve_llm()["ready"]


def llm_ready():
    return resolve_llm()["ready"]


if __name__ == "__main__":
    print("Neo4j  :", NEO4J_URI, NEO4J_USER)
    print("MySQL  :", MYSQL_CONFIG["host"], MYSQL_CONFIG["database"])
    cfg = resolve_llm()
    print("模型提供方:", cfg["provider"], "—", cfg["label"])
    print("接口地址  :", cfg["base_url"] or "(未配置)")
    print("模型名称  :", cfg["model"] or "(未配置)")
    print("API Key   :", ("已配置 %s***" % cfg["api_key"][:8]) if cfg["api_key"] else "(未配置)")
    print("附加参数  :", cfg["params"] or "(无)")
    print("可用状态  :", "可用" if cfg["ready"] else "不可用 → 将使用图谱结构化输出")
    vis = resolve_vision()
    print("-" * 46)
    print("视觉模型  :", vis["model"] or "(未配置)", "" if vis["ready"] else "→ 拍照识病不可用")
    if vis["ready"]:
        print("  接口地址:", vis["base_url"], "（来源：%s）"
              % ("专用配置" if vis["source"] == "vision" else "复用主模型"))
    else:
        print("  说明    :", vis["reason"])
    print("数据目录  :", DATA_DIR, os.path.exists(DATA_DIR))
