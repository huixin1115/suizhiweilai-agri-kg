#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os
import torch
import re
import numpy as np
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForCausalLM, pipeline
from sklearn.metrics.pairwise import cosine_similarity

# 路径配置：由当前位置动态推导，换目录 / 换机器都不用改
current_file_path = os.path.abspath(__file__)
core_folder = os.path.dirname(current_file_path)
project_root = os.path.dirname(core_folder)      # core/ 的上一级就是 code/
sys.path.insert(0, project_root)

# 本地模型路径
EMBED_MODEL_PATH = os.path.join(project_root, "models", "bge-small-zh-v1.5")
LLM_MODEL_PATH = os.path.join(project_root, "models", "Qwen3.5-0.5B-Chat")

# 知识库向量缓存（首次生成约几十秒，之后秒开；图谱变了会自动失效重算）
VEC_CACHE = os.path.join(project_root, "output", "kg_vectors.npz")

# 设备配置
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# 导入你原有的Neo4j连接类
from core.neo4j_conn import Neo4jConnection

# 大模型统一走 config 里的 OpenAI 兼容配置（当前接入豆包·火山方舟），
# 由 core/llm_client.py 负责实际调用。
# 配置了 API 就用 API 润色，没配置才退回本地小模型，两者都没有就只出图谱事实。
try:
    import config
    from core import llm_client
    from core import answer_filler
except Exception:                                    # pragma: no cover
    config = None
    llm_client = None
    answer_filler = None

