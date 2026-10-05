# -*- coding: utf-8 -*-
"""
答案补全：图谱里没有覆盖到的小节，改由大模型用通用农技知识补齐。

背景
----
为了让答案"只说实话"，提示词里要求：图谱没覆盖到的类别写一行
「图谱暂无此项数据」。实测下来，一条防治建议里常有三四节是这样
（比如"物理防治""生物防治"），整份答案看着像没写完 —— 截图里就是这样。

做法
----
**主答案一个字都不动**：那部分严格只用图谱事实，是可信来源，不能被模型改写。
生成完之后扫一遍，找出所有空小节，**再发一次请求**让大模型用通用农技常识补齐，
以「补充：通用农技建议（大模型生成，非本图谱数据）」单独成节接在末尾。

这样两个事同时成立：内容补上了，而且读者一眼能分清
哪句来自图谱、哪句来自模型 —— 参赛材料的"真实性"这条也就站得住。

用法（两条链路都这么接）
------------------------
同步：

    if answer_filler.should_supplement(answer):
        answer = answer_filler.fill(answer, question, facts, llm_client.chat)

流式：把 `stream_supplement` 产出的 delta 转给前端，最后用 final 里的文本当答案。

补全内容会**就地填回原来那个空小节** —— 把「图谱暂无此项数据」那一行直接换成正文，
并在正文前留一行 `INLINE_NOTE`（「以下由**大模型豆包生成**，非本图谱数据」）；
对不上号的一律落到末尾，内容不会丢。
"""
import re

# 图谱没数据时，提示词要求大模型写的那一行
EMPTY_MARK = "图谱暂无此项数据"

# 「小节标题」的识别规则：
#   ① Markdown 标题：            ### 物理防治
#   ② 整行加粗：                 **物理防治**
#   ③ 短编号行/短条目：          2. 物理防治      - 生物防治
# ③ 的边界靠"短 + 不带句读"来划：正常句子里有逗号句号，会被排除掉。
_HEAD = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
_LABEL = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\d+\s*[.、)）]\s*)?(?:\*\*)?\s*"
    r"([^，,。；;：:！!？?、\n]{2,20}?)\s*(?:\*\*)?\s*$")


def _is_our_note(line):
    """
    这行是不是我们自己补上去的标注（而不是模型写的「图谱暂无此项数据」）？

    很重要：标注里也带着 EMPTY_MARK 那几个字（INLINE_NOTE 是「以下由**大模型豆包生成**，
    非本图谱数据」），若不排除，补过一轮的答案会被再判一次"有空小节"，于是又补一轮
    —— 来回叠加。
    （另外末尾那节「补充：通用农技建议（大模型豆包生成，非本图谱数据）」也会命中这里，
      反正它本来也不含空标记，多排除一次无害。）
    """
    return "大模型" in line and "非本图谱数据" in line


def find_empty_sections(text, limit=6):
    """
    找出答案里"内容为空"的小节标题。

    判据：某一节标题到下一个标题之间，出现了「图谱暂无此项数据」。
    取**最内层**标题（遇到 `###` 就用 `###`，不会退回外面的 `##`），
    所以"## 二、防治措施"下面那个空的"### 物理防治"能被准确挑出来。

    返回 [标题, ...]（去重、按出现顺序，最多 limit 个）。找不到就返回 []。
    """
    if not text or EMPTY_MARK not in text:
        return []
    out = []
    cur = None
    for raw in str(text).split("\n"):
        line = raw.strip()
        if not line or _is_our_note(line):
            continue
        m = _HEAD.match(line)
        if m:
            cur = _clean(m.group(2))
            continue
        if not cur:
            continue
        if EMPTY_MARK in line:
            if cur and cur not in out:
                out.append(cur)
            continue
        # 只有"看起来像小节标题"的短行才允许刷新当前小节，
        # 免得正文里一句"注意通风"把真正的标题顶掉
        m2 = _LABEL.match(line)
        if m2:
            cur = _clean(m2.group(1))
    return out[:limit]


def should_supplement(answer):
    """有没有需要补的小节（先做一次廉价判断，命中才去扫）"""
    return bool(answer) and EMPTY_MARK in str(answer) and bool(find_empty_sections(answer))


def _clean(s):
    s = str(s or "").strip().strip("*").strip()
    s = re.sub(r"^[#\s]+", "", s)
    s = re.sub(r"^[（(]\s*", "", s).strip()
    return s


