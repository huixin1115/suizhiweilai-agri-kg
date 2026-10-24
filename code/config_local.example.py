# -*- coding: utf-8 -*-
"""
私有配置模板（config_local.example.py）
=======================================
使用方法：
    1. 复制本文件为 config_local.py
         Windows:  copy config_local.example.py config_local.py
         macOS/Linux:  cp config_local.example.py config_local.py
    2. 把下面的 LLM_API_KEY 换成你自己的密钥
    3. 重启服务

⚠ config_local.py 已在 .gitignore 中排除，不会被提交到代码仓库。
   私自把自己项目的密钥提交到公开仓库，会被平台扫描并自动吊销。

本文件优先级高于 config.py：config.py 末尾会尝试 `from config_local import *`，
同名变量覆盖 config.py 里的默认值。没有这个文件也能运行，只是大模型相关功能降级。
"""

# ==================================================================
#  大模型（火山方舟 / 豆包）
# ==================================================================
# 说明：项目所有大模型调用统一走 core/llm_client.py，只认下面这几项配置。
#       不填 API Key 也能启动，此时：
#         · 知识问答 → 只输出知识图谱的结构化事实
#         · 决策处方 → 只输出图谱规则生成的方案骨架
#         · 拍照识病 → 提示"视觉模型未配置"，可改用文字描述症状
# ==================================================================
LLM_PROVIDER = "doubao"
LLM_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LLM_API_KEY = "ark-请替换为你自己的密钥"
LLM_MODEL = "doubao-seed-2-1-pro-260915"

# 豆包 Seed 系列默认会先输出思维链，实测让一次问答从 4.4 秒涨到 27.9 秒，
# 所以统一关掉（config.py 的 provider 预置里也已带这一项）。
LLM_EXTRA_PARAMS = {}

# ==================================================================
#  视觉模型（拍照识病）
# ==================================================================
# 留空 = 自动复用上面的主模型。doubao-seed-2-1-pro 本身是原生多模态，
# 图文共用一个模型，不需要单独再配一个视觉模型。
#
# 如果要换成别的视觉模型（均兼容 OpenAI 协议）：
#   通义千问 VL   https://dashscope.aliyuncs.com/compatible-mode/v1  →  qwen-vl-max
#   智谱 GLM-4V   https://open.bigmodel.cn/api/paas/v4               →  glm-4v-plus
#   OpenAI        https://api.openai.com/v1                          →  gpt-4o
# ==================================================================
LLM_VISION_BASE_URL = ""
LLM_VISION_API_KEY = ""
LLM_VISION_MODEL = ""

# ==================================================================
#  存储后端（可选）
# ==================================================================
# 默认走 MySQL（本机开发）。想用单文件 SQLite（免安装 / 演示），
# 把下面两行取消注释即可 —— 无需安装任何数据库服务。
#
#    DB_BACKEND = "sqlite"
# ==================================================================