class AgricultureRAG:
    """
    图谱增强 RAG 问答（基础模式）

    一次提问按**三级逐级兜底**取证据，拿到证据后再交给大模型组织语言：

        ① 知识图谱    —— 规则快答 + 多跳聚合（knowledge_builder）把相关关系一次铺开，
                        命中就把图谱事实整理成三元组清单，交给豆包整理话术
        ② 本地知识库  —— 图谱查不到时，到 raw_data 原始语料库做 bge 向量召回，
                        命中就把片段交给豆包整理话术
        ③ 大模型直出  —— 前两级都没有，豆包依据通用农技常识直接生成，并在答案里标注来源

    ⚠ 这里是**逐级兜底**，不是三路并行合并：命中哪一级就只用哪一级的证据，
      不会再拼一份"什么都有一点"的事实包 —— 那样既说不清答案出自哪里，
      也让"图谱里没有"这件事被悄悄掩盖掉。

    第 ① 级之所以要走两步：规则快答准但覆盖窄，多跳聚合宽但偏散，
    两步合起来才既准又全（防治措施类型/剂型/安全间隔期、栽培要点时期与作用、
    生育期各阶段农事、产区土壤气候）。
    """

    def __init__(self, neo4j_uri, neo4j_user, neo4j_pwd):
        # 1. 完全复用你原有的Neo4j连接和实体提取逻辑
        self.neo4j_conn = Neo4jConnection(neo4j_uri, neo4j_user, neo4j_pwd)
        self.all_entities = self._load_all_entities()
        self.aliases = self._load_alias_map()
        print(f"INFO: 预加载了 {len(self.all_entities)} 个实体、{len(self.aliases)} 个别名")

        # 2. 可选加载大模型（默认开启，失败自动降级）
        self.enable_llm = True
        self.embed_model = None
        self.llm_tokenizer = None
        self.llm_pipeline = None
        self._load_models()

        # 3. 本地知识库（第二级）：把已加载的 bge 传进去复用，不重复加载模型。
        #    它只在图谱查空时才真正建索引，所以这里登记一下即可，不影响启动速度。
        try:
            from core import local_kb
            self.local_kb = local_kb.get_kb(embed_model=self.embed_model)
        except Exception as e:                              # pragma: no cover
            print(f"WARN: 本地知识库不可用（将直接由大模型兜底）: {e}")
            self.local_kb = None

        # 图谱三元组的向量副本：三级路由落地后它**不再参与主链路** ——
        # 数据源就是图谱自己，图谱查空时它也召不回新东西，留着只会让答案来源说不清。
        # 属性与方法保留，仅为兼容旧调用（默认不再预加载，省去几十秒启动耗时）。
        self.knowledge_vectors = []

    def _load_all_entities(self):
        """完全复用你原有的实体加载逻辑"""
        try:
            with self.neo4j_conn.driver.session() as session:
                result = session.run("""
                    MATCH (n)
                    WHERE n.name IS NOT NULL
                    RETURN labels(n)[0] as label, n.name as name
                """)
                entities = [(record["name"], record["label"]) for record in result]
                return sorted(entities, key=lambda x: len(x[0]), reverse=True)
        except Exception as e:
            print(f"ERROR: 加载实体失败: {e}")
            return []

    def _load_alias_map(self):
        """
        别名 → (正式名, 标签)。

        图谱里 52 种作物带 `别名` 属性（土豆→马铃薯、西红柿→番茄、苞谷→玉米…），
        农户提问用的多是土叫法，只按正式名匹配会一条都命中不了，
        所以把别名也接进实体识别。
        """
        try:
            with self.neo4j_conn.driver.session() as session:
                result = session.run("""
                    MATCH (n) WHERE n.别名 IS NOT NULL
                    RETURN n.name AS name, labels(n)[0] AS label, n.别名 AS alias
                """)
                out = {}
                for record in result:
                    for a in str(record["alias"] or "").split("|"):
                        a = re.sub(r"[（(].*?[)）]", "", a).strip()
                        # 单字别名（棉、蒜、姜…）太容易误伤，只收 2 字以上的
                        if len(a) >= 2 and a != record["name"] and a not in out:
                            out[a] = (record["name"], record["label"])
                return out
        except Exception as e:
            print(f"WARN: 别名加载失败（不影响其它功能）: {e}")
            return {}

    def _load_models(self):
        """加载嵌入模型（必需）与生成模型（优先走大模型 API，未配置才加载本地小模型）"""
        # 1. 嵌入模型：向量检索必需，始终加载
        try:
            self.embed_model = SentenceTransformer(EMBED_MODEL_PATH, device=DEVICE)
            print("INFO: 嵌入模型加载成功")
        except Exception as e:
            print(f"WARN: 嵌入模型加载失败，将退化为纯规则检索: {e}")

        # 2. 生成模型：配置了大模型 API 就走 API（更快、不占内存、启动也快），
        #    没配置才按需加载本地 Qwen 兜底
        if config is not None and config.llm_ready():
            cfg = config.resolve_llm()
            print(f"INFO: 已配置大模型（{cfg['label']} / {cfg['model']}），跳过本地小模型加载")
            return
        if not getattr(config, "ENABLE_LOCAL_LLM", False):
            print("INFO: 未配置大模型 API 且未启用本地小模型，问答将只输出图谱结构化结果")
            self.enable_llm = False
            return

        try:
            self.llm_tokenizer = AutoTokenizer.from_pretrained(
                LLM_MODEL_PATH, trust_remote_code=True, padding_side="right"
            )
            llm_model = AutoModelForCausalLM.from_pretrained(
                LLM_MODEL_PATH,
                trust_remote_code=True,
                device_map="auto",
                dtype=torch.float16 if torch.cuda.is_available() else torch.float32
            )
            self.llm_pipeline = pipeline(
                "text-generation",
                model=llm_model,
                tokenizer=self.llm_tokenizer,
                device=0 if torch.cuda.is_available() else -1
            )
            print("INFO: 本地小模型加载成功")
        except Exception as e:
            print(f"WARN: 本地小模型加载失败，将使用纯规则检索: {e}")
            self.enable_llm = False

    def _get_text_embedding(self, text):
        """生成文本向量（bge-small-zh-v1.5 输出 512 维）"""
        if not self.embed_model:
            return np.zeros(512)
        text = (text or "").strip()
        if not text:
            return np.zeros(512)
        return self.embed_model.encode(text, normalize_embeddings=True)

    def _preload_knowledge_vectors(self):
        """
        预加载三元组为向量。
        5693 条在 CPU 上逐条编码要 40 多秒，页面第一次打开会像卡死，
        所以这里做了两件事：
          1. 批量编码（batch_size=64），比逐条快数倍
          2. 结果缓存到 output/kg_vectors.npz，指纹对得上就直接读，秒开
        """
        texts = []
        try:
            with self.neo4j_conn.driver.session() as session:
                result = session.run("""
                    MATCH (s)-[r]->(o)
                    WHERE s.name IS NOT NULL AND o.name IS NOT NULL
                    RETURN s.name as subject, coalesce(r.type, type(r)) as rel, o.name as object
                """)
                for record in result:
                    texts.append("%s的%s是%s"
                                 % (record["subject"], record["rel"], record["object"]))
        except Exception as e:
            print(f"ERROR: 读取图谱三元组失败: {e}")
            return []
        if not texts or self.embed_model is None:
            return []

        # 缓存指纹：条数 + 首尾文本 + 嵌入模型路径（换模型或图谱变了就失效重算）
        fp = "%d|%s|%s|%s" % (len(texts), texts[0], texts[-1], EMBED_MODEL_PATH)

        cached = self._read_vec_cache(fp)
        if cached is not None:
            print("INFO: 命中知识向量缓存，共 %d 条" % len(cached))
            return cached

        try:
            vecs = self.embed_model.encode(texts, batch_size=64,
                                           normalize_embeddings=True,
                                           show_progress_bar=False)
        except Exception as e:
            print(f"ERROR: 预加载知识库向量失败: {e}")
            return []

        self._write_vec_cache(fp, texts, vecs)
        return [{"text": t, "vector": v} for t, v in zip(texts, vecs)]

    def _read_vec_cache(self, fp):
        """读向量缓存；指纹不符或文件损坏都返回 None（走现算）"""
        try:
            d = np.load(VEC_CACHE, allow_pickle=False)
            if str(d["fp"]) == fp:
                txts = [str(t) for t in d["texts"]]
                vecs = d["vecs"]
                return [{"text": t, "vector": v} for t, v in zip(txts, vecs)]
        except Exception:
            pass
        return None

    def _write_vec_cache(self, fp, texts, vecs):
        try:
            os.makedirs(os.path.dirname(VEC_CACHE), exist_ok=True)
            np.savez_compressed(VEC_CACHE, fp=fp,
                                texts=np.array(texts), vecs=np.asarray(vecs))
            print("INFO: 已写入知识向量缓存 %s" % VEC_CACHE)
        except Exception as e:
            print(f"WARN: 写向量缓存失败（不影响本次使用）: {e}")

    def _extract_entities(self, query):
        """完全复用你原有的实体提取逻辑（补上杂草与"不带病/虫字"的病虫害）"""
        entities = {
            "crop": None,
            "region": None,
            "soil": None,
            "climate": None,
            "fertilizer": None,
            "disease": None,
            "pest": None,
            "weed": None
        }
        print(f"INFO: 正在从 '{query}' 中提取实体...")
        pest_hits = []          # 已命中的病虫害名（同一件事的长短节点只留更具体的）
        for name, label in self.all_entities:
            if name in query:
                print(f"INFO: 匹配到实体: {name} (标签: {label})")
                if label == "作物" and not entities["crop"]:
                    entities["crop"] = name
                elif label == "地域" and not entities["region"]:
                    entities["region"] = name
                elif label == "土壤" and not entities["soil"]:
                    entities["soil"] = name
                elif label == "气候" and not entities["climate"]:
                    entities["climate"] = name
                elif label == "农药肥料" and not entities["fertilizer"]:
                    entities["fertilizer"] = name
                elif label == "杂草" and not entities["weed"]:
                    entities["weed"] = name
                elif label == "病虫害":
                    # 「锈病」和「小麦条锈病」是同一种病的两个节点，entities 按长度倒序，
                    # 先命中的更具体，短的丢掉即可
                    if any(name in h or h in name for h in pest_hits):
                        continue
                    pest_hits.append(name)
                    # 图谱里有「小麦红蜘蛛」这种名字不带"虫"字的虫害，
                    # 所以不能只靠关键字判断：带"病"字的记成病害，其余都记成虫害
                    if "病" in name and not entities["disease"]:
                        entities["disease"] = name
                    elif not entities["pest"]:
                        entities["pest"] = name
        # 地域兜底（复用你原有的逻辑）
        if not entities["region"]:
            for name, label in self.all_entities:
                if label == "地域" and any(kw in name for kw in ["河南", "山东", "湖北"]):
                    if name[:2] in query:
                        entities["region"] = name
                        break
        # 别名兜底：农户多说土叫法（土豆、西红柿、苞谷），别名也认
        for alias in sorted(getattr(self, "aliases", {}), key=len, reverse=True):
            if alias in query:
                real, label = self.aliases[alias]
                if label == "作物" and not entities["crop"]:
                    entities["crop"] = real
                elif label == "农药肥料" and not entities["fertilizer"]:
                    entities["fertilizer"] = real
        print(f"INFO: 提取结果: {entities}")
        return entities

    def _retrieve_relevant_knowledge(self, query, top_k=5):
        """向量检索相关知识"""
        if not self.embed_model:
            return []
        query_vec = self._get_text_embedding(query)
        similarities = []
        for item in self.knowledge_vectors:
            sim = cosine_similarity([query_vec], [item["vector"]])[0][0]
            similarities.append((item, sim))
        similarities.sort(key=lambda x: x[1], reverse=True)
        relevant = [item["text"] for item, sim in similarities[:top_k] if sim > 0.3]
        print(f"INFO: 检索到 {len(relevant)} 条相关知识")
        return relevant

    def _llm_api(self, system, user, max_tokens=400):
        """
        调用大模型（统一走 core/llm_client.py，当前接入豆包·火山方舟）
        llm_client 内部已处理：附加参数（豆包关思维链）、超时与重试。
        """
        text, err = llm_client.chat(system, user, max_tokens=max_tokens)
        return text

    # ---- 空小节补全（图谱没覆盖到的条目，让大模型用通用农技知识补） ----
    def _supplement(self, answer, question, facts):
        """答案里有「图谱暂无此项数据」的小节时，再补一次；任何失败都原样返回"""
        if not answer_filler or not answer:
            return answer
        return answer_filler.fill(
            answer, question, facts, llm_client.chat,
            max_tokens=int(getattr(config, "SUPPLEMENT_MAX_TOKENS", 900)),
            enabled=bool(getattr(config, "ENABLE_LLM_SUPPLEMENT", True)))

    # ---------------------------------------------------------------- 规则快答
    def _rule_answer(self, user_query, entities):
        """
        精确规则查询：问句结构清楚时（「X 的 Y 怎么防治」）直接按关系名取，
        命中就跟图谱一一对应，比向量检索更准。命中不了返回空串，交给下面的兜底。
        """
        crop = entities["crop"]
        region = entities["region"]
        disease = entities["disease"]
        pest = entities["pest"]
        weed = entities.get("weed")

        if crop and disease and any(kw in user_query for kw in ["防治", "如何防治", "怎么治", "怎么防"]):
            # 注意：不能带 target_label —— 防治措施的落点是「农药肥料」和「栽培要点」混合的，
            # 传 "病虫害" 会一条都查不到（这是原来查不出结果的原因）
            methods = self.neo4j_conn.query_triple(disease, "防治方法")
            if methods:
                return f"{crop} 的{disease}防治方法：\n- " + "\n- ".join(sorted(set(methods)))
            return f"暂无{disease}的防治方法数据"
        if region and any(kw in user_query for kw in ["种", "产", "适合", "种植"]):
            # 原写法用了不存在的 self.neo4j_conn.session，且标签/关系名都不对，已改为按
            # 本项目的图模型查：关系统一是 :REL + r.type='主产区'
            rows = self.neo4j_conn._run(
                "MATCH (c)-[r:REL {type:'主产区'}]->(g {name:$name}) RETURN c.name AS n",
                name=region)
            crops = [x["n"] for x in rows if x.get("n")]
            return (f"{region} 适合种植：\n- " + "\n- ".join(sorted(set(crops)))
                    if crops else f"暂无{region}种植数据")
        if crop and any(kw in user_query for kw in ["主产区", "产地", "哪里种"]):
            areas = self.neo4j_conn.query_triple(crop, "主产区", "地域")
            return f"{crop} 主产区：\n" + "\n- ".join(list(set(areas))) if areas else f"暂无{crop}主产区数据"
        if crop and any(kw in user_query for kw in ["病", "病害"]):
            diseases = self.neo4j_conn.query_triple(crop, "易感病害")
            return f"{crop} 易感病害：\n" + "\n- ".join(list(set(diseases))) if diseases else f"暂无{crop}病害数据"
        if (disease or pest or weed) and any(
                kw in user_query for kw in ["防治", "怎么治", "药剂", "怎么除", "除草", "打药"]):
            target = disease or pest or weed
            methods = self.neo4j_conn.query_triple(target, "防治方法")
            return f"{target} 防治方法：\n" + "\n- ".join(list(set(methods))) if methods else f"暂无{target}防治数据"
        if crop and any(kw in user_query for kw in ["土壤", "适配土壤"]):
            soils = self.neo4j_conn.query_triple(crop, "适宜土壤")
            return f"{crop} 适宜土壤：\n" + "\n- ".join(list(set(soils))) if soils else f"暂无{crop}土壤数据"
        if crop and any(kw in user_query for kw in ["种植", "怎么种", "栽培", "栽培要点", "怎么栽培"]):
            points = self.neo4j_conn.query_triple(crop, "栽培要点")
            return f"{crop} 的栽培种植要点：\n" + "\n- ".join(list(set(points))) if points else f"暂无{crop}的栽培种植数据"
        return ""

    # ---------------------------------------------------------------- 三级路由
    # 路由值：graph（知识图谱命中）｜local_kb（本地知识库命中）｜llm（前两级都没有）
    ROUTE_NAME = {"graph": "知识图谱", "local_kb": "本地知识库", "llm": "大模型生成"}
    # 第 ① 级命中门槛：图谱铺出来的事实跟问题的相关度低于它，就当图谱"没有"
    GRAPH_HIT_THRESHOLD = float(getattr(config, "GRAPH_HIT_THRESHOLD", 0.60)) \
        if config else 0.60

    def _graph_facts(self, user_query, entities):
        """
        第 ① 级：知识图谱。

          1. 规则快答     —— 问句结构清楚时按关系名精确取，命中即准
          2. 多跳图谱聚合 —— knowledge_builder 顺着关系再挖一层细节
                            （防治措施的类型/剂型/安全间隔期、栽培要点的时期与作用、
                             生育期各阶段农事、产区土壤气候 …）

        只做「凑素材」，不做任何推理补全。两步都空 → 返回空串，交给第 ② 级。
        """
        parts = []
        quick = ""
        try:
            quick = self._rule_answer(user_query, entities)
        except Exception as e:
            print(f"WARN: 规则快答失败：{e}")
        if quick and "暂无" not in quick:
            parts.append("【一、图谱精确查询结果】\n" + quick)

        pack = {"sources": [], "followups": [], "topics": [], "block_count": 0}
        try:
            from core.knowledge_builder import KnowledgeBuilder
            pack = KnowledgeBuilder(self.neo4j_conn).collect(user_query, entities)
            if pack.get("facts_text"):
                parts.append("【二、图谱关联知识（多跳聚合，含防治措施明细）】\n"
                             + pack["facts_text"])
        except Exception as e:
            print(f"WARN: 知识聚合失败（本轮视为图谱未命中，交给本地知识库）：{e}")

        return ("\n\n".join(parts) if parts else ""), pack

    def _local_kb_facts(self, user_query):
        """第 ② 级：本地知识库（原始语料）向量召回。返回 (片段文本, hits)"""
        kb = getattr(self, "local_kb", None)
        if not kb:
            return "", []
        try:
            hits = kb.search(user_query)
        except Exception as e:
            print(f"WARN: 本地知识库检索失败：{e}")
            return "", []
        if not hits:
            return "", []
        text = ("【本地知识库片段（来自系统本地语料库，非知识图谱）】\n"
                + kb.format(hits))
        return text, hits

    def _route(self, user_query, entities):
        """
        三级逐级兜底。返回 (facts, route, pack)：

            ("...", "graph",    pack)  知识图谱命中 —— 事实来自三元组，最可信
            ("...", "local_kb", pack)  图谱没查到，本地知识库向量召回命中
            ("",    "llm",      pack)  前两级都没有 —— 由豆包直接生成并标注来源
        """
        facts, pack = self._graph_facts(user_query, entities)
        if facts:
            # 「有东西」不等于「有答案」：还得看这些事实跟问题相不相关
            score = 1.0
            try:
                from core import local_kb
                score = local_kb.relevance(user_query, facts, self.embed_model)
            except Exception as e:
                print(f"WARN: 相关度计算失败（按命中处理）：{e}")
            if score >= self.GRAPH_HIT_THRESHOLD:
                return facts, "graph", pack
            print("INFO: 图谱事实与问题相关度 %.3f < %.2f，视为未命中，转本地知识库"
                  % (score, self.GRAPH_HIT_THRESHOLD))

        facts, hits = self._local_kb_facts(user_query)
        if facts:
            # 溯源改列语料片段，别再冒充图谱三元组
            pack = dict(pack or {})
            pack["sources"] = [
                "本地知识库片段#%d（相似度 %.2f）" % (i + 1, h.get("score", 0))
                for i, h in enumerate(hits)]
            return facts, "local_kb", pack

        # 走到这里说明前两级都没有依据 —— 图谱那几十条溯源也不该再挂出来：
        # 答案写的是"没查到"，下面却列着一串图谱关系，自相矛盾
        pack = dict(pack or {})
        pack["sources"] = []
        return "", "llm", pack

    # ---------------------------------------------------------------- 大模型组织语言
    SYSTEM_PROMPT = (
        "你是「农业知识问答」助手，面向基层农户和农技员。\n"
        "下面给你的【图谱知识】是**唯一可信来源**：只允许使用其中出现的事实、数字、"
        "名称和措施，严禁编造或补充图谱以外的农药、用量、标准、年份。\n"
        "\n"
        "请把图谱知识组织成一份**完整、能直接照着做**的回答，固定按下面的结构输出 Markdown：\n"
        "\n"
        "## 一句话结论\n"
        "（用一句话说清：这是什么、该怎么办）\n"
        "\n"
        "## 关键信息\n"
        "- 分点列出对象、发生规律 / 适宜条件 / 危害部位与时期等，只写图谱里有的\n"
        "\n"
        "## 具体做法\n"
        "1. 按「农业防治 → 物理防治 → 生物防治 → 化学农药」的优先顺序分点展开\n"
        "2. 每条措施写清楚怎么做；涉及药剂必须带上名称、剂型、使用要点、安全间隔期\n"
        "3. 图谱里有数字的（天数、pH、温度、降水、亩产、间隔期）一个都不能漏\n"
        "\n"
        "## 注意事项\n"
        "- 安全间隔期、轮换用药、施药时期与天气限制、连作与轮作提醒等，只能来自图谱事实\n"
        "\n"
        "写作要求：句子短、说人话；不要重复同一句话；不要输出「根据图谱」之类的客套；"
        "图谱里确实没有的类别，写一行「图谱暂无此项数据」，不要编。"
    )

    # 第 ② 级：本地知识库片段。同样要求"只许用片段里的内容"，但来源换成本地语料
    SYS_LOCAL_KB = (
        "你是「农业知识问答」助手，面向基层农户和农技员。\n"
        "下面给你的【本地知识库片段】是**唯一可信来源**：这些片段来自系统本地语料库"
        "（不是知识图谱），只允许使用其中出现的事实、数字、名称和措施，"
        "严禁编造或补充片段以外的农药、用量、标准、年份。\n"
        "\n"
        "请把片段内容组织成一份**完整、能直接照着做**的回答，固定按下面的结构输出 Markdown：\n"
        "\n"
        "## 一句话结论\n"
        "（用一句话说清：这是什么、该怎么办）\n"
        "\n"
        "## 关键信息\n"
        "- 分点列出对象、发生规律 / 适宜条件 / 危害部位与时期等，只写片段里有的\n"
        "\n"
        "## 具体做法\n"
        "1. 按「农业防治 → 物理防治 → 生物防治 → 化学农药」的优先顺序分点展开\n"
        "2. 每条措施写清楚怎么做；涉及药剂必须带上名称、剂型、使用要点、安全间隔期\n"
        "3. 片段里有数字的（天数、pH、温度、降水、亩产、间隔期）一个都不能漏\n"
        "\n"
        "## 注意事项\n"
        "- 安全间隔期、轮换用药、施药时期与天气限制、连作与轮作提醒等，只能来自片段内容\n"
        "\n"
        "写作要求：句子短、说人话；不要重复同一句话；"
        "片段里确实没有的类别，写一行「本地知识库暂无此项数据」，不要编。"
    )

    # 第 ③ 级：前两级都没命中，只能靠模型自己的常识 —— 放开约束，但必须说清边界
    SYS_LLM = (
        "你是「农业知识问答」助手，面向基层农户和农技员。\n"
        "本次提问在系统的**知识图谱**和**本地知识库**中都没有查到可用依据，"
        "因此请你依据**公开的通用农技常识**作答。\n"
        "\n"
        "硬性要求：\n"
        "1) 只写长期验证过的常规做法，说清什么时候做、怎么做、注意什么；\n"
        "2) **严禁编造**具体农药的推荐用量、登记证号、地方标准或年份数据 —— "
        "药剂可以给名称，用量一律写成「按产品标签说明」；\n"
        "3) 拿不准的内容不要写，宁可少一条也不硬凑；\n"
        "4) 不要写「根据图谱」「根据资料」这类客套，也不要解释为什么查不到。\n"
        "\n"
        "按下面的结构输出 Markdown：\n"
        "## 一句话结论\n"
        "## 关键信息\n"
        "## 具体做法\n"
        "## 注意事项\n"
    )

    # 第 ③ 级答案开头的来源标注（第 ① 级靠 sources 溯源，不需要额外标注）
    ROUTE_HEAD_LLM = "> 本条由**大模型豆包生成**，未命中知识图谱与本地知识库。\n\n"
    # 第 ② 级答案结尾的来源标注
    ROUTE_TAIL_KB = ("\n\n---\n\n"
                     "> 本条依据**本地知识库**片段整理（知识图谱未命中），由大模型豆包输出。")

    @classmethod
    def _apply_route_note(cls, text, route):
        """按路由补一行来源标注；模型自己已经写了就不重复加"""
        text = (text or "").strip()
        if not text:
            return text
        if route == "llm" and "未命中" not in text:
            text = cls.ROUTE_HEAD_LLM + text
        elif route == "local_kb" and "本地知识库" not in text:
            text += cls.ROUTE_TAIL_KB
        return text

    def _compose_prompt(self, query, facts, entities=None, route="graph"):
        """
        组装提示词；流式与非流式共用（问法一致，答案才一致）。返回 (system, user)

        route 决定用哪一套：graph=图谱事实、local_kb=语料片段、llm=不给素材直出。
        """
        ent_txt = ""
        if entities:
            ent_txt = "；".join("%s=%s" % (k, v) for k, v in (entities or {}).items() if v)
        user = ("【农户问题】%s\n" % query)
        if ent_txt:
            user += "【识别到的实体】%s\n" % ent_txt

        if route == "local_kb":
            system = self.SYS_LOCAL_KB
            user += ("\n【本地知识库片段（唯一可信来源，不得超出）】\n%s\n\n"
                     "请据此写出完整回答。" % facts)
        elif route == "llm":
            system = self.SYS_LLM
            user += ("\n【说明】系统知识图谱与本地知识库均未查到可用依据，"
                     "请依据通用农技常识直接作答，并在开头标注来源。\n")
        else:
            system = self.SYSTEM_PROMPT
            user += ("\n【图谱知识（唯一可信来源，不得超出）】\n%s\n\n"
                     "请据此写出完整回答。" % facts)
        return system, user

    def _compose_with_llm(self, query, facts, entities=None, route="graph"):
        """优先走大模型 API；未配置再退回本地小模型；都没有就返回 None（保留原文）"""
        if not self.enable_llm:
            return None
        # 第 ③ 级本来就没有素材可给，不能因为 facts 为空就直接放弃
        if route != "llm" and not facts:
            return None

        system, user = self._compose_prompt(query, facts, entities, route=route)
        text = self._llm_api(system, user, max_tokens=1200)
        if text:
            # 图谱没覆盖到的小节（「图谱暂无此项数据」），再发一次请求让大模型补齐。
            # 只有第 ① 级需要 —— 第 ②③ 级本就是模型自由发挥，没必要再补一轮。
            if route == "graph":
                text = self._supplement(text, query, facts)
            return self._apply_route_note(text, route)

        # 本地小模型兜底（未启用则为 None）
        if self.llm_pipeline is None:
            return None
        try:
            messages = [{"role": "user", "content": user}]
            prompt_text = self.llm_tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            outputs = self.llm_pipeline(
                prompt_text,
                max_new_tokens=200,
                pad_token_id=self.llm_tokenizer.eos_token_id,
                eos_token_id=self.llm_tokenizer.eos_token_id,
                max_length=None
            )
            generated_text = outputs[0]["generated_text"]
            if "<|assistant|>" in generated_text:
                answer = generated_text.split("<|assistant|>")[-1].strip()
            else:
                answer = generated_text.replace(prompt_text, "").strip()
            answer = re.sub(r'<\|.*?\|>', '', answer).strip()
            return answer or None
        except Exception as e:
            print(f"WARN: 大模型润色失败，返回原始答案: {e}")
            return None

    def _refine_with_llm(self, query, structured_answer):
        """兼容旧调用：原先只传一段结构化文本，现在转发到新的组织器"""
        if not self.enable_llm or "暂无" in structured_answer:
            return structured_answer if "暂无" not in structured_answer else "暂时还没有这方面的具体数据哈"
        return self._compose_with_llm(query, structured_answer) or structured_answer

    def _prepare(self, user_query):
        """检索阶段：实体识别 + 三级路由（不含大模型）。流式与非流式共用"""
        entities = self._extract_entities(user_query)
        facts, route, pack = self._route(user_query, entities)
        return entities, facts, route, pack

    def _no_data_answer(self, user_query):
        """前两级都没命中、且大模型也不可用时，给一句引导而不是空白"""
        return ("暂未在知识图谱与本地知识库中找到与「%s」直接相关的数据。\n\n"
                "可以换个问法，例如：\n"
                "- 玉米大斑病怎么防治\n"
                "- 土豆怎么种植\n"
                "- 水稻的适宜土壤\n"
                "- 黄淮海平原适合种什么" % user_query)

    def search_rich(self, user_query, use_llm=True):
        """
        核心查询逻辑（返回完整结果，供页面展示溯源与追问）：

        1. 复用原有实体识别与规则查询，先问知识图谱
        2. 图谱查不到 → 本地知识库向量召回
        3. 两级都查不到 → 大模型依据通用农技常识直接生成（答案里标注来源）
        4. 命中哪一级，就只用哪一级的证据让大模型整理话术
        """
        entities, facts, route, pack = self._prepare(user_query)
        base_ent = {k: v for k, v in entities.items() if v}

        # 第 ③ 级：图谱与本地知识库都没命中 → 豆包直接生成（_compose_with_llm 内标注来源）
        if route == "llm":
            llm_used = False
            answer = self._no_data_answer(user_query)
            if use_llm and self.enable_llm:
                free = self._compose_with_llm(user_query, "", entities, route="llm")
                if free:
                    answer, llm_used = free, True
            return {"answer": answer, "structured": "", "sources": [],
                    "entities": base_ent,
                    "followups": pack.get("followups") or [],
                    "topics": [], "llm_used": llm_used,
                    "route": "llm", "route_name": self.ROUTE_NAME["llm"],
                    "plan_type": "basic", "plan_name": "基础图谱问答"}

        answer, llm_used = facts, False
        if use_llm:
            refined = self._compose_with_llm(user_query, facts, entities, route=route)
            if refined:
                answer, llm_used = refined, True

        return {
            "answer": answer,
            "structured": facts,
            "sources": pack.get("sources") or [],
            "entities": base_ent,
            "followups": pack.get("followups") or [],
            "topics": pack.get("topics") or [],
            "llm_used": llm_used,
            "route": route,
            "route_name": self.ROUTE_NAME.get(route, ""),
            "plan_type": "basic",
            "plan_name": "基础图谱问答",
        }

    def search_rich_stream(self, user_query, use_llm=True):
        """
        **流式版** search_rich（生成器），事件与 `AgriAdvisor.advise_stream` 同构：
        meta → delta(0~N) → done（done 的字段与 `search_rich()` 返回一致）

        ⚠ 一个字都没吐出来时退同步的 `_compose_with_llm`（含本地小模型兜底），
          再不行就用原文 —— 前端以 done 为准，中间片段只用于"边写边看"。

        三级路由与 `search_rich()` 完全一致：
            图谱命中 → 本地知识库命中 → 大模型直出（route 字段会一并给前端）
        """
        entities, facts, route, pack = self._prepare(user_query)
        base = {"structured": facts,
                "sources": pack.get("sources") or [],
                "entities": {k: v for k, v in entities.items() if v},
                "followups": pack.get("followups") or [],
                "topics": pack.get("topics") or [],
                "plan_type": "basic", "plan_name": "基础图谱问答",
                "route": route, "route_name": self.ROUTE_NAME.get(route, "")}

        # 第 ③ 级：图谱与本地知识库都没命中 → 豆包依据通用农技常识直出
        if route == "llm":
            yield {"event": "meta", "plan_name": base["plan_name"],
                   "plan_type": "basic", "entities": base["entities"],
                   "sources": [], "followups": base["followups"],
                   "route": "llm", "route_name": self.ROUTE_NAME["llm"]}

            got = ""
            if use_llm and self.enable_llm and llm_client:
                system, user = self._compose_prompt(user_query, "", entities, route="llm")
                for ev in llm_client.chat_stream(system, user, max_tokens=1200):
                    if ev.get("type") == "delta":
                        got += ev.get("text") or ""
                        yield {"event": "delta", "text": ev.get("text") or ""}
                    elif not ev.get("ok"):
                        break
                if not got.strip():
                    once = self._compose_with_llm(user_query, "", entities, route="llm")
                    if once:
                        got = once
                        yield {"event": "delta", "text": once}

            base.update({"structured": "", "sources": [], "topics": []})
            if got.strip():
                base["answer"] = self._apply_route_note(got.strip(), "llm")
                base["llm_used"] = True
            else:
                base["answer"] = self._no_data_answer(user_query)
                base["llm_used"] = False
            yield {"event": "done", **base}
            return

        yield {"event": "meta", "plan_name": base["plan_name"],
               "plan_type": "basic", "entities": base["entities"],
               "sources": base["sources"], "followups": base["followups"],
               "route": route, "route_name": base["route_name"]}

        got, broke, filled = "", False, False
        if use_llm and self.enable_llm and llm_client:
            system, user = self._compose_prompt(user_query, facts, entities, route=route)
            for ev in llm_client.chat_stream(system, user, max_tokens=1200):
                if ev.get("type") == "delta":
                    got += ev.get("text") or ""
                    yield {"event": "delta", "text": ev.get("text") or ""}
                elif not ev.get("ok"):
                    broke = True
            if not got.strip():
                once = self._compose_with_llm(user_query, facts, entities, route=route)
                if once:
                    got = once
                    filled = True          # ← _compose_with_llm 内部已经补过空小节了
                    yield {"event": "delta", "text": once}
            if broke and len(got.strip()) < 40:
                got = ""

            # 图谱没覆盖到的小节 → 再发一次请求补齐，补出来的内容照样以 delta 流出去。
            # 只有第 ① 级走这条 —— 第 ② 级的空标记写的是「本地知识库暂无此项数据」，
            # answer_filler 认的是「图谱暂无此项数据」，混用会对不上号。
            if got.strip() and not filled and route == "graph":
                for ev in answer_filler.stream_supplement(
                        got, user_query, facts, llm_client.chat_stream,
                        max_tokens=int(getattr(config, "SUPPLEMENT_MAX_TOKENS", 900)),
                        enabled=bool(getattr(config, "ENABLE_LLM_SUPPLEMENT", True))):
                    if ev["event"] == "delta":
                        yield {"event": "delta", "text": ev["text"]}
                    else:
                        got = ev["text"]

        if got.strip():
            base["answer"] = self._apply_route_note(got.strip(), route)
            base["llm_used"] = True
        else:
            base["answer"] = facts
            base["llm_used"] = False
        yield {"event": "done", **base}

    def search(self, user_query, use_llm=True):
        """兼容旧接口：只返回答案正文"""
        return self.search_rich(user_query, use_llm=use_llm)["answer"]


    def close(self):
        self.neo4j_conn.close()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()