#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地知识库（**第二级**证据源）
==============================
系统检索链路改成三级逐级兜底之后：

    ① 知识图谱  —— Neo4j 结构化查询（规则快答 + 多跳聚合）→ 三元组事实 → 豆包整理话术
    ② 本地知识库 —— 原始语料切块 + bge 向量召回 → 片段 → 豆包整理话术
    ③ 大模型    —— 前两级都没命中 → 豆包直接生成（答案里标注来源）

本模块只负责第 ② 级。

为什么必须跟图谱分开
--------------------
原来 `rag_search` 里那个"向量检索"（`kg_vectors.npz`）是把 Neo4j 的 5693 条关系
编码成向量 —— **它的数据源就是图谱本身**，图谱查不到时，它多半也召回不出新东西，
算不上独立的第二级。这里的语料来自 `raw_data/raw_agri_data.txt`：那是建图谱**之前**
采集的原始农业文本（整段描述），没进过图、也不受图模型 schema 限制，
所以图谱结构性查询落空时，它才有兜住的可能性。

设计要点
--------
* **延迟加载**：嵌入模型只在第一次真正检索时才加载，服务启动不受影响；
* **可传入已有模型**：`rag_search` 启动时已经加载过 bge，这里直接复用，不重复占内存；
* **向量缓存**：块文本 + 向量写进 `output/local_kb_vectors.npz`，
  指纹（文件 mtime / 大小 / 切块参数 / 模型路径）对得上就直接读，秒开；
* **全程降级**：文件缺失、模型加载失败、编码报错……一律返回空列表，
  上层会自然落到第 ③ 级（豆包直接生成），不会把问答搞崩。
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import numpy as np                                          # noqa: E402
from sklearn.metrics.pairwise import cosine_similarity      # noqa: E402

try:
    import config
except Exception:                                           # pragma: no cover
    config = None


def _cfg(name, default):
    return getattr(config, name, default) if config else default


# 原始语料：建图谱前采集的农业文本，一段讲一种作物，天然成块
KB_FILE = _cfg("LOCAL_KB_FILE", os.path.join(BASE, "raw_data", "raw_agri_data.txt"))
VEC_CACHE = _cfg("LOCAL_KB_VEC_CACHE",
                 os.path.join(BASE, "output", "local_kb_vectors.npz"))
EMBED_MODEL_PATH = os.path.join(BASE, "models", _cfg("EMBED_MODEL_DIR", "bge-small-zh-v1.5"))

CHUNK_SIZE = int(_cfg("LOCAL_KB_CHUNK", 260))       # 每块目标字数
CHUNK_OVERLAP = int(_cfg("LOCAL_KB_OVERLAP", 1))    # 相邻块重叠几句，避免切断语义
TOP_K = int(_cfg("LOCAL_KB_TOP_K", 5))
THRESHOLD = float(_cfg("LOCAL_KB_THRESHOLD", 0.40))
ENABLED = bool(_cfg("ENABLE_LOCAL_KB", True))

# 行首序号：「1. 小麦是…」→ 去掉「1. 」
_LEAD = re.compile(r"^\s*\d+\s*[.、]\s*")
# 句子边界（保留标点在句尾）
_SENT_SPLIT = re.compile(r"(?<=[。！？；])")


def load_chunks(path=None, size=None, overlap=None):
    """
    把原始语料切成检索用的块，返回 [(来源行号, 文本), ...]。

    两刀：先按行首序号切成"作物段落"，再把过长的段落按句切成 ~size 字的块
    （相邻块重叠 1 句，防止一句话被拦腰截断后两边都不像人话）。
    """
    path = path or KB_FILE
    size = size or CHUNK_SIZE
    overlap = CHUNK_OVERLAP if overlap is None else overlap
    out = []
    if not path or not os.path.exists(path):
        print("WARN: 本地知识库文件不存在：%s" % path)
        return out

    try:
        raw = open(path, encoding="utf-8", errors="ignore").read()
    except Exception as e:
        print("WARN: 本地知识库读取失败：%s" % e)
        return out

    # 第一刀：按行首序号切段（没有序号时整篇当一段，照样能用）
    blocks, cur, no = [], [], 0
    for line in raw.split("\n"):
        s = line.strip()
        if not s:
            continue
        if _LEAD.match(s) and cur:
            blocks.append((no, _LEAD.sub("", "".join(cur)).strip()))
            cur, no = [s], no + 1
        else:
            cur.append(s)
    if cur:
        blocks.append((no, _LEAD.sub("", "".join(cur)).strip()))

    # 第二刀：长段按句号再切
    for no, b in blocks:
        if not b:
            continue
        if len(b) <= size:
            out.append((no, b))
            continue
        sents = [x for x in _SENT_SPLIT.split(b) if x and x.strip()]
        buf = []
        for s in sents:
            buf.append(s)
            if len("".join(buf)) >= size:
                out.append((no, "".join(buf).strip()))
                buf = buf[-overlap:] if overlap > 0 else []
        if buf:
            out.append((no, "".join(buf).strip()))
    return out


