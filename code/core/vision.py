#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拍照识别病虫害（DiagnosisEngine）
==================================
上传一张作物照片 → 视觉大模型识别 → **回图谱校验并对齐** → 直接给出防治方案。

为什么不是"调个模型就完事"：
1. 大模型给出的病名常常不规范（"小麦锈病" / "锈病" / "条锈"），
   直接拿去查库会查不到；所以识别结果必须回到知识图谱里做实体对齐，才能接上防治数据。
2. 只有对齐到图谱节点，才能顺带给出危害部位、常发时期、危害等级、
   防治措施（含安全间隔期）—— 这才是"识别完能照着治"。

隐私与成本：
   **原图不落盘**，只在内存中压缩后转 base64 发给模型；默认长边压到 1280px 降低 token 消耗。
   为了让「历史记录」能回看"上次拍的是什么"，会额外存一张**压缩后的小图**
   （`static/uploads/diag/<日期>/`，长边 480、约 40~60KB），库里只存它的 URL（见 save_thumb）。
"""
import base64
import datetime
import io
import json
import os
import re
import sys
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import config                                       # noqa: E402
from core import llm_client                         # noqa: E402

# 识别提示词：要求严格 JSON，且不确定时要说不确定
PROMPT = """你是植物病虫害诊断专家，请仔细观察这张作物照片后判断：

1. 图中的作物是什么（看得出就写，看不出写"不确定"）
2. 是否存在病虫害、杂草或明显缺素症状；如果是，具体是什么（尽量用规范中文名）
3. 判断依据：危害部位、颜色、形状、分布特征等可见证据
4. 置信度：高 / 中 / 低
5. 有没有其他可能（列出 1~3 个备选，按可能性排序）

要求：
- 只依据照片里看得见的信息判断，不要凭空推测；
- 如果照片模糊、不是作物、或看不出明显症状，请如实说明；
- 严格只输出 JSON，不要任何解释或 Markdown 代码块包裹。

命名要求（很重要）：problem 与 alternatives 里只写**规范的中文病虫害名**（例如"小麦条锈病""玉米螟"），
不要加"疑似""可能""高度怀疑"这类修饰词，不要加括号补充说明（写成"小麦锈病（条锈病可能性大）"会查不到图谱数据），
不确定的部分写进 note 里。

JSON 格式：
{"crop": "作物名或不确定",
 "problem": "规范的病虫害中文名，没看出来则填 无",
 "category": "病害|虫害|草害|缺素|药害|健康|无法判断",
 "evidence": "可见症状描述，50字以内",
 "confidence": "高|中|低",
 "alternatives": ["备选1", "备选2"],
 "note": "补充说明或建议"}

