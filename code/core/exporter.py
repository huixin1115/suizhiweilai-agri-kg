#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案导出（Word / PDF）
======================
把顾问生成的 Markdown 方案导出成可打印、可分发的文档。

- Word：python-docx 生成 .docx（真格式，Word/WPS 打开无警告）
- PDF ：fpdf2 生成 .pdf，嵌入系统中文 TTF（正文微软雅黑、标题雅黑粗体）
- 两者共用同一套 Markdown 解析，支持：一级/二级标题、引用、无序/有序列表、
  **加粗**、以及表格（方案里的生育期时间轴、适种作物表都是表格）

文件在内存中生成后直接返回，不落盘。
"""
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# 主题色（与网页端一致）
C_PRIMARY = (0x2F, 0x55, 0x97)      # 主蓝
C_GREEN = (0x3F, 0x7D, 0x5F)        # 辅绿
C_TEXT = (0x1F, 0x29, 0x37)         # 正文
C_MUTED = (0x64, 0x74, 0x8B)        # 次要文字
C_QUOTE_BG = (0xF4, 0xF7, 0xFB)     # 引用底色
C_LINE = (0xD8, 0xE0, 0xEA)         # 分隔线

# 导出里的图片尺寸上限（cm）：A4 正文宽约 16.2cm，留白后取 9.6cm 宽
IMG_MAX_W_CM = 9.6
IMG_MAX_H_CM = 11.0

# 候选中文字体（按优先级）。全部为 Windows 自带；缺失时会在 PDF 里提示。
FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc", "微软雅黑"),
    (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf", "黑体"),
    (r"C:\Windows\Fonts\simfang.ttf", r"C:\Windows\Fonts\simfang.ttf", "仿宋"),
    (r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simsun.ttc", "宋体"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
     "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "文泉驿正黑"),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc", "PingFang"),
]


def find_cjk_font():
    """返回 (正文路径, 粗体路径, 字体名)；找不到返回 (None, None, '')"""
    for normal, bold, label in FONT_CANDIDATES:
        if os.path.exists(normal):
            return normal, (bold if os.path.exists(bold) else normal), label
    return None, None, ""


# ======================================================================
#  Markdown 解析
# ======================================================================
def parse_inline(text):
    """把 `**加粗**` 拆成 [(片段, 是否加粗)]，供 Word/PDF 分别渲染"""
    parts, pos = [], 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text or ""):
        if m.start() > pos:
            parts.append((text[pos:m.start()], False))
        parts.append((m.group(1), True))
        pos = m.end()
    if pos < len(text or ""):
        parts.append((text[pos:], False))
    return parts or [("", False)]


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_markdown(md):
    """把 Markdown 解析成块列表：h2/h3/quote/ul/ol/table/p"""
    blocks, lines, i = [], (md or "").split("\n"), 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()

        if not line:
            i += 1
            continue

        # 图片：![图注](本地文件路径) —— 历史记录导出识病照片时用
        # ⚠ 只认整行独占的图片语法，行内混排图片不支持（导出场景用不到）
        m = re.match(r"^!\[(.*?)\]\((.+?)\)\s*$", line)
        if m:
            blocks.append({"type": "image",
                           "caption": m.group(1).strip(),
                           "path": m.group(2).strip()})
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            blocks.append({"type": "h2" if level <= 2 else "h3",
                           "text": m.group(2).strip()})
            i += 1
            continue

        # 表格：当前行以 | 开头且下一行是分隔行
        if line.startswith("|") and i + 1 < n and re.match(r"^\|?[\s:|-]+\|[\s:|-]*$",
                                                           lines[i + 1].strip()):
            header = _split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = _split_row(lines[i])
                cells += [""] * (len(header) - len(cells))
                rows.append(cells[:len(header)])
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        # 引用
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "quote", "text": " ".join(x for x in buf if x)})
            continue

        # 无序列表
        if re.match(r"^[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]).strip())
                i += 1
            blocks.append({"type": "ul", "items": items})
            continue

        # 有序列表
        if re.match(r"^\d+[.、]\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+[.、]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.、]\s+", "", lines[i]).strip())
                i += 1
            blocks.append({"type": "ol", "items": items})
            continue

        # 普通段落（合并连续行）
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|>|[-*]\s|\d+[.、]\s|\|)", lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        blocks.append({"type": "p", "text": " ".join(buf)})
    return blocks


# ======================================================================
#  Word 导出
# ======================================================================
def build_docx(title, meta_lines, markdown, sources=None, footer_text=""):
    """生成 .docx，返回 BytesIO"""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    font_name = "微软雅黑"
    doc = Document()

    # 页面：A4 + 常规页边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.4)
    sec.top_margin = sec.bottom_margin = Cm(2.2)

    style = doc.styles["Normal"]
    style.font.name = font_name
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)

    def set_font(run, size=None, bold=None, color=None, italic=None, name=None):
        run.font.name = name or font_name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), name or font_name)
        if size is not None:
            run.font.size = Pt(size)
        if bold is not None:
            run.bold = bold
        if italic is not None:
            run.italic = italic
        if color is not None:
            run.font.color.rgb = RGBColor(*color)

    def para(space_before=0, space_after=4, indent=0, align=None):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before = Pt(space_before)
        pf.space_after = Pt(space_after)
        pf.line_spacing = 1.35
        if indent:
            pf.left_indent = Cm(indent)
        if align is not None:
            p.alignment = align
        return p

    def add_image(path, caption):
        """
        插入一张图片（居中 + 图注）。
        ⚠ 只给宽或只给高（另一个按原图比例自动算），否则会被拉伸变形；
          细高图按高度兜底，避免超出一页。文件缺失/损坏/格式不支持时
          降级成一行小字，绝不让整份导出失败。
        """
        p = para(space_after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
        ok = False
        if path and os.path.isfile(path):
            try:
                ratio = None
                try:
                    from PIL import Image
                    with Image.open(path) as im:
                        w0, h0 = im.size
                        ratio = (float(h0) / float(w0)) if w0 else None
                except Exception:
                    ratio = None          # 没有 Pillow 也能导出，只是不做高度兜底
                run = p.add_run()
                if ratio and ratio * IMG_MAX_W_CM > IMG_MAX_H_CM:
                    run.add_picture(path, height=Cm(IMG_MAX_H_CM))
                else:
                    run.add_picture(path, width=Cm(IMG_MAX_W_CM))
                ok = True
            except Exception as e:
                print("WARN: 导出时嵌入图片失败（已跳过该图）：%s" % e)
        if not ok:
            set_font(p.add_run("（照片文件缺失，无法嵌入）"),
                     size=9, color=C_MUTED, italic=True)
        if caption:
            cp = para(space_after=6, align=WD_ALIGN_PARAGRAPH.CENTER)
            set_font(cp.add_run(caption), size=9, color=C_MUTED)

    # ---------- 文档标题 ----------
    p = para(space_after=2, align=WD_ALIGN_PARAGRAPH.CENTER)
    set_font(p.add_run(title or "农事方案"), size=19, bold=True, color=C_PRIMARY)
    p = para(space_after=10, align=WD_ALIGN_PARAGRAPH.CENTER)
    set_font(p.add_run("穗知未来——农业知识图谱智能决策服务系统"), size=9, color=C_MUTED)

    # 分隔线
    p = para(space_after=8)
    set_font(p.add_run("─" * 46), size=8, color=C_LINE)

    # ---------- 元信息 ----------
    for line in (meta_lines or []):
        if not line:
            continue
        p = para(space_after=2)
        set_font(p.add_run(line), size=9.5, color=C_MUTED)

    if meta_lines:
        para(space_after=4)

    # ---------- 正文 ----------
    for b in parse_markdown(markdown):
        t = b["type"]
        if t == "h2":
            p = para(space_before=10, space_after=5)
            set_font(p.add_run(b["text"]), size=14, bold=True, color=C_PRIMARY)
        elif t == "h3":
            p = para(space_before=8, space_after=4)
            set_font(p.add_run(b["text"]), size=11.5, bold=True, color=C_GREEN)
        elif t == "image":
            add_image(b.get("path"), b.get("caption"))
        elif t == "quote":
            p = para(space_after=6, indent=0.4)
            set_font(p.add_run(b["text"]), size=10, color=C_MUTED, italic=True)
        elif t in ("ul", "ol"):
            for k, item in enumerate(b["items"]):
                p = para(space_after=2, indent=0.55)
                mark = "• " if t == "ul" else "%d. " % (k + 1)
                set_font(p.add_run(mark), size=10.5, color=C_PRIMARY, bold=True)
                for seg, bold in parse_inline(item):
                    set_font(p.add_run(seg), size=10.5, bold=bold, color=C_TEXT)
        elif t == "table":
            rows = [b["header"]] + b["rows"]
            tb = doc.add_table(rows=len(rows), cols=len(b["header"]))
            tb.style = "Table Grid"
            tb.alignment = WD_TABLE_ALIGNMENT.CENTER
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    c = tb.cell(ri, ci)
                    c.text = ""
                    cp = c.paragraphs[0]
                    cp.paragraph_format.space_before = Pt(1)
                    cp.paragraph_format.space_after = Pt(1)
                    for seg, bold in parse_inline(cell):
                        set_font(cp.add_run(seg), size=9.5,
                                 bold=bold or ri == 0,
                                 color=C_PRIMARY if ri == 0 else C_TEXT)
            para(space_after=4)
        else:
            p = para(space_after=4)
            for seg, bold in parse_inline(b["text"]):
                set_font(p.add_run(seg), size=10.5, bold=bold, color=C_TEXT)

    # ---------- 知识溯源 ----------
    if sources:
        p = para(space_before=12, space_after=5)
        set_font(p.add_run("附：知识溯源（共 %d 条）" % len(sources)),
                 size=11.5, bold=True, color=C_GREEN)
        for s in sources:
            p = para(space_after=1, indent=0.4)
            set_font(p.add_run("· " + str(s)), size=9, color=C_MUTED)

    # ---------- 页脚说明 ----------
    p = para(space_before=14)
    set_font(p.add_run("─" * 46), size=8, color=C_LINE)
    p = para(space_after=0)
    set_font(p.add_run(footer_text or
                       "本方案由知识图谱事实生成，结果仅供田间参考；重大病虫害请以当地植保部门鉴定为准。"),
             size=8.5, color=C_MUTED)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# ======================================================================
#  PDF 导出
# ======================================================================
class _AgriPDF(object):
    """按需创建 fpdf 子类，避免导入失败时影响 Word 导出"""
    @staticmethod
    def create(title, meta_lines, markdown, sources, footer_text):
        from fpdf import FPDF

        normal, bold, label = find_cjk_font()
        if not normal:
            raise RuntimeError("系统中未找到可用的中文字体（如微软雅黑 / 黑体），无法生成中文 PDF")

        class PDF(FPDF):
            def header(self):
                if self.page_no() == 1:
                    return
                self.set_font("yh", size=8)
                self.set_text_color(*C_MUTED)
                self.cell(0, 6, title or "", align="L")
                self.ln(2)
                self.set_draw_color(*C_LINE)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
                self.ln(4)

            def footer(self):
                self.set_y(-13)
                self.set_font("yh", size=8)
                self.set_text_color(*C_MUTED)
                self.cell(0, 6, "第 %d 页" % self.page_no(), align="C")

        pdf = PDF(orientation="P", unit="mm", format="A4")
        pdf.add_font("yh", "", normal)
        pdf.add_font("yh", "B", bold)
        pdf.set_margins(18, 16, 18)
        pdf.set_auto_page_break(True, margin=18)
        pdf.add_page()

        # 注意：fpdf2 的 multi_cell 默认会把光标停在行尾（XPos.RIGHT），
        # 必须显式指定 new_x/new_y，否则后续块会因起始 x 已在右边界而报
        # "Not enough horizontal space to render a single character"
        NL = dict(new_x="LMARGIN", new_y="NEXT")

        # ---------- 标题 ----------
        pdf.set_font("yh", "B", 17)
        pdf.set_text_color(*C_PRIMARY)
        pdf.multi_cell(0, 9, title or "农事方案", align="C", **NL)
        pdf.set_font("yh", "", 8.5)
        pdf.set_text_color(*C_MUTED)
        pdf.multi_cell(0, 5, "穗知未来——农业知识图谱智能决策服务系统", align="C", **NL)
        pdf.ln(4)

        for line in (meta_lines or []):
            if not line:
                continue
            pdf.set_font("yh", "", 9)
            pdf.set_text_color(*C_MUTED)
            pdf.multi_cell(0, 5, line, **NL)
        pdf.ln(2)

        # ---------- 正文 ----------
        for b in parse_markdown(markdown):
            t = b["type"]
            if t == "h2":
                pdf.ln(2)
                pdf.set_font("yh", "B", 13)
                pdf.set_text_color(*C_PRIMARY)
                pdf.multi_cell(0, 7, b["text"], **NL)
                pdf.ln(1)
            elif t == "h3":
                pdf.ln(1)
                pdf.set_font("yh", "B", 11)
                pdf.set_text_color(*C_GREEN)
                pdf.multi_cell(0, 6, b["text"], **NL)
                pdf.ln(0.5)
            elif t == "image":
                # PDF 里图片同样居中，宽度不超过正文宽；文件缺失时降级为一行小字
                pdf.ln(1)
                p_ = (b.get("path") or "")
                if p_ and os.path.isfile(p_):
                    try:
                        w = min(95.0, pdf.epw)
                        x = pdf.l_margin + max(0.0, (pdf.epw - w) / 2.0)
                        pdf.image(p_, x=x, w=w)
                        pdf.ln(1.5)
                        if b.get("caption"):
                            pdf.set_font("yh", "", 8.5)
                            pdf.set_text_color(*C_MUTED)
                            pdf.multi_cell(0, 4.6, _plain(b["caption"]),
                                           align="C", **NL)
                    except Exception as e:
                        print("WARN: PDF 导出嵌入图片失败（已跳过该图）：%s" % e)
                else:
                    pdf.set_font("yh", "", 9)
                    pdf.set_text_color(*C_MUTED)
                    pdf.multi_cell(0, 5, "（照片文件缺失，无法嵌入）",
                                   align="C", **NL)
                pdf.ln(1)
            elif t == "quote":
                pdf.set_fill_color(*C_QUOTE_BG)
                pdf.set_text_color(*C_MUTED)
                pdf.set_font("yh", "", 9.5)
                pdf.multi_cell(0, 5.2, b["text"], fill=True, **NL)
                pdf.ln(2)
            elif t in ("ul", "ol"):
                for k, item in enumerate(b["items"]):
                    mark = "- " if t == "ul" else "%d. " % (k + 1)
                    x0 = pdf.get_x()
                    pdf.set_font("yh", "", 10)
                    pdf.set_text_color(*C_PRIMARY)
                    pdf.cell(5, 5.4, mark)
                    pdf.set_x(x0 + 5)
                    pdf.set_text_color(*C_TEXT)
                    pdf.multi_cell(0, 5.4, _plain(item), **NL)
                pdf.ln(1)
            elif t == "table":
                cols = b["header"]
                widths = _table_widths(cols, b["rows"], pdf.epw)
                pdf.set_font("yh", "", 8.5)
                pdf.set_text_color(*C_TEXT)
                with pdf.table(col_widths=widths, text_align="LEFT",
                               line_height=5, padding=(1.2, 1.2, 1.2, 1.2)) as table:
                    r = table.row()
                    pdf.set_font("yh", "B", 8.5)
                    for c in cols:
                        r.cell(_plain(c))
                    pdf.set_font("yh", "", 8.5)
                    for row in b["rows"]:
                        rr = table.row()
                        for c in row:
                            rr.cell(_plain(c))
                pdf.set_x(pdf.l_margin)
                pdf.ln(2)
            else:
                pdf.set_font("yh", "", 10)
                pdf.set_text_color(*C_TEXT)
                pdf.multi_cell(0, 5.6, _plain(b["text"]), **NL)
                pdf.ln(1)

        # ---------- 溯源 ----------
        if sources:
            pdf.ln(3)
            pdf.set_font("yh", "B", 11)
            pdf.set_text_color(*C_GREEN)
            pdf.multi_cell(0, 6, "附：知识溯源（共 %d 条）" % len(sources), **NL)
            pdf.set_font("yh", "", 8.5)
            pdf.set_text_color(*C_MUTED)
            for s in sources:
                pdf.multi_cell(0, 4.6, "· " + str(s), **NL)

        pdf.ln(4)
        pdf.set_font("yh", "", 8.5)
        pdf.set_text_color(*C_MUTED)
        pdf.multi_cell(0, 4.8, footer_text or
                       "本方案由知识图谱事实生成，结果仅供田间参考；重大病虫害请以当地植保部门鉴定为准。",
                       **NL)

        raw = pdf.output()
        return io.BytesIO(bytes(raw))


# PDF 用的中文字体没有下标数字（CO₂ 之类）的字形，会丢字并刷一堆字体警告
# —— 统一换成普通数字，观感几乎无损
_SUBSCRIPT_FIX = str.maketrans({
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "⁻": "-", "⁺": "+",
})


def _plain(text):
    """PDF 里不做加粗混排，直接去掉 ** 标记；顺带修掉字体缺字形的下标字符"""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "").translate(_SUBSCRIPT_FIX)


def _table_widths(header, rows, total):
    """按各列最长内容估算列宽，保证表格不溢出页面"""
    n = len(header)
    if not n:
        return None
    weights = []
    for i in range(n):
        longest = len(str(header[i]))
        for row in rows:
            if i < len(row):
                longest = max(longest, len(str(row[i])))
        weights.append(max(4, min(longest, 26)))
    s = float(sum(weights))
    return [total * w / s for w in weights]


# ======================================================================
# 对外入口
# ======================================================================
def export_docx(title, meta_lines, markdown, sources=None, footer_text=""):
    return build_docx(title, meta_lines, markdown, sources, footer_text)


def export_pdf(title, meta_lines, markdown, sources=None, footer_text=""):
    return _AgriPDF.create(title, meta_lines, markdown, sources, footer_text)


if __name__ == "__main__":
    demo_md = """## 水稻农事管理方案