SUPPLEMENT_SYS = (
    "你是「农业智能决策顾问」，服务基层农业从业者。\n"
    "这一轮只做一件事：**补全**知识图谱没有覆盖到的条目。\n"
    "硬性要求：\n"
    "1) 用长期验证过的常规农技做法，写到能直接照做 —— 什么时候做、怎么做、注意什么；\n"
    "2) **不要编造**具体农药的推荐用量、登记证号、地方标准或年份数据。"
    "药剂可以说名称，用量一律写成「按产品标签说明」；\n"
    "3) 不要重复图谱里已经写过的事实，只补缺的那几条；\n"
    "4) 句子短、说人话，不要「综上所述」「由此可见」这类套话；\n"
    "5) 每条补全用三级标题（### 小节名），每条下面 2~4 句，"
    "不要写总标题、不要写开头结尾的客套话；\n"
    "6) 必须紧贴这一小节自己的主题写 —— 叫「物理防治」就只写物理防治，"
    "叫「注意事项」就只写注意事项，别跑去讲别的小节；"
    "实在没有把握的就不要写，宁可少一条也不硬凑。"
)


def compose_supplement_prompt(question, facts, answer, facts_limit=1400):
    """
    组装补全请求。返回 (system, user)；没有空小节时返回 (None, None)。

    facts 一并带上，是为了让模型知道**边界在哪** —— 免得把图谱里已经写过的
    又重复一遍，或者补出与图谱事实互相矛盾的内容。
    """
    sections = find_empty_sections(answer)
    if not sections:
        return None, None

    facts = (facts or "").strip()
    if len(facts) > facts_limit:
        facts = facts[:facts_limit] + "\n…（图谱事实较长，此处截断）"

    todo = "\n".join("- %s" % s for s in sections)
    user = ("【农户问题】%s\n\n"
            "【知识图谱已经给出的事实（已在上文呈现，请勿重复）】\n%s\n\n"
            "【需要你补全的小节】\n%s\n\n"
            "请用通用农技常识逐条补齐，每条用三级标题「### 小节名」，"
            "下面 2~4 句可操作的做法。"
            % (question, facts or "（本次没有可用的事实）", todo))
    return SUPPLEMENT_SYS, user


# 就地补齐后，在补出来的正文前面留一行标注。
# ⚠ 这一行是**真实性凭据**：图谱事实与模型生成的内容混排却不标出处，是最容易被追问的地方。
#   文案要求来自用户原话：「给个提示：大模型豆包生成」。
#   想关掉就把它清空（`INLINE_NOTE = ""`），apply() 会自动不加 —— 识别"自己的标注"的
#   _is_our_note() 是按内容特征判的，清空后自动失效，不会误伤正文。
INLINE_NOTE = "> 以下由**大模型豆包生成**，非本图谱数据"

# 实在对不上号的那些（模型自己起了新标题），统一落到末尾这一节
SUPPLEMENT_HEAD = (
    "\n\n---\n\n"
    "## 补充：通用农技建议（大模型豆包生成，非本图谱数据）\n\n"
    "> 下面这几条图谱里没有对应记录，由大模型豆包按通用农技常识补充，供田间参考；"
    "具体用药与用量请以产品标签和当地植保部门指导为准。\n\n"
)

# 流式时先说一句"正在补"，免得这十几秒看着像卡住（final 会把整段覆盖掉）
WAIT_NOTE = "\n\n> 图谱里没覆盖到的几条，正在让大模型补 …\n"


def _key(s):
    """
    标题归一化，用来把补全内容对回原来的小节。
        （二）物理防治 / 二）物理防治 / 2. 物理防治 / 物理防治  →  物理防治
    """
    s = re.sub(r"[（()）\[\]【】、，,。.；;：:＃#\s]+", "", str(s or ""))
    s = re.sub(r"^[一二三四五六七八九十]+", "", s)
    s = re.sub(r"^\d+", "", s)
    return s.strip()


def _parse_blocks(text):
    """把补全内容按 Markdown 标题切成 [(标题, 正文), ...]"""
    blocks, title, buf = [], None, []
    for raw in str(text or "").split("\n"):
        m = _HEAD.match(raw.strip())
        if m:
            if title is not None:
                blocks.append((title, "\n".join(buf).strip()))
            title, buf = _clean(m.group(2)), []
            continue
        if title is not None:
            buf.append(raw)
    if title is not None:
        blocks.append((title, "\n".join(buf).strip()))

    blocks = [(t, b) for t, b in blocks if b]
    if not blocks:                        # 模型没给标题 → 整段当一块
        body = str(text or "").strip()
        if body:
            blocks.append(("", body))
    return blocks


