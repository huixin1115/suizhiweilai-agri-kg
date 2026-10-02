#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一的大模型调用客户端（OpenAI 兼容）
==========================================
全项目**只有这里**真正向大模型发 HTTP 请求，其它模块都调本模块的函数，
避免"三个地方各写一份、改一处漏两处"。

当前接入：豆包（火山方舟 Volcengine Ark），配置见 config.py / config_local.py。

两个要点（都是实测踩出来的，写在代码里免得以后忘）
--------------------------------------------------
1. **豆包要关掉思维链**：Seed 系列默认先输出一大段「思维链」再给正文。
   实测同一个问题：默认 27.9 秒 → 关掉后 4.4 秒，且 max_tokens 不再被思维链吃掉
   （短回答场景 60 秒 → 9.6 秒）。参数由 config 里 provider 的 `params` 带出，
   本模块负责合并进请求体。

2. **必须重试**：实测连续 6 次同样的短请求，耗时分别是
   10.1 / 4.1 / 2.1 / 超时60s / 1.7 / 23.0 秒 —— 服务端延迟波动很大。
   只调一次的话，偶发慢请求就会让整条链路降级成"图谱结构化输出"，
   用户看到的是"有时候有润色、有时候没有"。所以默认重试 2 次。
"""
import json
import time

BASE_PRINT = "[LLM]"


def _cfg():
    import config
    return config


def _post(body, timeout, retries):
    """
    发一次请求（含重试）。返回 (data_dict, err_text)，
    err_text 为空字符串表示成功。
    """
    import requests

    cfg = _cfg()
    conf = cfg.resolve_llm()
    if not conf["ready"]:
        return None, "未配置大模型（缺少接口地址 / API Key / 模型名）"

    base = conf["base_url"].rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": "Bearer %s" % conf["api_key"],
               "Content-Type": "application/json"}

    last = "未知错误"
    for i in range(max(1, retries + 1)):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code != 200:
                last = "HTTP %s：%s" % (r.status_code, r.text[:200])
                # 4xx 里的参数错误重试也没用，直接返回
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    print("WARN: %s %s" % (conf["label"], last))
                    return None, last
            else:
                return r.json(), ""
        except Exception as e:
            last = "%s（%s）" % (type(e).__name__, str(e)[:160])

        if i < retries:
            wait = 1.5 * (i + 1)
            print("WARN: %s 第 %d 次调用失败：%s —— %.1fs 后重试"
                  % (conf["label"], i + 1, last, wait))
            time.sleep(wait)

    print("WARN: %s 调用失败（已重试 %d 次）：%s" % (conf["label"], retries, last))
    return None, last


def _extract(data):
    """从返回体里取正文；正文为空时退而取思维链（总比什么都没有好）"""
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("reasoning_content") or "").strip()
    return text


def chat(system, user, max_tokens=None, temperature=None,
         timeout=None, retries=None):
    """
    纯文本对话。
    返回 (正文 或 None, 错误说明)。成功时错误说明为空串。
    """
    cfg = _cfg()
    conf = cfg.resolve_llm()
    max_tokens = max_tokens or getattr(cfg, "LLM_MAX_TOKENS", 1400)
    temperature = getattr(cfg, "LLM_TEMPERATURE", 0.3) if temperature is None else temperature
    timeout = timeout or getattr(cfg, "LLM_TIMEOUT", 90)
    retries = getattr(cfg, "LLM_RETRIES", 2) if retries is None else retries

    body = {
        "model": conf["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body.update(conf.get("params") or {})       # 豆包在这里被关掉思维链

    data, err = _post(body, timeout, retries)
    if err:
        return None, err
    return _extract(data) or None, ""


def _stream_events(resp, state):
    """
    消费一个流式响应，**逐个 yield `{"type":"delta","text":...}`**，
    结束时 return 一个 `{"type":"end", ...}`（不 yield，交给调用方 yield）。

    只认 OpenAI 兼容协议：`data: {...}` / `data: [DONE]`。
    ⚠ 思维链字段 `reasoning_content` **不往外吐** —— 配置里已经关了豆包的思维链，
      万一服务端还是给了，也不能让农户看到那一大段自言自语。
    """
    import json as _json

    parts = []
    try:
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore").strip() if isinstance(raw, bytes) else str(raw).strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                break
            try:
                obj = _json.loads(data)
            except Exception:
                continue                      # 心跳注释等非 JSON 行，跳过
            ch = (obj.get("choices") or [{}])[0]
            d = ch.get("delta") or {}
            piece = d.get("content")
            if not piece:
                # 少数实现把首帧放在 message 里（非增量），这里一并兼容
                piece = (ch.get("message") or {}).get("content")
            if piece:
                parts.append(piece)
                state["started"] = True
                yield {"type": "delta", "text": piece}
            if ch.get("finish_reason"):
                break
    except Exception as e:
        return {"type": "end", "ok": False,
                "error": "流式读取中断：%s（%s）" % (type(e).__name__, str(e)[:160])}
    return {"type": "end", "ok": True, "text": "".join(parts)}


def chat_stream(system, user, max_tokens=None, temperature=None,
                timeout=None, retries=None, read_timeout=45):
    """
    **流式**对话（生成器），给前端"边生成边显示"用。

    逐个 yield 事件 dict：
      {"type": "delta", "text": "..."}          中间片段（0~N 个）
      {"type": "end", "ok": True,  "text": 全文}  正常结束
      {"type": "end", "ok": False, "error": "..."} 失败

    ⚠ 重试策略和非流式的 `chat()` 不一样：**只有还没吐出任何片段时才重试**。
      一旦开始往外吐字，中途断流就只报失败、绝不重发 —— 否则前端会看到
      "同一段话说两遍"（前半截是第一次的、后半截是第二次从头生成的）。

    ⚠ `read_timeout` 是**两次片段之间的最长间隔**，不是整次生成的上限：
      模型正常吐字时每隔几百毫秒就有数据，卡住 45 秒才算异常。
    """
    cfg = _cfg()
    conf = cfg.resolve_llm()
    if not conf["ready"]:
        yield {"type": "end", "ok": False,
               "error": "未配置大模型（缺少接口地址 / API Key / 模型名）"}
        return

    import requests

    max_tokens = max_tokens or getattr(cfg, "LLM_MAX_TOKENS", 1400)
    temperature = getattr(cfg, "LLM_TEMPERATURE", 0.3) if temperature is None else temperature
    retries = getattr(cfg, "LLM_RETRIES", 2) if retries is None else retries
    connect_timeout = min(15, getattr(cfg, "LLM_TIMEOUT", 90))
    timeout = timeout or (connect_timeout, max(20, read_timeout))

    body = {
        "model": conf["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    body.update(conf.get("params") or {})       # 豆包在这里被关掉思维链

    url = conf["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer %s" % conf["api_key"],
               "Content-Type": "application/json",
               "Accept": "text/event-stream"}

    state = {"started": False}
    last = "未知错误"
    for i in range(max(1, int(retries) + 1)):
        if state["started"]:          # 已经吐过字了，不再重试
            break
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=timeout, stream=True)
            if resp.status_code != 200:
                last = "HTTP %s：%s" % (resp.status_code, (resp.text or "")[:200])
                # 4xx（除 429）是参数/鉴权问题，重试也没用
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    print("WARN: %s %s" % (conf["label"], last))
                    yield {"type": "end", "ok": False, "error": last}
                    return
                raise RuntimeError(last)
            end = yield from _stream_events(resp, state)
            if end.get("ok") and (end.get("text") or "").strip():
                yield end
                return
            # 空内容或断流：没吐过字就重试，吐过字就直接认了
            last = end.get("error") or "模型返回空内容"
            if not state["started"]:
                raise RuntimeError(last)
            yield end
            return
        except Exception as e:
            last = "%s（%s）" % (type(e).__name__, str(e)[:160])
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        if i < retries and not state["started"]:
            wait = 1.5 * (i + 1)
            print("WARN: %s 流式第 %d 次失败：%s —— %.1fs 后重试"
                  % (conf["label"], i + 1, last, wait))
            time.sleep(wait)

    print("WARN: %s 流式调用失败（已重试 %d 次）：%s" % (conf["label"], retries, last))
    yield {"type": "end", "ok": False, "error": last}


def chat_vision(user_text, data_url, system=None, max_tokens=900,
                temperature=0.1, timeout=None, retries=None):
    """
    带一张图片（data URL）的对话，用于拍照识病。
    返回 (正文 或 None, 错误说明)。
    """
    cfg = _cfg()
    conf = cfg.resolve_vision()
    if not conf["ready"]:
        return None, conf["reason"]

    max_tokens = max_tokens or 900
    timeout = timeout or max(getattr(cfg, "LLM_TIMEOUT", 90), 90)
    retries = getattr(cfg, "LLM_RETRIES", 2) if retries is None else retries

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]})
    body = {
        "model": conf["model"],
        "messages": msgs,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body.update(conf.get("params") or {})

    # 视觉模型可能不走主模型（比如单独配了 qwen-vl），这里要按它自己的配置发
    base = conf["base_url"].rstrip("/")
    import requests
    headers = {"Authorization": "Bearer %s" % conf["api_key"],
               "Content-Type": "application/json"}
    last = "未知错误"
    for i in range(max(1, retries + 1)):
        try:
            r = requests.post(base + "/chat/completions", headers=headers,
                              json=body, timeout=timeout)
            if r.status_code != 200:
                last = "视觉模型返回 HTTP %s：%s" % (r.status_code, r.text[:200])
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    print("WARN:", last)
                    return None, last
            else:
                return _extract(r.json()) or None, ""
        except Exception as e:
            last = "视觉模型调用失败：%s（%s）" % (type(e).__name__, str(e)[:150])

        if i < retries:
            wait = 1.5 * (i + 1)
            print("WARN: 视觉模型第 %d 次失败：%s —— %.1fs 后重试" % (i + 1, last, wait))
            time.sleep(wait)

    print("WARN:", last)
    return None, last


def status():
    """当前接入状态（供前端展示，不含密钥）"""
    cfg = _cfg()
    conf = cfg.resolve_llm()
    st = {"provider": conf["provider"], "label": conf["label"],
          "base_url": conf["base_url"], "model": conf["model"],
          "ready": conf["ready"],
          "timeout": getattr(cfg, "LLM_TIMEOUT", 90),
          "retries": getattr(cfg, "LLM_RETRIES", 2)}
    if (conf.get("params") or {}).get("thinking", {}).get("type") == "disabled":
        st["thinking"] = "已关闭（响应更快）"
    return st