补充信息（可选，可能为空）：%s"""


class DiagnosisEngine:
    def __init__(self, advisor):
        """advisor: AgriAdvisor 实例（复用其 Neo4j 连接与防治方案生成能力）"""
        self.advisor = advisor
        self.conn = advisor.conn

    # ==================================================== 状态
    def status(self):
        cfg = config.resolve_vision()
        main = config.resolve_llm()
        return {
            "ready": cfg["ready"],
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "source": cfg["source"],
            "reason": cfg["reason"],
            "main_model": main["model"],
            "max_upload_mb": getattr(config, "VISION_MAX_UPLOAD_MB", 8),
        }

    # ==================================================== 图片处理
    @staticmethod
    def to_data_url(raw, max_edge=None, quality=None):
        """压缩并转 data URL；返回 (data_url, (w,h), (orig_w,orig_h))"""
        from PIL import Image, ImageOps
        max_edge = max_edge or getattr(config, "VISION_MAX_EDGE", 1280)
        quality = quality or getattr(config, "VISION_JPEG_QUALITY", 85)
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)          # 手机竖拍照片的旋转修正
        orig = im.size
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + b64, im.size, orig

    # ==================================================== 模型调用
    def _call_vision(self, data_url, hint=""):
        """统一走 core/llm_client.py（当前为豆包·火山方舟，自带超时与重试）"""
        text, err = llm_client.chat_vision(PROMPT % (hint or "无"), data_url)
        if err:
            return None, err
        return text, ""

    # ==================================================== 解析与对齐
    @staticmethod
    def parse_json(text):
        """模型有时会包 ```json 或加解释文字，这里做容错提取"""
        if not text:
            return {}
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        raw = m.group(0)
        for candidate in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
            try:
                return json.loads(candidate)
            except Exception:
                continue
        return {}

    @staticmethod
    def _score(a, b):
        """名称相似度打分：完全一致 > 包含 > 最长公共子串"""
        a, b = str(a or "").strip(), str(b or "").strip()
        if not a or not b:
            return 0
        if a == b:
            return 100
        if a in b or b in a:
            return 60 + min(len(a), len(b))
        best = 0
        for n in range(min(len(a), len(b)), 1, -1):
            for i in range(len(a) - n + 1):
                if a[i:i + n] in b:
                    best = max(best, n * 8)
                    break
            if best:
                break
        return best

    @staticmethod
    def _variants(name):
        """模型给出的病名可能仍带修饰，这里逐级收敛出几个干净的查询变体。

        "疑似小麦锈病（条锈病可能性大）" → 去括号 → 去"疑似" → 去"病" → "小麦锈"
        """
        out, seen = [], set()

        def add(s):
            s = re.sub(r"\s+", "", str(s or "")).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)

        add(name)
        for base in list(out):                       # 去掉括号里的补充说明
            add(re.sub(r"[（(].*?[)）]", "", base))
        for base in list(out):                       # 去掉"疑似/可能"这类不确定修饰
            for w in ("疑似", "可能为", "可能是", "高度怀疑", "考虑为", "不排除", "大概"):
                if w in base:
                    add(base.replace(w, ""))
        for base in list(out):                       # 去掉类别后缀，缩小到核心词
            if len(base) > 3 and base.endswith("病"):
                add(base[:-1])
            if len(base) > 3 and base.endswith("虫害"):
                add(base[:-2])
        for base in list(out):                       # 截断到前 3 字（"小麦条锈病"→"小麦条"）
            if len(base) >= 3:
                add(base[:3])
        return out

    def align(self, name, threshold=32):
        """
        把模型给出的病名对齐到图谱节点（这是能接上防治数据的前提）
        返回 (最佳匹配 dict 或 None, 候选列表)
        """
        name = str(name or "").strip()
        if not name or name in ("无", "不确定", "健康", "无法判断"):
            return None, []
        vs = self._variants(name)
        pool, seen = [], set()
        for q in vs:
            for r in self.conn.search_entities(q, limit=12):
                if r["name"] not in seen:
                    seen.add(r["name"])
                    pool.append(r)

        def best_score(node):                        # 取任一变体与该节点的最高分
            return max(self._score(v, node) for v in vs)

        ranked = sorted(pool, key=lambda x: -best_score(x["name"]))
        cands = [{"name": r["name"], "label": r["label"],
                  "score": best_score(r["name"])} for r in ranked[:6]]
        best = ranked[0] if ranked and best_score(ranked[0]["name"]) >= threshold else None
        return best, cands

    # ==================================================== 主入口
    def diagnose(self, image_bytes=None, filename="", crop_hint="", description=""):
        """
        拍照识别主流程
        image_bytes: 图片二进制；不传则退回「文字描述症状」模式
        返回统一结构，前端据此渲染
        """
        out = {"ok": False, "mode": "", "vision_used": False,
               "vision_model": "", "raw": {}, "matched": None, "candidates": [],
               "advice": "", "sources": [], "crop": crop_hint or "",
               "image_size": None, "message": "", "disclaimer":
                   "识别结果由 AI 根据照片推断，仅供田间参考；重大病虫害请以当地植保部门鉴定为准。"}

        cfg = config.resolve_vision()

        # ---------- 无图：走文字描述（复用顾问引擎，任何配置下都能用） ----------
        if not image_bytes:
            text = (description or "").strip()
            if not text:
                out["message"] = "请上传照片，或填写症状描述"
                return out
            out["mode"] = "text"
            res = self.advisor.advise(text, mode="pest")
            out.update(ok=True, advice=res.get("answer", ""),
                       sources=res.get("sources") or [],
                       message="已按文字描述匹配图谱知识")
            return out

        # ---------- 有图但没配视觉模型：明确告知，不假装成功 ----------
        if not cfg["ready"]:
            out["mode"] = "image"
            out["message"] = cfg["reason"]
            out["need_vision"] = True
            return out

        # ---------- 有图且有视觉模型 ----------
        out["mode"] = "image"
        out["vision_model"] = cfg["model"]
        try:
            data_url, size, orig = self.to_data_url(image_bytes)
        except Exception as e:
            out["message"] = "图片无法解析（支持 JPG/PNG/WEBP）：%s" % e
            return out
        out["image_size"] = {"width": size[0], "height": size[1],
                             "original": {"width": orig[0], "height": orig[1]}}

        hint = ""
        if crop_hint:
            hint = "农户说这块地种的是「%s」。" % crop_hint
        if description:
            hint += "农户补充描述：%s" % description

        text, err = self._call_vision(data_url, hint)
        if text is None:
            out["message"] = err or "视觉模型调用失败"
            return out
        out["vision_used"] = True

        parsed = self.parse_json(text)
        if not parsed:
            out["ok"] = True
            out["raw"] = {"text": text}
            out["message"] = ("模型返回的不是标准格式，已原样展示，"
                              "建议换个角度重拍或补充描述")
            return out
        out["raw"] = parsed
        if not out["crop"]:
            out["crop"] = (parsed.get("crop") or "").strip()

        # 健康 / 看不出问题：不做图谱查询
        category = (parsed.get("category") or "").strip()
        problem = (parsed.get("problem") or "").strip()
        if category in ("健康",) or problem in ("无", "", "健康"):
            out["ok"] = True
            out["message"] = "照片中未见明显病虫害特征"
            return out

        # ---------- 图谱对齐 ----------
        best, cands = self.align(problem)
        out["candidates"] = cands
        if not best:
            out["ok"] = True
            out["message"] = ("模型判断为「%s」，但图谱中没有找到对应条目，"
                              "下面是模型的原始判断，可换个名字再查一次" % problem)
            return out

        out["matched"] = {"name": best["name"], "label": best["label"],
                          "score": self._score(problem, best["name"])}

        # ---------- 用图谱出防治方案 ----------
        crop = out["crop"] if out["crop"] and out["crop"] != "不确定" else None
        advice, sources = self.advisor.pest_advice(crop=crop, pest=best["name"])
        if not advice and not crop:
            advice, sources = self.advisor.pest_advice(pest=best["name"])
        if not advice:
            advice, sources = self.advisor.pest_advice(crop=crop)
        out["advice"] = advice or ""
        out["sources"] = sources or []
        out["ok"] = True
        out["message"] = "识别完成"
        return out


# ======================================================================
# 缩略图：给「历史记录」回看用
#   「拍照识病也要留痕」——但记录里只有一串文字的话，农户根本想不起当时拍的是什么。
#   所以存一张**压缩后的小图**（长边 480、约 40~60KB），库里只存 URL。
#   原图依旧不落盘，隐私与体积都可控；删历史记录时这张小图也会一起删掉。
# ======================================================================
STATIC_DIR = os.path.join(BASE, "static")
_UPLOAD_ROOT = os.path.join(STATIC_DIR, "uploads")
THUMB_EDGE = 480
THUMB_QUALITY = 72


def save_thumb(raw, max_edge=None, quality=None):
    """
    把上传的图片压成缩略图存盘，返回可直接放进 <img src> 的 URL；
    没图或保存失败一律返回 ""（历史记录照记，只是少一张图，绝不影响识别结果）。
    """
    if not raw:
        return ""
    try:
        from PIL import Image, ImageOps
        day = datetime.datetime.now().strftime("%Y%m%d")
        folder = os.path.join(_UPLOAD_ROOT, "diag", day)
        os.makedirs(folder, exist_ok=True)
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im)          # 手机竖拍的旋转修正，和送模型那份保持一致
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        edge = max_edge or THUMB_EDGE
        im.thumbnail((edge, edge), Image.LANCZOS)
        name = uuid.uuid4().hex[:12] + ".jpg"     # 随机文件名：不把用户输入拼进路径
        im.save(os.path.join(folder, name), "JPEG",
                quality=quality or THUMB_QUALITY, optimize=True)
        return "/static/uploads/diag/%s/%s" % (day, name)
    except Exception as e:
        print("WARN: 缩略图保存失败（历史记录将不带图）：%s" % e)
        return ""


def remove_thumb(url):
    """
    删除缩略图文件（删单条历史 / 清空历史时调用）。
    只允许删 `static/uploads/` 里面的文件 —— 库里的值万一被改过，
    也不能顺着 `../` 删到项目别的地方去。
    """
    if not url:
        return False
    try:
        rel = str(url)
        if rel.startswith("/static/"):
            rel = rel[len("/static/"):]
        path = os.path.realpath(os.path.join(STATIC_DIR, rel))
        root = os.path.realpath(_UPLOAD_ROOT)
        if not path.startswith(root + os.sep):
            print("WARN: 拒绝删除 uploads 目录之外的文件：%s" % url)
            return False
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception as e:
        print("WARN: 缩略图删除失败（可忽略）：%s" % e)
    return False


if __name__ == "__main__":
    from core.neo4j_conn import Neo4jConnection
    from core.agri_advisor import AgriAdvisor

    print("视觉模型状态：", json.dumps(config.resolve_vision(), ensure_ascii=False))
    adv = AgriAdvisor(config.NEO4J_URI, config.NEO4J_USER, config.NEO4J_PWD)
    eng = DiagnosisEngine(adv)

    # 对齐能力自测：模拟模型输出的各种不规范病名
    for name in ["小麦条锈病", "条锈", "锈病", "稻瘟", "玉米大斑病", "蚜虫",
                 "红蜘蛛", "看麦娘", "完全不存在的东西"]:
        best, cands = eng.align(name)
        print("对齐 %-14s -> %s" % (name, best["name"] if best else "未匹配"),
              "| 候选：", "、".join(c["name"] for c in cands[:3]))

    # 文字模式
    r = eng.diagnose(description="玉米叶子上有黄色长条斑，边缘枯焦")
    print("-" * 60)
    print("文字模式：", r["message"], "| 溯源", len(r["sources"]), "条")
    adv.close()