def apply(answer, supplement):
    """
    把补全内容**就地填回原来的空小节**。

    模型按 `### 小节名` 分块返回；这里用归一化后的名字去匹配答案里那些
    写着「图谱暂无此项数据」的小节，对上就把那一行换成补全内容。
    对不上号的（模型自己起了新标题）统一追加到末尾 —— 内容不会丢。
    """
    blocks = _parse_blocks(supplement)
    if not blocks:
        return answer

    pools, order = {}, []
    for t, b in blocks:
        k = _key(t) or ("\u0001%d" % len(order))     # 没标题的单独成池，天然对不上号
        if k not in pools:
            pools[k] = []
            order.append(k)
        pools[k].append((t, b))

    out, cur = [], ""
    for raw in str(answer).split("\n"):
        s = raw.strip()
        # ① 先排掉我们自己补的标注（否则再跑一遍会把标注重替换一次）
        if _is_our_note(s):
            out.append(raw)
            continue
        # ② 再判空标记 —— 必须排在标题判定前面：万一模型把这一行写成
        #    不带句号的"图谱暂无此项数据"，_LABEL 会把它当成小节标题，
        #    那样就找不着真正的原小节了
        if s and EMPTY_MARK in s:
            k = _key(cur)
            if k in pools and pools[k]:
                _t, b = pools[k].pop(0)
                out.append(((INLINE_NOTE + "\n") if INLINE_NOTE.strip() else "") + b)
                continue
            out.append(raw)
            continue
        if s:
            m = _HEAD.match(s) or _LABEL.match(s)
            if m:
                cur = _clean(m.group(m.lastindex or 1))
        out.append(raw)

    leftover = [(t, b) for k in order for (t, b) in pools[k]]
    if not leftover:
        return "\n".join(out)

    tail = "\n\n".join(("### %s\n%s" % (t, b)) if t else b for t, b in leftover)
    return "\n".join(out).rstrip() + SUPPLEMENT_HEAD + tail + "\n"


def section_names(answer):
    """方便日志/排查：这次补了哪些小节"""
    return find_empty_sections(answer)


# ---------------------------------------------------------------------------
# 两个现成的接入入口：同步 / 流式
# 两条链路（顾问页 agri_advisor、基础问答 rag_search）都用这两个，避免各写一份。
# 共同原则：**补全是锦上添花，任何失败都必须原样返回主答案。**
# ---------------------------------------------------------------------------

def fill(answer, question, facts, chat_fn, max_tokens=900, enabled=True):
    """
    同步补全。chat_fn(system, user, max_tokens=...) -> (正文 或 None, 错误说明)
    没有空小节 / 未启用 / 调用失败 → 原答案原样返回。
    """
    try:
        if not enabled or not should_supplement(answer):
            return answer
        system, user = compose_supplement_prompt(question, facts, answer)
        if not user:
            return answer
        res = chat_fn(system, user, max_tokens=max_tokens)
        text = res[0] if isinstance(res, (tuple, list)) else res
        if not text or not str(text).strip():
            return answer
        print("INFO: 已补全空小节：%s" % "、".join(find_empty_sections(answer)))
        return apply(answer, text)
    except Exception as e:
        print("WARN: 空小节补全失败（返回原答案）：%s" % e)
        return answer


def stream_supplement(answer, question, facts, stream_fn, max_tokens=900, enabled=True):
    """
    流式补全。生成器，依次 yield：
        {"event": "delta", "text": "..."}   —— 转发给前端，目前只有一条进度提示
        {"event": "final", "text": "..."}   —— 最后一个，是**补全后的完整答案**

    ⚠ 补全是**就地填入原位置**的，没法像正文那样边生成边写进去，
      所以这里只发一句"正在补"的提示，等全部补完再由 final 一次性覆盖渲染。
      调用方必须以 final 里的文本作为最终答案。
    """
    if not enabled or not should_supplement(answer):
        yield {"event": "final", "text": answer}
        return
    system, user = compose_supplement_prompt(question, facts, answer)
    if not user:
        yield {"event": "final", "text": answer}
        return

    yield {"event": "delta", "text": WAIT_NOTE}
    sup = ""
    try:
        for ev in stream_fn(system, user, max_tokens=max_tokens):
            if ev.get("type") == "delta":
                sup += ev.get("text") or ""
    except Exception as e:
        print("WARN: 空小节补全（流式）失败：%s" % e)

    if sup.strip():
        print("INFO: 已补全空小节：%s" % "、".join(find_empty_sections(answer)))
        yield {"event": "final", "text": apply(answer, sup)}
    else:
        yield {"event": "final", "text": answer}