> 全生育期约 150 天；播种季节：春；收获季节：秋。

| 阶段 | 历时 | 关键农事操作 |
|---|---|---|
| 播种出苗期 | 12 天 | 适时播种、查苗补苗 |
| 分蘖期/苗期 | 27 天 | 化学除草、看苗追肥 |
| 拔节期 | 27 天 | 看苗追肥、科学灌溉 |

### 常规田间管理要点
- **深耕整地**（播前）：改善耕层结构，破除犁底层
- **适时收获**（成熟期）：保证产量与品质

### 茬口与风险提示
1. 优先选择与本地已有作物**不同科属**的品种进行轮作。
2. 生育期天数应留出 15~20 天农耗，避免茬口冲突。
"""
    ws = r"C:\Users\User\WorkBuddy\2026-09-17-20-15-03"
    meta = ["作物：水稻", "生成时间：2026-09-21 10:40", "知识来源：Neo4j 农业知识图谱"]
    src = ["水稻 -[生育期]-> 拔节期", "水稻 -[适配肥料]-> 尿素"]
    for kind, fn in (("docx", export_docx), ("pdf", export_pdf)):
        try:
            buf = fn("水稻农事管理方案", meta, demo_md, src)
            path = os.path.join(ws, "_export_demo." + kind)
            open(path, "wb").write(buf.getvalue())
            print("%-5s OK  %d bytes -> %s" % (kind, len(buf.getvalue()), path))
        except Exception as e:
            print("%-5s FAIL %s" % (kind, e))
