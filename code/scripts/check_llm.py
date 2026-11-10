#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大模型接入自检（check_llm.py）
================================
一条命令看清：现在用的是哪个模型、通不通、这个 Key 还能用哪些模型。

用法（在 code 目录下执行）：
    python scripts/check_llm.py              # 当前配置 + 连通性测试
    python scripts/check_llm.py --models     # 额外列出这个 Key 可用的模型清单
    python scripts/check_llm.py --vision     # 额外测一下模型能不能看图（拍照识病用）
    python scripts/check_llm.py --all        # 全做

为什么要 --models：
    方舟/各家平台的模型列表里混着大量**已下线**的模型，直接抄名字会得到
    "ModelNotOpen / NotFound" 这类报错。这个命令会把已下线的过滤掉，
    并标出哪些是**你的账号真能调**的（逐个发一次极小请求实测）。
"""
import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                       # noqa: E402


def _req(url, key, body=None, method="GET", timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            j = json.loads(raw)
            return e.code, j.get("error") or j
        except ValueError:
            return e.code, {"raw": raw[:200]}
    except Exception as e:
        return "ERR", {"raw": str(e)}


def show_config():
    cfg = config.resolve_llm()
    vis = config.resolve_vision()
    print("=" * 66)
    print("当前生效的模型配置")
    print("=" * 66)
    print("  提供方    :", cfg["provider"], "—", cfg["label"])
    print("  接口地址  :", cfg["base_url"] or "(未配置)")
    print("  模型名称  :", cfg["model"] or "(未配置)")
    print("  API Key   :", ("已配置 %s***" % cfg["api_key"][:8]) if cfg["api_key"] else "(未配置)")
    print("  可用状态  :", "✓ 可用" if cfg["ready"] else "✗ 不可用（将退回图谱结构化输出）")
    print("-" * 66)
    print("  视觉模型  :", vis["model"] or "(未配置)", "" if vis["ready"] else "→ 拍照识病不可用")
    if vis["ready"]:
        print("  来源      :", "复用主模型" if vis["source"] == "main" else "专用配置")
    else:
        print("  说明      :", vis["reason"])
    return cfg


def test_chat(cfg):
    print()
    print("=" * 66)
    print("连通性测试（发一句“你好”）")
    print("=" * 66)
    if not cfg["ready"]:
        print("  跳过：配置不完整")
        return
    s, d = _req(cfg["base_url"] + "/chat/completions", cfg["api_key"],
                {"model": cfg["model"],
                 "messages": [{"role": "user", "content": "你好，请回复四个字：连接正常"}],
                 "max_tokens": 32, "temperature": 0}, method="POST", timeout=90)
    if s == 200:
        reply = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = d.get("usage") or {}
        print("  ✓ 成功：", reply.strip()[:60])
        if usage:
            print("    tokens：输入 %s / 输出 %s" % (usage.get("prompt_tokens"), usage.get("completion_tokens")))
    else:
        print("  ✗ 失败 [%s]：%s" % (s, json.dumps(d, ensure_ascii=False)[:220]))


def list_models(cfg, probe=True, limit=8):
    print()
    print("=" * 66)
    print("模型清单（%s）" % (cfg["base_url"] or "-"))
    print("=" * 66)
    s, d = _req(cfg["base_url"] + "/models", cfg["api_key"], timeout=40)
    if s != 200 or not isinstance(d, dict) or "data" not in d:
        print("  该平台不支持列出模型，或调用失败 [%s]：%s" % (s, json.dumps(d, ensure_ascii=False)[:200]))
        return
    models = d["data"]
    live = [m for m in models if str(m.get("status") or "").lower() != "shutdown"]
    dead = len(models) - len(live)
    print("  共 %d 个，其中 %d 个已下线（已滤掉）" % (len(models), dead))
    print("  下列为未下线的：")
    for m in live:
        print("    %-46s %s" % (m["id"], m.get("status") or ""))
    if not probe:
        return
    print()
    print("  逐个实测能否调用（只发 max_tokens=8 的极小请求，选出你的账号真能用的）…")
    usable = []
    for m in live[:limit * 3]:
        s2, d2 = _req(cfg["base_url"] + "/chat/completions", cfg["api_key"],
                      {"model": m["id"], "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 8}, method="POST", timeout=40)
        if s2 == 200:
            usable.append(m["id"])
            print("    ✓ %s" % m["id"])
        else:
            msg = json.dumps(d2, ensure_ascii=False)
            tag = "未开通" if "ModelNotOpen" in msg else ("不存在/无权限" if "NotFound" in msg else str(s2))
            print("    ✗ %-44s %s" % (m["id"], tag))
        if len(usable) >= limit:
            print("    （已找到 %d 个可用，停止探测）" % limit)
            break
    print()
    print("  >>> 你的账号实测可用：", ", ".join(usable) if usable else "无")
    if usable:
        print("  >>> 想改用其中某个，把 config_local.py 的 LLM_MODEL 改成它即可")


def test_vision(cfg):
    print()
    print("=" * 66)
    print("视觉能力测试（造一张红蓝图，看模型能不能读出来）")
    print("=" * 66)
    vis = config.resolve_vision()
    if not vis["ready"]:
        print("  跳过：未配置视觉模型 ——", vis["reason"])
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  跳过：未安装 Pillow（pip install pillow）")
        return
    im = Image.new("RGB", (200, 100), (220, 40, 40))
    ImageDraw.Draw(im).rectangle([100, 0, 200, 100], fill=(40, 60, 200))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    s, d = _req(vis["base_url"] + "/chat/completions", vis["api_key"], {
        "model": vis["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图有哪几种颜色？一句话回答。"},
            {"type": "image_url", "image_url": {"url": url}}]}],
        "max_tokens": 60, "temperature": 0,
    }, method="POST", timeout=90)
    if s == 200:
        reply = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        ok = "红" in reply and "蓝" in reply
        print("  %s %s" % ("✓ 能看图：" if ok else "△ 返回了内容但没读对：", reply.strip()[:90]))
    else:
        print("  ✗ 失败 [%s]：%s" % (s, json.dumps(d, ensure_ascii=False)[:220]))


def main():
    args = sys.argv[1:]
    do_all = "--all" in args
    cfg = show_config()
    test_chat(cfg)
    if do_all or "--models" in args:
        list_models(cfg, probe=True)
    if do_all or "--vision" in args:
        test_vision(cfg)
    print()


if __name__ == "__main__":
    main()
