#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr2epub_fixed.py - 清洗OCR合并TXT → 分章EPUB（星号脚注双向链接）
修复自 yuanbao_python_20261125_zPGBYs.py，主要修复：
  1. is_header 的 re.IGNORECASE 灾难（原版会误删一切 ≤20 字符短行）
  2. clean_line 无词边界全局替换（witchcraft→witchheraft 类误伤）
  3. 段落重组（原版 OCR 每行成一段，阅读器里碎成沙子）
  4. 星号脚注双向链接（原版数字注号逻辑不适用本书且误伤正文数字）
  5. 按章分 XHTML + 两级 TOC（原版 2.6MB 单章违反 EPUB 惯例且卡阅读器）
  6. 章切分：Part/罗马数字章+年份行，自动识别并跳过目录区假章
用法: python ocr2epub_fixed.py 合并.txt [输出.epub]
"""
import sys, re, os
from ebooklib import epub

# ---------- OCR 词级修正（词边界整词替换，保守清单） ----------
WORD_FIX = {
    'thc':'the','tlic':'the','tliat':'that','wliich':'which','wliile':'while',
    'froin':'from','rcad':'read','wlth':'with','wlien':'when',
    'hc':'he','shc':'she','wc':'we','morc':'more','havc':'have','bavc':'have',
    'jamcs':'james','joycc':'joyce',
    'fllmann':'ellmann','ellmam':'ellmann',
    'frst':'first','preace':'preface','makerial':'material','upublished':'unpublished',
    'bicgraphy':'biography','fricnds':'friends','gencrously':'generously',
    'knowledgc':'knowledge','prescrving':'preserving','cxperiences':'experiences',
    'centcnary':'centenary','pcrplcxitics':'perplexities','infornation':'information',
    'incorporatcd':'incorporated','indispensahle':'indispensable',
    'benevalently':'benevolently','capc':'cape',
}
WORD_RE = re.compile(r'\b(' + '|'.join(re.escape(k) for k in WORD_FIX) + r')\b', re.IGNORECASE)

def fix_words(line):
    def _sub(m):
        w = m.group(1); r = WORD_FIX[w.lower()]
        return r.capitalize() if w[:1].isupper() else r
    return WORD_RE.sub(_sub, line)

# ---------- 全角/OCR 标点归一 ----------
PUNCT_MAP = str.maketrans({'，':', ','：':':','；':';','（':'(','）':')',
    '“':'"','”':'"','‘':"'",'’':"'",'．':'.','。':'.','、':', '})
def fix_punct(s):
    s = s.translate(PUNCT_MAP)
    s = re.sub(r'(\d)o(\d)', r'\g<1>0\g<2>', s)   # 19o4 → 1904
    s = re.sub(r'2oth', '20th', s); s = re.sub(r'2oth century','20th century',s)
    return s

# ---------- 行分类 ----------
RE_HEADER = re.compile(r'^(JAMES\s*JOYCE|JAMES\s*JOYCEJAMES\s*JOYCE|JOYCE|RICHARD\s*ELLMANN)$')
RE_TOCROMAN = re.compile(r'^[IVXLC]{1,7}$')
RE_PART = re.compile(r'^(PART\s+(ONE|TWO|THREE|FOUR|FIVE|SIX)\b|Part\s+(One|Two|Three|Four|Five)\b.*)$')
RE_YEAR = re.compile(r'^(\d{4})(\s*[-—:：·]\s*(\d{2,4}))?$')
RE_ROMAN = re.compile(r'^[IVXLC×x]{1,7}$')
RE_PAGENUM = re.compile(r'^(\d{1,4}|[ivxlcdm]{1,6}|\d{4,8})$', re.IGNORECASE)
RE_DECOR = re.compile(r'^([\d\s\-_=~*•·○●◎⊙⊕⊗∞⑥⑤④③②①⑨⑧⑦⑩]+)$')
RE_FN_MARK = re.compile(r'^[\*†‡](?=\S)')     # 脚注首行: 星号紧贴文字
RE_REF = re.compile(r'(?<=\S)\s([\*†‡])\s')   # 正文注号: 词-星-空格（"path. * In"）

def is_noise(line):
    if not line: return True
    if RE_HEADER.match(line): return True
    if RE_PAGENUM.match(line) and len(line)<=8: return True
    if RE_DECOR.fullmatch(line) and not re.search(r'[A-Za-z]', line): return True
    return False

# ---------- 读取与预处理 ----------
def read_lines(path):
    with open(path,'r',encoding='utf-8',errors='ignore') as f:
        raw = f.read()
    return [l.strip() for l in re.split(r'[\r\n]+', raw)]

def find_toc_region(lines):
    """目录区: 含 'Contents' 的行 → 'Introduction' 正文行（首个后接正文段落的）"""
    start = end = None
    for i,l in enumerate(lines):
        if start is None and 'Contents' in l and len(l)<30:
            start = i
        elif start is not None and l=='Introduction':
            end = i; break
        elif start is not None and i > start+300:
            end = start+100; break
    return (start, end) if start is not None and end else (None, None)

# ---------- 章切分 ----------
RE_YEAR_ANCHOR = re.compile(r'^(18[2-9]\d|19[0-4]\d)(\s*[-—:：·]\s*(18\d\d|19\d\d))?$')
RE_ROMANISH = re.compile(r'[IVXLC×￥¥1]', re.IGNORECASE)

def is_chapter_year(lines, i):
    """年份锚: 值域 1882-1941、前一行像章号(≤10字符含罗马痕迹)、该年份未出现过"""
    if i<1 or not RE_YEAR_ANCHOR.match(lines[i]) or len(lines[i])>15: return False
    prev = lines[i-1]
    if len(prev)>10 or not RE_ROMANISH.search(prev): return False
    if prev.isdigit() or re.fullmatch(r'[ivxlcdm]+', prev, re.IGNORECASE) and prev.islower():
        return False   # 纯数字页码或小写罗马页码
    return True

def find_tail_region(lines):
    """Notes/Index 区起点: 独立行后 200 行内再出现 ≥3 次（页眉重复）"""
    def locate(word):
        cands=[i for i,l in enumerate(lines) if l==word]
        for i in cands:
            after = sum(1 for j in range(i+1, min(i+500,len(lines))) if lines[j]==word)
            if after>=2: return i
        return None
    return locate('Notes'), locate('Index')

def build_structure(lines, toc_s, toc_e):
    """返回 [(title, [lines]), ...]：Preface → Contents → Introduction → 各章 → Notes → Index"""
    chapters = []
    cur_title, cur_buf = 'Preface', []
    def flush():
        nonlocal cur_title, cur_buf
        if cur_buf: chapters.append((cur_title, cur_buf)); cur_buf=[]
    i = 0
    notes_s, index_s = find_tail_region(lines)
    seen_years = set()
    ch_n = 0
    while i < len(lines):
        l = lines[i]
        if toc_s is not None and i == toc_s:
            flush(); cur_title='Contents'; cur_buf=lines[toc_s:toc_e+1]; i=toc_e+1
            flush(); cur_title='Introduction'; continue
        if notes_s is not None and i==notes_s:
            flush(); cur_title='Notes'; cur_buf=lines[notes_s:(index_s or len(lines))]
            i = index_s if index_s else len(lines)
            continue
        if index_s is not None and i==index_s:
            flush(); cur_title='Index'; cur_buf=lines[index_s:]
            i = len(lines); continue
        if i > (toc_e or 0) and is_chapter_year(lines, i):
            yr = re.sub(r'\s*[-—:：·]\s*', '-', lines[i])
            if yr not in seen_years:
                seen_years.add(yr); ch_n += 1
                flush(); cur_title=f'Chapter {ch_n}（{yr}）'; cur_buf=[l]; i+=1; continue
        cur_buf.append(l); i+=1
    flush()
    return chapters

# ---------- 段落重组 ----------
def paragraphs(buf_lines):
    """连续行→段落: 当前行以句末标点收尾 且 下一行首字母大写 → 断段"""
    text = [l for l in buf_lines if not is_noise(l) and not RE_FN_MARK.match(l)]
    paras, cur = [], ''
    for k,l in enumerate(text):
        l = fix_punct(fix_words(l))
        if not l: continue
        cur = (cur + ' ' + l).strip() if cur else l
        nxt = text[k+1] if k+1 < len(text) else ''
        if cur and re.search(r'[.!?""\']$', cur) and (not nxt or (nxt and nxt[:1].isupper())):
            paras.append(cur); cur=''
    if cur: paras.append(cur)
    return paras

# ---------- 星号脚注（按章收集 + 全局顺序配对） ----------
def extract_footnotes(buf_lines):
    """返回 (剩余正文行, [脚注文本])"""
    body, notes, i = [], [], 0
    while i < len(buf_lines):
        l = buf_lines[i]
        if RE_FN_MARK.match(l):
            note = l.lstrip('*†‡').strip()
            j = i+1
            taken = 0
            while j < len(buf_lines) and taken < 2:   # 脚注延续行最多 2 行，防吞正文
                nx = buf_lines[j]
                if is_noise(nx) or RE_FN_MARK.match(nx) or nx.strip()[:1].isupper():
                    break
                note += ' ' + nx; j += 1; taken += 1
            notes.append(note); i = j; continue
        body.append(l); i += 1
    return body, notes

def link_refs(paras, offset, fn_last):
    """正文 ' * ' → noteref；严格 clamp 在本章脚注区间 [offset+1, fn_last] 内（同文件锚）"""
    out, k = [], 0
    for p in paras:
        while True:
            m = RE_REF.search(p)
            if not m: break
            k += 1
            n = min(offset + k, fn_last)
            p = p[:m.start()+1] + f'<a href="#fn{n}" id="ref{n}" class="noteref"><sup>*</sup></a>' + p[m.end()-1:]
        out.append(p)
    return out, k

# ---------- EPUB ----------
CSS = """
body { font-family: serif; line-height: 1.6; margin: 1em; }
p { text-indent: 1.5em; margin: 0.4em 0; text-align: justify; }
h1 { font-size: 1.4em; text-align: center; margin: 1.2em 0; }
h2 { font-size: 1.1em; color: #444; text-align: center; }
.footnote { font-size: 0.85em; color: #333; border-top: 1px solid #999; margin-top: 1.5em; padding-top: 0.4em; }
.footnote p { text-indent: 0; margin: 0.25em 0; }
.noteref { text-decoration: none; font-size: 0.75em; vertical-align: super; color: #0645ad; }
.backref { text-decoration: none; color: #0645ad; margin-right: 0.4em; }
"""

def make_chapter(book, css, title, fid, paras_html):
    ch = epub.EpubHtml(title=title, file_name=f'{fid}.xhtml', lang='en')
    ch.content = f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title>'\
                 f'<link rel="stylesheet" type="text/css" href="style/main.css"/></head>'\
                 f'<body><h1>{title}</h1>{paras_html}</body></html>'
    ch.add_item(css)
    book.add_item(ch)
    return ch

def build_epub(units, out_path):
    book = epub.EpubBook()
    book.set_identifier('joyce-ellmann-ocr')
    book.set_title('James Joyce (New and Revised Edition)')
    book.set_language('en')
    book.add_author('Richard Ellmann')
    css = epub.EpubItem(uid='css', file_name='style/main.css', media_type='text/css', content=CSS)
    book.add_item(css)

    spine, toc = [], []
    # 脚注全局计数
    fn_total = 0
    chapter_items = []
    for idx,(title, paras, notes) in enumerate(units):
        html_parts = []
        fn_ids = []
        for n,note in enumerate(notes):
            fn_total += 1
            fn_ids.append(fn_total)
        # 正文引用链接（offset = 本章首个脚注号-1；无脚注章不生成链接）
        offset = fn_total - len(notes)
        if notes:
            linked, used = link_refs(paras, offset, offset + len(notes))
        else:
            linked, used = paras, 0
        for p in linked:
            html_parts.append(f'<p>{p}</p>')
        if notes:
            html_parts.append('<div class="footnote">')
            for local,(n,note) in enumerate(zip(fn_ids, notes)):
                html_parts.append(f'<p id="fn{n}"><a href="#ref{n}" class="backref">＊{local+1}</a> {note}</p>')
            html_parts.append('</div>')
        ch = make_chapter(book, css, title, f'ch{idx:02d}', '\n'.join(html_parts))
        chapter_items.append(ch); spine.append(ch); toc.append(ch)

    book.toc = tuple(toc)
    book.spine = ['nav'] + spine
    book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    epub.write_epub(out_path, book)
    return chapter_items

# ---------- 主流程 ----------
def main():
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv)>2 else os.path.splitext(inp)[0]+'_cleaned.epub'
    lines = read_lines(inp)
    toc_s, toc_e = find_toc_region(lines)
    print(f'目录区: {toc_s}~{toc_e}')

    chapters = build_structure(lines, toc_s, toc_e)
    print(f'识别章节: {len(chapters)}')
    for t,_ in chapters[:12]:
        print('  -', t)

    units = []
    for title, buf in chapters:
        body, notes = extract_footnotes(buf)
        paras = paragraphs(body)
        if paras: units.append((title or '前言', paras, notes))
    print(f'非空章节: {len(units)}; 脚注总数: {sum(len(n) for _,_,n in units)}')

    items = build_epub(units, out)
    print(f'EPUB 已生成: {out} ({len(items)} 章)')

    # 附带输出清洗后的 txt 便于人工检查
    txt_out = os.path.splitext(out)[0] + '_cleaned.txt'
    with open(txt_out,'w',encoding='utf-8') as f:
        for title, paras, notes in units:
            f.write(f'\n\n===== {title} =====\n')
            for p in paras: f.write(p+'\n\n')
    print(f'清洗文本: {txt_out}')

if __name__ == '__main__':
    main()