def split_blocks(text, min_len=30):
    """
    把一段长文本切成块，用于算相关度。

    先按 Markdown 标题切（事实包本来就是按实体分块的），
    切不出来再按空行切，最后整段兜底。
    """
    text = text or ""
    bs = [b.strip() for b in re.split(r"\n(?=#{1,3}\s)", text)]
    bs = [b for b in bs if len(b) >= min_len]
    if bs:
        return bs
    bs = [b.strip() for b in re.split(r"\n\s*\n", text) if len(b.strip()) >= min_len]
    return bs or ([text.strip()] if text.strip() else [])


def relevance(query, texts, embed_model=None):
    """
    取 texts 里与 query 最像的那条的余弦相似度（0~1）。

    为什么需要它（这是三级路由能不能真正跑起来的关键）
    ------------------------------------------------
    只要问句里出现**任何一个**实体名，"葡萄园能用无人机授粉吗" 里的"葡萄"就够
    —— 图谱聚合器马上能铺出两三千字的葡萄事实（产区、土壤、病害…），
    于是每一问都"命中图谱"，第 ②③ 级永远触发不了，而且答案会跑偏。

    实测（bge-small-zh，取最相关块）：
        图谱真有答案      0.649 ~ 0.844
        只是蹭到实体名    0.480 ~ 0.560
    分得很开，所以用它当第 ① 级的命中门槛（GRAPH_HIT_THRESHOLD）。
    """
    if not query or not texts:
        return 0.0
    blocks = split_blocks(texts) if isinstance(texts, str) else \
        ([t for t in texts if t and len(t.strip()) >= 30] or list(texts))
    if not blocks:
        return 0.0

    model = embed_model
    if model is None:
        kb = get_kb()
        if not kb.ready():
            return 0.0
        model = kb.embed_model
    try:
        qv = model.encode(query, normalize_embeddings=True)
        bv = model.encode(blocks, normalize_embeddings=True)
        return float(cosine_similarity([qv], bv)[0].max())
    except Exception as e:
        print("WARN: 相关度计算失败：%s" % e)
        return 0.0


