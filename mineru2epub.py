#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mineru2epub.py - 将 MinerU (2.5 Pro 1.2B / 3.x / 4.x) 输出的 *_content_list.json 无损转为标准 EPUB3
特性:
  1. 直接利用 MinerU 视觉坐标分离结果: 自动剔除 header/footer/page_number，精准提取独立 footnote 块
  2. 支持插图自动打包进 EPUB3 (OEBPS/images/) 并附带图注 (image_caption)
  3. 通用全书章节与特殊区块识别 (目录/序言/导言/部/章/尾注/词汇表/参考文献/索引)
  4. 每章脚注统一重编码 1..N，基于页号就近与正文中上标/方括号/星号建立 EPUB3 双向弹窗跳转
  5. 可选衔接本地 Ollama (qwen14b-pro) 做结构仲裁或 OCR 错词精修
  6. 输出完整防丢字审计报告 (_audit_report.json)

用法:
  python3 mineru2epub.py ./mineru_out/book/vlm/book_content_list.json --title "书名" --author "作者"
  python3 mineru2epub.py ./mineru_out/book/vlm/book_content_list.json --qwen-refine --qwen-model qwen14b-pro
"""

import argparse
import html
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None


# ==================== 数据结构 ====================
@dataclass
class FootnoteItem:
    unified_num: int
    unit_id: str
    orig_marker: str
    marker_kind: str   # number / symbol / auto
    text: str
    page_idx: int
    matched: bool = False


@dataclass
class SectionUnit:
    unit_id: str
    title: str
    kind: str          # toc, preface, introduction, part, body, endnotes, glossary, bibliography, index, appendix
    level: int = 1
    blocks: List[Dict] = field(default_factory=list)
    footnotes: List[FootnoteItem] = field(default_factory=list)


# ==================== 正则与辅助函数 ====================
SPECIAL_PATTERNS = [
    ("toc", re.compile(r"^(?:table\s+of\s+contents|contents|目\s*录|总\s*目\s*录)$", re.I)),
    ("preface", re.compile(r"^(?:preface|foreword|author['’]?s\s+note|translator['’]?s\s+note|序|序\s*言|前\s*言|自\s*序|译\s*序|导\s*读)$", re.I)),
    ("acknowledgments", re.compile(r"^(?:acknowledg(?:e)?ments?|致\s*谢|鸣\s*谢)$", re.I)),
    ("chronology", re.compile(r"^(?:chronology|timeline|年\s*表|大\s*事\s*记)$", re.I)),
    ("introduction", re.compile(r"^(?:introduction|prologue|导\s*言|引\s*言|引\s*论|绪\s*论|楔\s*子)$", re.I)),
    ("epilogue", re.compile(r"^(?:epilogue|afterword|conclusion|尾\s*声|后\s*记|跋|结\s*语)$", re.I)),
    ("appendix", re.compile(r"^(?:appendi(?:x|ces)\b.*|附\s*录.*)$", re.I)),
    ("endnotes", re.compile(r"^(?:notes|endnotes|chapter\s+notes|注\s*释|尾\s*注|全\s*书\s*注\s*释)$", re.I)),
    ("glossary", re.compile(r"^(?:glossary(?:\s+of\s+terms)?|vocabulary|词\s*汇\s*表|术\s*语\s*表|人\s*物\s*表)$", re.I)),
    ("bibliography", re.compile(r"^(?:(?:selected\s+)?bibliography|references|works\s+cited|参\s*考\s*文\s*献|参\s*考\s*书\s*目)$", re.I)),
    ("index", re.compile(r"^(?:(?:general\s+)?index|索\s*引|主\s*题\s*索\s*引)$", re.I)),
]

RE_PART = re.compile(
    r"^(?:(?:PART|BOOK|VOLUME|VOL\.?)\s+(?:\d+|[IVXLCDM]+|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)\b.*|"
    r"第\s*[零一二三四五六七八九十百0-9]+\s*[部卷篇编].*|[上中下]\s*[部卷篇编].*)$",
    re.I,
)
RE_CHAPTER = re.compile(
    r"^(?:(?:CHAPTER|CHAP\.?|CH\.?|SECTION|LETTER|ESSAY)\s+(?:\d+|[IVXLCDM]+|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)\b.*|"
    r"第\s*[零一二三四五六七八九十百0-9]+\s*[章回节讲].*|[一二三四五六七八九十百]{1,4}\s*[、.．]\s*\S.*)$",
    re.I,
)

# 脚注起行标记提取
RE_FN_PREFIX = re.compile(
    r"^\s*(?:"
    r"\[(\d{1,3})\]|"
    r"([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])|"
    r"([¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3})|"
    r"([*†‡§¶]{1,3})|"
    r"(?:注\s*(\d{1,3})|【注(\d*)】)[：:.]?|"
    r"(\d{1,3})[\.\s]\s*"
    r")\s*(.+)$",
    re.S,
)

CIRCLED_MAP = {c: str(i + 1) for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳")}
SUP_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

RE_REF_BRACKET = re.compile(r"(?<=[A-Za-z0-9\u4e00-\u9fff.,;:!?\"'”’)\]])\s*\[(\d{1,3})\]")
RE_REF_CIRCLED = re.compile(r"([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])")
RE_REF_SUP = re.compile(r"(?<=[A-Za-z0-9\u4e00-\u9fff.,;:!?\"'”’)\]])([¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3})")
RE_REF_SYMBOL = re.compile(r"(?<=[A-Za-z0-9\u4e00-\u9fff.,;:!?\"'”’)\]])\s*([*†‡§¶]{1,2})(?=\s|$|[,.;:!?\"'”’)\]])")


def count_non_ws(s: str) -> int:
    return len(re.sub(r"\s+", "", s))


def detect_lang(items: List[Dict]) -> str:
    sample = " ".join(str(x.get("text", "")) for x in items[:300])
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    lat = len(re.findall(r"[A-Za-z]", sample))
    if cjk + lat == 0:
        return "en"
    return "zh" if (cjk / (cjk + lat)) >= 0.4 else "en"


def classify_heading(text: str, text_level: Optional[int], page_idx: int, total_pages: int) -> Optional[Tuple[str, int]]:
    """根据 MinerU text_level + 语义模式判断是否为章节/特殊区块标题"""
    t = text.strip()
    if not t or len(t) > 95:
        return None
    # 排除明显带句末标点的长句子
    if re.search(r"[。！？；;]$", t) or (t.endswith(".") and len(t) > 35 and not RE_CHAPTER.match(t)):
        return None

    pos_ratio = page_idx / max(total_pages, 1)
    clean_t = t.strip("#*—–-_= ")

    for kind, pat in SPECIAL_PATTERNS:
        if pat.match(clean_t):
            if kind in {"endnotes", "index", "glossary", "bibliography"} and pos_ratio < 0.25:
                return None
            return kind, 1

    if RE_PART.match(clean_t):
        return "part", 1

    if RE_CHAPTER.match(clean_t):
        return "body", 2

    # MinerU 明确给出了标题层级 (text_level == 1 或 2)，且长度像标题
    if text_level in (1, 2) and 2 <= len(clean_t) <= 75 and not re.search(r"[,，]$", clean_t):
        return ("part", 1) if text_level == 1 and len(clean_t) <= 30 else ("body", 2)

    return None


def parse_fn_marker(raw_text: str) -> Tuple[str, str, str]:
    """解析 MinerU 提取的 footnote 文本前导序号/符号，返回 (norm_marker, kind, clean_body)"""
    t = raw_text.strip()
    m = RE_FN_PREFIX.match(t)
    if not m:
        return "", "auto", t
    g_br, g_cir, g_sup, g_sym, g_zh1, g_zh2, g_num, body = m.groups()
    body = body.strip()
    if g_br:
        return g_br, "number", body
    if g_cir:
        return CIRCLED_MAP[g_cir], "number", body
    if g_sup:
        return g_sup.translate(SUP_TRANS), "number", body
    if g_sym:
        return g_sym, "symbol", body
    if g_zh1 or g_zh2:
        num = g_zh1 or g_zh2
        return num, ("number" if num.isdigit() else "symbol"), body
    if g_num:
        return g_num, "number", body
    return "", "auto", t


# ==================== 可选本地 Qwen 精修 ====================
def refine_units_with_ollama(units: List[SectionUnit], model: str, url: str, batch_size: int = 6, lang: str = "en"):
    if requests is None:
        print("  [警告] 未安装 requests，跳过 Qwen 精修")
        return
    jobs = []
    for ui, u in enumerate(units):
        if u.kind in {"toc", "index", "bibliography"}:
            continue
        for bi, b in enumerate(u.blocks):
            if b["type"] == "p" and len(b["text"]) >= 45:
                jobs.append((ui, "p", bi, b["text"]))
        for fi, fn in enumerate(u.footnotes):
            if len(fn.text) >= 20:
                jobs.append((ui, "fn", fi, fn.text))

    print(f"  Qwen ({model}) 精修任务数: {len(jobs)}")
    re_token = re.compile(r"\{\{FN:[a-zA-Z0-9_-]+:\d+\}\}")
    for s in range(0, len(jobs), batch_size):
        batch = jobs[s:s + batch_size]
        framed = "\n".join(f"<<<SEG:{k}>>>\n{j[3]}\n<<<END:{k}>>>" for k, j in enumerate(batch, 1))
        prompt = (
            "校对下面OCR文本中残留的极少数形近错别字或断词错误。严禁增删改写句子，必须原封不动保留所有 {{FN:...}} 标记，"
            "严格按 <<<SEG:编号>>> ... <<<END:编号>>> 输出：\n\n" + framed
        )
        try:
            r = requests.post(url, json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.05}}, timeout=300)
            resp = r.json().get("response", "")
            got = {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"<<<SEG:(\d+)>>>\s*(.*?)\s*<<<END:\1>>>", resp, re.S)}
            for k, (ui, kind, idx, orig) in enumerate(batch, 1):
                cand = got.get(k)
                if not cand:
                    continue
                # 防丢字与占位符守卫
                if re_token.findall(orig) != re_token.findall(cand):
                    continue
                if not (0.85 <= len(cand) / max(len(orig), 1) <= 1.15):
                    continue
                if kind == "p":
                    units[ui].blocks[idx]["text"] = cand
                else:
                    units[ui].footnotes[idx].text = cand
        except Exception as e:
            print(f"  批次 {s} 异常已回退原文: {e}")


# ==================== 核心转换流程 ====================
def convert_mineru_to_epub(
    content_list_path: str,
    out_epub: str,
    title: str,
    author: str = "Unknown",
    use_qwen: bool = False,
    qwen_model: str = "qwen14b-pro",
    ollama_url: str = "http://localhost:11434/api/generate",
) -> Dict:
    base_dir = os.path.dirname(os.path.abspath(content_list_path))
    with open(content_list_path, encoding="utf-8") as f:
        items: List[Dict] = json.load(f)

    lang = detect_lang(items)
    epub_lang = "zh-CN" if lang == "zh" else "en"
    total_pages = max((int(x.get("page_idx", 0)) for x in items), default=0) + 1

    # 统计原始有效文本字符数（排除 MinerU 已识别的页眉/页脚/页码）
    discarded_types = {"header", "footer", "page_number", "page_footnote_line"}
    raw_valid_chars = 0

    units: List[SectionUnit] = []
    cur_unit = SectionUnit(unit_id="ch000", title="卷首" if lang == "zh" else "Front Matter", kind="preface", level=1)
    seen_special_once: Set[str] = set()
    image_assets: List[Tuple[str, str]] = []  # (abs_src_path, epub_rel_path)

    def push_unit():
        nonlocal cur_unit
        if cur_unit.blocks or cur_unit.footnotes or cur_unit.kind == "part":
            units.append(cur_unit)
        cur_unit = SectionUnit(
            unit_id=f"ch{len(units) + 1:03d}",
            title="正文" if lang == "zh" else "Chapter",
            kind="body",
            level=2,
        )

    for item in items:
        itype = item.get("type", "text")
        page_idx = int(item.get("page_idx", 0))

        # 1. 跳过页眉/页脚/页码
        if itype in discarded_types:
            continue

        # 2. 独立脚注块（MinerU 视觉坐标已精准剥离！）
        if itype == "footnote":
            fn_raw = str(item.get("text", "")).strip()
            if not fn_raw:
                continue
            raw_valid_chars += count_non_ws(fn_raw)
            mk, mkind, fn_body = parse_fn_marker(fn_raw)
            cur_unit.footnotes.append(FootnoteItem(
                unified_num=len(cur_unit.footnotes) + 1,
                unit_id=cur_unit.unit_id,
                orig_marker=mk,
                marker_kind=mkind,
                text=fn_body,
                page_idx=page_idx,
            ))
            continue

        # 3. 插图块
        if itype == "image":
            img_rel = item.get("img_path", "")
            captions = item.get("image_caption") or []
            cap_text = " ".join(str(c).strip() for c in captions if str(c).strip())
            if cap_text:
                raw_valid_chars += count_non_ws(cap_text)
            abs_img = os.path.join(base_dir, img_rel) if img_rel else ""
            if abs_img and os.path.exists(abs_img):
                epub_img_name = f"images/{os.path.basename(abs_img)}"
                image_assets.append((abs_img, epub_img_name))
                cur_unit.blocks.append({"type": "image", "src": epub_img_name, "caption": cap_text})
            elif cap_text:
                cur_unit.blocks.append({"type": "p", "text": cap_text, "page_idx": page_idx})
            continue

        # 4. 表格块（MinerU 输出 HTML table_body）
        if itype == "table":
            tbody = str(item.get("table_body", "")).strip()
            tcap = " ".join(str(c).strip() for c in (item.get("table_caption") or []) if str(c).strip())
            if tbody:
                raw_valid_chars += count_non_ws(re.sub(r"<[^>]+>", "", tbody))
                cur_unit.blocks.append({"type": "table", "html": tbody, "caption": tcap})
            continue

        # 5. 正文 / 公式 / 标题文本块
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        raw_valid_chars += count_non_ws(text)
        text_level = item.get("text_level")

        heading = classify_heading(text, text_level, page_idx, total_pages)
        if heading:
            h_kind, h_level = heading
            # 防止运行页眉残留的 "Notes" 重复切章
            if h_kind in {"toc", "endnotes", "index", "glossary", "bibliography"} and h_kind in seen_special_once:
                continue
            if h_kind in {"toc", "endnotes", "index", "glossary", "bibliography"}:
                seen_special_once.add(h_kind)
            push_unit()
            cur_unit.title = text.strip("#* ")
            cur_unit.kind = h_kind
            cur_unit.level = h_level
            continue

        # 普通段落：跨页未完结段落自动合并（如果上一段没有句末标点且本段小写开头）
        if (
            cur_unit.blocks
            and cur_unit.blocks[-1]["type"] == "p"
            and cur_unit.kind not in {"toc", "index", "bibliography", "glossary"}
            and not re.search(r"[.!?\"'”’。！？…]$", cur_unit.blocks[-1]["text"])
            and re.match(r"^[a-z\u4e00-\u9fff]", text)
        ):
            sep = "" if (lang == "zh" and re.search(r"[\u4e00-\u9fff]$", cur_unit.blocks[-1]["text"])) else " "
            cur_unit.blocks[-1]["text"] += sep + text
            cur_unit.blocks[-1]["pages"].add(page_idx)
        else:
            cur_unit.blocks.append({"type": "p", "text": text, "pages": {page_idx}})

    push_unit()

    # 6. 每章正文与章末脚注建立双向锚点占位符 {{FN:unit_id:NNN}}
    total_fn, matched_fn = 0, 0
    for u in units:
        if not u.footnotes:
            continue
        total_fn += len(u.footnotes)
        valid_nums = {fn.orig_marker for fn in u.footnotes if fn.marker_kind == "number" and fn.orig_marker}
        has_sym = any(fn.marker_kind == "symbol" for fn in u.footnotes)

        for blk in u.blocks:
            if blk["type"] != "p":
                continue
            txt = blk["text"]
            p_pages = blk.get("pages", set())
            cands = []
            if valid_nums:
                for m in RE_REF_BRACKET.finditer(txt):
                    if m.group(1) in valid_nums:
                        cands.append((m.start(1) - 1, m.end(1) + 1, m.group(1), "number"))
                for m in RE_REF_CIRCLED.finditer(txt):
                    mk = CIRCLED_MAP[m.group(1)]
                    if mk in valid_nums:
                        cands.append((m.start(1), m.end(1), mk, "number"))
                for m in RE_REF_SUP.finditer(txt):
                    mk = m.group(1).translate(SUP_TRANS)
                    if mk in valid_nums:
                        cands.append((m.start(1), m.end(1), mk, "number"))
            if has_sym:
                for m in RE_REF_SYMBOL.finditer(txt):
                    cands.append((m.start(1), m.end(1), m.group(1), "symbol"))

            cands.sort(key=lambda x: x[0])
            reps = []
            last_ed = -1
            for st, ed, mk, mkind in cands:
                if st < last_ed:
                    continue
                best, best_d = None, 10 ** 9
                for fn in u.footnotes:
                    if fn.matched or fn.marker_kind != mkind:
                        continue
                    if mkind == "number" and fn.orig_marker != mk:
                        continue
                    d = min((abs(fn.page_idx - pg) for pg in p_pages), default=0) * 10 + fn.unified_num
                    if d < best_d:
                        best_d, best = d, fn
                if best:
                    best.matched = True
                    matched_fn += 1
                    reps.append((st, ed, f"{{{{FN:{u.unit_id}:{best.unified_num:03d}}}}}"))
                    last_ed = ed

            for st, ed, tok in reversed(reps):
                txt = txt[:st] + tok + txt[ed:]
            blk["text"] = txt

    # 7. 可选本地大模型精修
    if use_qwen:
        refine_units_with_ollama(units, model=qwen_model, url=ollama_url, lang=lang)

    # 8. 生成标准 EPUB3
    css = """
    body { font-family: Georgia, "Songti SC", "Noto Serif CJK SC", serif; line-height: 1.7; margin: 1.2em; color: #1f1f1f; }
    h1 { font-size: 1.45em; text-align: center; margin: 1.3em 0 1em; }
    h2 { font-size: 1.1em; margin: 1.2em 0 0.5em; }
    p { text-indent: 1.8em; margin: 0.45em 0; text-align: justify; }
    p.toc-line { text-indent: 0; border-bottom: 1px dotted #ddd; padding-bottom: 0.15em; }
    figure { margin: 1em 0; text-align: center; }
    figure img { max-width: 100%; height: auto; }
    figcaption { font-size: 0.85em; color: #555; margin-top: 0.3em; }
    table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 0.9em; }
    td, th { border: 1px solid #ccc; padding: 0.35em 0.5em; }
    sup.fn-sup { font-size: 0.72em; line-height: 0; vertical-align: super; }
    a.noteref, a.backref { text-decoration: none; color: #1a56db; }
    section.chapter-notes { margin-top: 2em; padding-top: 0.8em; border-top: 1px solid #999; font-size: 0.88em; color: #333; }
    aside.fn-item p { text-indent: 0; margin: 0.25em 0; }
    """

    book_uid = f"urn:uuid:{uuid.uuid4()}"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    title_esc = html.escape(title)
    author_esc = html.escape(author)

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style/main.css" media-type="text/css"/>',
    ]
    spine = []
    nav_items = []
    xhtml_files = []

    for idx_img, (_, rel_p) in enumerate(dict.fromkeys(image_assets)):
        ext = os.path.splitext(rel_p)[1].lower()
        mtype = "image/png" if ext == ".png" else "image/jpeg"
        manifest.append(f'<item id="img{idx_img}" href="{rel_p}" media-type="{mtype}"/>')

    for u in units:
        t_esc = html.escape(u.title)
        parts = [f"<h1>{t_esc}</h1>"]
        for blk in u.blocks:
            bt = blk["type"]
            if bt == "p":
                esc = html.escape(blk["text"])
                esc = re.sub(
                    r"\{\{FN:([a-zA-Z0-9_-]+):(\d+)\}\}",
                    lambda m: (
                        f'<sup class="fn-sup"><a id="{m.group(1)}-ref-{int(m.group(2)):03d}" '
                        f'href="#{m.group(1)}-fn-{int(m.group(2)):03d}" epub:type="noteref" '
                        f'role="doc-noteref" class="noteref">[{int(m.group(2))}]</a></sup>'
                    ),
                    esc,
                )
                cls = ' class="toc-line"' if u.kind == "toc" else ""
                parts.append(f"<p{cls}>{esc}</p>")
            elif bt == "image":
                cap = f"<figcaption>{html.escape(blk['caption'])}</figcaption>" if blk.get("caption") else ""
                parts.append(f'<figure><img src="{html.escape(blk["src"])}" alt=""/>{cap}</figure>')
            elif bt == "table":
                cap = f"<p><strong>{html.escape(blk['caption'])}</strong></p>" if blk.get("caption") else ""
                parts.append(cap + blk["html"])

        if u.footnotes:
            nh = "本章注释" if lang == "zh" else "Notes"
            parts.append(f'<section class="chapter-notes" epub:type="endnotes" role="doc-endnotes"><h2>{nh}</h2>')
            for fn in u.footnotes:
                fn_id = f"{u.unit_id}-fn-{fn.unified_num:03d}"
                ref_id = f"{u.unit_id}-ref-{fn.unified_num:03d}"
                back = (
                    f'<a href="#{ref_id}" class="backref" role="doc-backlink">[{fn.unified_num}] ↩</a> '
                    if fn.matched else f"<strong>[{fn.unified_num}]</strong> "
                )
                parts.append(
                    f'<aside id="{fn_id}" epub:type="footnote" role="doc-footnote" class="fn-item">'
                    f"<p>{back}{html.escape(fn.text)}</p></aside>"
                )
            parts.append("</section>")

        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{epub_lang}">\n'
            f'<head><meta charset="utf-8"/><title>{t_esc}</title>'
            f'<link rel="stylesheet" type="text/css" href="style/main.css"/></head>\n'
            f'<body>\n' + "\n".join(parts) + "\n</body></html>\n"
        )
        ET.fromstring(xhtml.encode("utf-8"))
        fname = f"{u.unit_id}.xhtml"
        xhtml_files.append((fname, xhtml))
        manifest.append(f'<item id="{u.unit_id}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{u.unit_id}"/>')
        nav_items.append(f'  <li><a href="{fname}">{t_esc}</a></li>')

    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{epub_lang}">\n'
        f'<head><meta charset="utf-8"/><title>{title_esc}</title></head>\n'
        f'<body><nav epub:type="toc" id="toc"><h1>{"目录" if lang == "zh" else "Contents"}</h1>'
        f'<ol>\n' + "\n".join(nav_items) + "\n</ol></nav></body></html>\n"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{book_uid}</dc:identifier>\n'
        f'    <dc:title>{title_esc}</dc:title>\n'
        f'    <dc:creator>{author_esc}</dc:creator>\n'
        f'    <dc:language>{epub_lang}</dc:language>\n'
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n    ' + "\n    ".join(manifest) + '\n  </manifest>\n'
        '  <spine>\n    ' + "\n    ".join(spine) + '\n  </spine>\n'
        '</package>\n'
    )
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n'
    )

    with zipfile.ZipFile(out_epub, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style/main.css", css, compress_type=zipfile.ZIP_DEFLATED)
        for fname, xhtml in xhtml_files:
            zf.writestr(f"OEBPS/{fname}", xhtml, compress_type=zipfile.ZIP_DEFLATED)
        for abs_src, rel_dst in dict.fromkeys(image_assets):
            zf.write(abs_src, f"OEBPS/{rel_dst}", compress_type=zipfile.ZIP_DEFLATED)

    # 9. 解压校验字符留存率
    epub_chars = 0
    with zipfile.ZipFile(out_epub, "r") as zf:
        for fname, _ in xhtml_files:
            root = ET.fromstring(zf.read(f"OEBPS/{fname}"))
            body = root.find("{http://www.w3.org/1999/xhtml}body")
            epub_chars += count_non_ws("".join(body.itertext()))

    report = {
        "output_epub": out_epub,
        "chapters": len(units),
        "footnotes_total": total_fn,
        "footnotes_matched": matched_fn,
        "images_embedded": len(dict.fromkeys(image_assets)),
        "mineru_valid_chars": raw_valid_chars,
        "epub_extracted_chars": epub_chars,
        "retention_pct": round(epub_chars / max(raw_valid_chars, 1) * 100, 2),
    }
    with open(os.path.splitext(out_epub)[0] + "_audit.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("转换完成:", json.dumps(report, ensure_ascii=False))
    return report


def main():
    ap = argparse.ArgumentParser(description="MinerU content_list.json -> 标准双链 EPUB3")
    ap.add_argument("content_list_json", help="MinerU 输出的 *_content_list.json 路径")
    ap.add_argument("-o", "--output", default=None, help="输出 EPUB 路径")
    ap.add_argument("--title", default=None, help="书名")
    ap.add_argument("--author", default="Unknown", help="作者")
    ap.add_argument("--qwen-refine", action="store_true", help="调用本地 Ollama 校对残留错字")
    ap.add_argument("--qwen-model", default="qwen14b-pro")
    args = ap.parse_args()

    base = os.path.splitext(args.content_list_json)[0].replace("_content_list", "")
    out_epub = args.output or (base + ".epub")
    title = args.title or os.path.basename(base)
    convert_mineru_to_epub(
        args.content_list_json, out_epub, title=title, author=args.author,
        use_qwen=args.qwen_refine, qwen_model=args.qwen_model,
    )


if __name__ == "__main__":
    main()