class LocalKB:
    """原始语料的向量检索器"""

    def __init__(self, embed_model=None, path=None):
        self.path = path or KB_FILE
        self.embed_model = embed_model
        self.items = []          # [{"text","vec","no"}]
        self._ready = False
        self._err = ""

    # ------------------------------------------------------------ 初始化
    def _ensure_model(self):
        """嵌入模型：优先用外部传入的那个，没有才自己加载（只加载一次）"""
        if self.embed_model is not None:
            return True
        if self._err:
            return False
        try:
            from sentence_transformers import SentenceTransformer
            self.embed_model = SentenceTransformer(EMBED_MODEL_PATH)
            print("INFO: 本地知识库已加载嵌入模型 %s" % os.path.basename(EMBED_MODEL_PATH))
            return True
        except Exception as e:
            self._err = "嵌入模型不可用：%s" % e
            print("WARN: %s —— 本地知识库不可用，将直接由大模型兜底" % self._err)
            return False

    def _build(self):
        """切块 + 编码（带缓存），只跑一次"""
        if self._ready or self._err:
            return
        if not ENABLED:
            self._err = "本地知识库已关闭（ENABLE_LOCAL_KB=False）"
            return
        if not self._ensure_model():
            return

        chunks = load_chunks(self.path)
        if not chunks:
            self._err = "本地知识库没有可用文本"
            return

        texts = ["%s" % t for _no, t in chunks]
        try:
            st = os.stat(self.path)
            fp = "%s|%d|%d|%d|%d|%s" % (os.path.basename(self.path), st.st_mtime_ns,
                                        st.st_size, len(texts), CHUNK_SIZE,
                                        EMBED_MODEL_PATH)
        except Exception:
            fp = "%s|%d|%s" % (os.path.basename(self.path), len(texts), EMBED_MODEL_PATH)

        vecs = self._read_cache(fp)
        if vecs is None:
            try:
                vecs = self.embed_model.encode(texts, batch_size=64,
                                               normalize_embeddings=True,
                                               show_progress_bar=False)
            except Exception as e:
                self._err = "本地知识库编码失败：%s" % e
                print("WARN: %s" % self._err)
                return
            self._write_cache(fp, texts, vecs)

        self.items = [{"text": t, "vec": v, "no": no}
                      for (no, _t), t, v in zip(chunks, texts, vecs)]
        self._ready = True
        print("INFO: 本地知识库就绪，共 %d 个文本块（来源 %s）"
              % (len(self.items), os.path.basename(self.path)))

    def _read_cache(self, fp):
        try:
            d = np.load(VEC_CACHE, allow_pickle=False)
            if str(d["fp"]) == fp and len(d["texts"]) == d["vecs"].shape[0]:
                return d["vecs"]
        except Exception:
            pass
        return None

    def _write_cache(self, fp, texts, vecs):
        try:
            os.makedirs(os.path.dirname(VEC_CACHE), exist_ok=True)
            np.savez_compressed(VEC_CACHE, fp=fp,
                                texts=np.array(texts), vecs=np.asarray(vecs))
            print("INFO: 已写入本地知识库向量缓存 %s" % VEC_CACHE)
        except Exception as e:
            print("WARN: 写本地知识库缓存失败（不影响本次使用）：%s" % e)

    # ------------------------------------------------------------ 对外
    def ready(self):
        self._build()
        return self._ready

    def size(self):
        self._build()
        return len(self.items)

    def search(self, query, top_k=None, threshold=None):
        """
        语义召回。返回 [{"text","score","no"}, ...]（按分数倒序，低于阈值的不给）。

        上层判定"本地知识库命中"只看这个列表是不是空 —— 宁缺毋滥，
        宁可掉到第 ③ 级让豆包明确标注"没有本地依据"，也别拿不相干的片段充数。
        """
        if not self.ready():
            return []
        q = (query or "").strip()
        if not q:
            return []
        top_k = top_k or TOP_K
        threshold = THRESHOLD if threshold is None else threshold

        try:
            qv = self.embed_model.encode(q, normalize_embeddings=True)
        except Exception as e:
            print("WARN: 本地知识库检索失败：%s" % e)
            return []

        mat = np.vstack([it["vec"] for it in self.items])
        sims = cosine_similarity([qv], mat)[0]
        idx = np.argsort(-sims)[:max(top_k * 3, top_k)]
        out = []
        for i in idx:
            s = float(sims[i])
            if s < threshold:
                continue
            it = self.items[int(i)]
            out.append({"text": it["text"], "score": round(s, 3), "no": it["no"]})
            if len(out) >= top_k:
                break
        print("INFO: 本地知识库召回 %d 条（最高 %.3f，阈值 %.2f）"
              % (len(out), (out[0]["score"] if out else 0.0), threshold))
        return out

    @staticmethod
    def format(hits):
        """把召回片段拼成给大模型看的「本地知识库片段」文本"""
        if not hits:
            return ""
        return "\n\n".join("[片段%d] %s" % (i + 1, h["text"]) for i, h in enumerate(hits))


# ---------------------------------------------------------------------------
# 模块级单例：两个问答入口（rag_search / agri_advisor）共用一份，
# 免得各建一次、把 512 维向量在内存里存两份。
# ---------------------------------------------------------------------------
_KB = None


def get_kb(embed_model=None):
    """取单例；第一次调用时可以把已加载的嵌入模型传进来复用"""
    global _KB
    if _KB is None:
        _KB = LocalKB(embed_model=embed_model)
    elif embed_model is not None and _KB.embed_model is None:
        _KB.embed_model = embed_model
    return _KB


def reset():
    """测试/语料更新后强制重建"""
    global _KB
    _KB = None


if __name__ == "__main__":
    kb = get_kb()
    print("文本块数：", kb.size())
    for q in ["玉米大斑病怎么防治", "水稻适宜什么样的土壤", "大棚草莓授粉注意事项"]:
        hits = kb.search(q)
        print("-" * 60)
        print("Q:", q, "→ 命中", len(hits))
        for h in hits:
            print("  [%.3f] %s" % (h["score"], h["text"][:80]))
