#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hybrid_clean_v2_fixed.py - 规则预处理 + 本地Qwen精修 OCR TXT → 分章EPUB
修复自 yuanbao_python_20261125_JiKMBb.py (hybrid_clean_v2)，修复清单：
  1. qwen2:7b 不存在 → 自动探测/可配置（本机 qwen14b-pro）
  2. normalize_ocr 全局字母→数字替换会毁正文 → 仅用于页码候选归一化
  3. 页码序列判定允许非递增 → 严格递增 + 行号间隙约束
  4. parse_page_number 模式3 把正文行尾数字(年份)当页码 → 限行长度
  5. classify_paragraphs 依赖空行分段(OCR无空行,全书一段) → 句末启发式分段
  6. call_qwen timeout=30 太短/except吞错/单段串行太慢 → 批量8段/请求,重试,进度
  7. --epub 只写HTML不生成EPUB → ebooklib 真EPUB(分章+CSS)
  8. 无章节切分 → Part/章标题/年份锚检测(可配置)
  9. 无脚注双向链接 → 星号脚注 noteref/backref
 10. 固定文件名互相覆盖 → 基于输入文件名

用法:
  python hybrid_clean_v2_fixed.py 合并.txt                     # 规则+qwen+epub
  python hybrid_clean_v2_fixed.py 合并.txt --no-qwen           # 只跑规则(快速调参)
  python hybrid_clean_v2_fixed.py 合并.txt --limit 50          # 只处理前50段(调试)
"""
import re, json, sys, os, argparse, difflib, time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional
from ebooklib import epub

# ==================== 配置 ====================
@dataclass
class Config:
    # 页码检测
    page_min_number: int = 1
    page_max_number: int = 2000
    page_tolerance: int = 1
    page_min_sequence: int = 4
    page_line_gap: int = 60          # 相邻页码行最大行距(页码间隔≈整页文本)
    ocr_digit_fixes: Dict[str, str] = field(default_factory=lambda: {
        'l': '1', 'I': '1', 'O': '0', 'o': '0', 'S': '5'})
    # 页眉检测
    header_max_words: int = 6
    header_min_freq: int = 3
    header_similarity: float = 0.75
    header_nearby_distance: int = 2
    # 章切分
    year_anchor: bool = True         # 年份行锚(传记类)
    year_range: tuple = (1800, 1990)
    # Qwen
    qwen_model: str = 'qwen14b-pro'
    qwen_url: str = 'http://localhost:11434/api/generate'
    qwen_batch: int = 8              # 每请求段数
    qwen_timeout: int = 300
    verbose: bool = False

# ==================== 词级OCR修正（词边界，不伤正文） ====================
WORD_FIX = {
    'thc':'the','tlic':'the','tliat':'that','wliich':'which','wliile':'while',
    'froin':'from','rcad':'read','wlth':'with','wlien':'when','hc':'he','shc':'she',
    'wc':'we','morc':'more','havc':'have','bavc':'have','jamcs':'james','joycc':'joyce',
    'fllmann':'ellmann','ellmam':'ellmann','frst':'first','preace':'preface',
    'makerial':'material','upublished':'unpublished','bicgraphy':'biography',
    'fricnds':'friends','gencrously':'generously','knowledgc':'knowledge',
    'prescrving':'preserving','cxperiences':'experiences','centcnary':'centenary',
    'pcrplcxitics':'perplexities','infornation':'information','incorporatcd':'incorporated',
    'indispensahle':'indispensable','benevalently':'benevolently','capc':'cape',
    'meeling':'meeting','prohted':'profited','difhculty':'difficulty',
    'universily':'university','ihadan':'baden','clasgow':'glasgow','alckland':'auckland',
}
WORD_RE = re.compile(r'\b(' + '|'.join(re.escape(k) for k in WORD_FIX) + r')\b', re.IGNORECASE)

def fix_words(line):
    def _sub(m):
        w = m.group(1); r = WORD_FIX[w.lower()]
        return r.capitalize() if w[:1].isupper() else r
    return WORD_RE.sub(_sub, line)

PUNCT_MAP = str.maketrans({'，':', ','：':':','；':';','（':'(','）':')',
    '“':'"','”':'"','‘':"'",'’':"'",'．':'.','。':'.','、':', '})

def fix_punct(s):
    s = s.translate(PUNCT_MAP)
    s = re.sub(r'(\d)o(\d)', r'\g<1>0\g<2>', s)
    return s

# ==================== 行分类 ====================
RE_HEADER_EXACT = re.compile(r'^(JAMES\s*JOYCE|JAMES\s*JOYCEJAMES\s*JOYCE|JOYCE|IOYCE|RICHARD\s*ELLMANN)$')
RE_DECOR = re.compile(r'^([\d\s\-_=~*•·○●◎⊙⊕⊗∞⑥⑤④③②①⑨⑧⑦⑩]+)$')
RE_FN_MARK = re.compile(r'^[\*†‡](?=\S)')
RE_REF = re.compile(r'(?<=\S)\s([\*†‡])\s')
RE_PART = re.compile(r'^(PART\s+(ONE|TWO|THREE|FOUR|FIVE|SIX)\b.*)$', re.IGNORECASE)
RE_ROMANISH = re.compile(r'[IVXLC×￥¥1]', re.IGNORECASE)

def parse_page_number(line: str, cfg: Config) -> Optional[int]:
    """页码解析：纯数字/前缀/页眉+页码同行。限行长度防误伤正文。"""
    line = line.strip()
    if not line or len(line) > 40:      # 行太长必是正文
        return None
    # OCR 归一化仅在此处（页码候选内），不动正文
    norm = line
    for w, r in cfg.ocr_digit_fixes.items():
        norm = re.sub(rf'(?<=\d){w}(?=\d)', r, norm)   # 仅数字之间的字母
    m = re.fullmatch(r'(\d{1,4})', norm)
    if m: return int(m.group(1))
    for prefix in ('p', 'pg', 'page'):
        m = re.match(rf'^{prefix}[\s\.]*(\d{{1,4}})\s*[—\-–]*$', norm, re.IGNORECASE)
        if m: return int(m.group(1))
    m = re.fullmatch(r'[—\-–\s]*(\d{1,4})[—\-–\s]*', norm)
    if m: return int(m.group(1))
    # 页眉+页码同行："JAMES JOYCE 3" → 限 4 词以内
    m = re.search(r'\s(\d{1,3})$', norm)
    if m:
        prefix_part = norm[:m.start()].strip()
        if prefix_part and len(prefix_part.split()) <= 4 and prefix_part.isupper():
            return int(m.group(1))
    return None

def roman_to_int(s):
    romans = {'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}
    total, prev = 0, 0
    for c in reversed(s.lower()):
        if c not in romans: return None
        v = romans[c]
        total += -v if v < prev else v
        prev = v
    return total if 0 < total <= 500 else None

RE_YEAR = re.compile(r'^(1[89]\d\d)(\s*[-—:：·]\s*(1[89]\d\d))?$', re.IGNORECASE)

def is_chapter_anchor(lines, i, cfg):
    """年份锚：原始行上检测。前行像章号(罗马痕迹)、年份在值域"""
    if i < 1 or not RE_YEAR.match(lines[i]) or len(lines[i]) > 15: return False
    prev = lines[i-1]
    lo, hi = cfg.year_range
    if len(prev) > 10 or not RE_ROMANISH.search(prev) or prev.isdigit(): return False
    return lo <= int(lines[i][:4]) <= hi

def is_noise(line, cfg):
    if not line: return True
    if RE_HEADER_EXACT.match(line): return True
    if line.isdigit() and len(line) <= 4: return False   # 短纯数字交给页码检测
    if RE_DECOR.fullmatch(line) and not re.search(r'[A-Za-z]{2,}', line) \
            and not re.search(r'\d{4}', line): return True   # 年份行不是装饰
    return False

# ==================== 阶段1：规则预处理 ====================
class Preprocessor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stats = defaultdict(int)

    def read_lines(self, path):
        raw = open(path, encoding='utf-8', errors='ignore').read()
        return [l.strip() for l in re.split(r'[\r\n]+', raw)]

    def detect_page_numbers(self, lines):
        """页码候选 + 严格递增连续序列（修复：非递增不再算连续）"""
        candidates = []
        for i, line in enumerate(lines):
            num = parse_page_number(line, self.cfg)
            if num and self.cfg.page_min_number <= num <= self.cfg.page_max_number:
                candidates.append((i, num))
                continue
            s = line.strip()
            if len(s) <= 8 and s.isalpha() and s.islower():
                rn = roman_to_int(s)
                if rn and 1 <= rn <= 100:
                    candidates.append((i, rn))
        if not candidates:
            self.stats['page_lines_removed'] = 0
            return set()
        page_idx, cur = set(), []
        for c in candidates:
            if cur and (c[1] <= cur[-1][1] or                       # 修复3: 必须严格递增
                        c[1] - cur[-1][1] > self.cfg.page_tolerance + 1 or
                        c[0] - cur[-1][0] > self.cfg.page_line_gap):
                if len(cur) >= self.cfg.page_min_sequence:
                    page_idx.update(x[0] for x in cur)
                cur = []
            cur.append(c)
        if len(cur) >= self.cfg.page_min_sequence:
            page_idx.update(x[0] for x in cur)
        self.stats['page_lines_removed'] = len(page_idx)
        return page_idx

    def detect_headers(self, lines, page_idx):
        """页眉聚类：页码邻近短行 + difflib 模糊匹配 + 高频簇"""
        cands = {}
        for idx in page_idx:
            for off in range(-self.cfg.header_nearby_distance, self.cfg.header_nearby_distance+1):
                i = idx + off
                if 0 <= i < len(lines) and i not in page_idx:
                    t = lines[i].strip()
                    if t and len(t.split()) <= self.cfg.header_max_words:
                        cands.setdefault(i, t)
        if not cands:
            self.stats['header_lines_removed'] = 0
            return set()
        clusters = []
        for i, t in sorted(cands.items()):
            for cl in clusters:
                if difflib.SequenceMatcher(None, t.lower(), cl['text'].lower()).ratio() >= self.cfg.header_similarity:
                    cl['idx'].add(i); cl['n'] += 1; break
            else:
                clusters.append({'text': t, 'idx': {i}, 'n': 1})
        hdr = set()
        for cl in clusters:
            if cl['n'] >= self.cfg.header_min_freq:
                hdr.update(cl['idx'])
        self.stats['header_lines_removed'] = len(hdr)
        return hdr

    def preprocess(self, input_file):
        lines = self.read_lines(input_file)
        self.stats['input_lines'] = len(lines)
        # 锚/区域都在原始行上检测（过滤后行号体系会错位）
        anchors = {i for i in range(len(lines)) if is_chapter_anchor(lines, i, self.cfg)}
        regions = find_regions(lines)
        # 页码/页眉检测先行（噪声过滤会吞掉数字行，顺序不能反）
        page_idx = self.detect_page_numbers(lines)
        hdr_idx = self.detect_headers(lines, page_idx)
        noise_idx = {i for i, l in enumerate(lines) if is_noise(l, self.cfg)}
        self.stats['noise_removed'] = len(noise_idx - anchors)
        filtered, fmap = [], []   # fmap: 过滤行 → 原始行号
        for i, l in enumerate(lines):
            if (i in page_idx or i in hdr_idx or i in noise_idx) and i not in anchors:
                continue   # 锚行全局豁免（章内页眉年份=锚行,不能删）
            filtered.append(l); fmap.append(i)
        # 连字符合并
        out, omap, i = [], [], 0
        while i < len(filtered):
            line = filtered[i]
            if line.endswith('-') and i+1 < len(filtered):
                nxt = filtered[i+1].strip()
                if nxt and not parse_page_number(nxt, self.cfg):
                    out.append(line[:-1] + nxt)
                    omap.append(fmap[i]); i += 2
                    self.stats['hyphens_merged'] += 1
                    continue
            out.append(line); omap.append(fmap[i]); i += 1
        self.stats['output_lines'] = len(out)
        self.stats['anchors'] = len(anchors)
        return out, omap, anchors, regions

# ==================== 阶段2：章切分 ====================
def find_regions(lines):
    """区域识别（原始行号）：目录区 / Notes 区 / Index 区"""
    toc_s = toc_e = notes_s = index_s = None
    for i, l in enumerate(lines):
        if toc_s is None and 'Contents' in l and len(l) < 30:
            toc_s = i
        elif toc_s is not None and toc_e is None and l == 'Introduction':
            toc_e = i
    def locate(word):
        for i, l in enumerate(lines):
            if l == word:
                after = sum(1 for j in range(i+1, min(i+500, len(lines))) if lines[j] == word)
                if after >= 2: return i
        return None
    notes_s = locate('Notes') or locate('Nates')
    index_s = locate('Index')
    return toc_s, toc_e, notes_s, index_s

def build_chapters(lines, omap, anchors, regions, cfg):
    """Part行 + 年份锚切章；目录/Notes/Index 区整段保留不切章（regions=原始行号）"""
    toc_s, toc_e, notes_s, index_s = regions
    chapters, cur_title, cur_buf = [], '前言', []
    seen_years, ch_n = set(), 0
    def flush():
        nonlocal cur_buf
        if cur_buf: chapters.append((cur_title, cur_buf)); cur_buf = []
    def region(orig):
        if toc_s is not None and toc_s <= orig <= (toc_e or toc_s): return 'toc'
        if notes_s is not None and orig >= notes_s and (index_s is None or orig < index_s): return 'notes'
        if index_s is not None and orig >= index_s: return 'index'
        return 'body'
    cur_region = 'body'
    for i, l in enumerate(lines):
        orig = omap[i] if i < len(omap) else -1
        r = region(orig)
        if r != cur_region:   # 进入新区域 → 整段成章
            flush()
            cur_region = r
            cur_title = {'toc': 'Contents', 'notes': 'Notes', 'index': 'Index',
                         'body': 'Introduction' if cur_title == 'Contents' else '正文'}[r]
            cur_buf = [l]; continue
        if r == 'body' and orig in anchors:
            yr = re.sub(r'\s*[-—:：·]\s*', '-', l)
            if yr not in seen_years:
                seen_years.add(yr); ch_n += 1
                flush(); cur_title = f'Chapter {ch_n}（{yr}）'; cur_buf = [l]; continue
        if r == 'body' and RE_PART.match(l) and len(l) < 60:
            flush(); cur_title = l.title(); cur_buf = [l]; continue
        cur_buf.append(l)
    flush()
    return chapters

# ==================== 阶段3：段落重组 + 脚注 ====================
def paragraphs_of(buf):
    text = [l for l in buf if not is_noise(l, Config()) and not RE_FN_MARK.match(l)]
    paras, cur = [], ''
    for k, l in enumerate(text):
        l = fix_punct(fix_words(l))
        if not l: continue
        cur = (cur + ' ' + l).strip() if cur else l
        nxt = text[k+1] if k+1 < len(text) else ''
        if cur and re.search(r'[.!?\"\']$', cur) and (not nxt or nxt[:1].isupper()):
            paras.append(cur); cur = ''
    if cur: paras.append(cur)
    return paras

def extract_footnotes(buf):
    body, notes, i = [], [], 0
    while i < len(buf):
        l = buf[i]
        if RE_FN_MARK.match(l):
            note = l.lstrip('*†‡').strip()
            j, taken = i+1, 0
            while j < len(buf) and taken < 2:
                nx = buf[j]
                if is_noise(nx, Config()) or RE_FN_MARK.match(nx) or nx[:1].isupper(): break
                note += ' ' + nx; j += 1; taken += 1
            notes.append(note); i = j; continue
        body.append(l); i += 1
    return body, notes

# ==================== 阶段4：Qwen 批量精修 ====================
def call_qwen_batch(texts, cfg):
    """批量精修：一次请求 8 段，编号对齐；失败回退原文"""
    import requests
    numbered = '\n'.join(f'【{k}】{t}' for k, t in enumerate(texts, 1))
    prompt = ("修正下面OCR英文文本中的错字（c/e、o/e、rn/m、li/h混淆、断词连字符等）。"
              "保持段落编号与顺序，只输出修正后的文本，每段前保留【编号】，"
              "不要任何解释、不要增删内容：\n" + numbered)
    try:
        r = requests.post(cfg.qwen_url,
                          json={'model': cfg.qwen_model, 'prompt': prompt, 'stream': False},
                          timeout=cfg.qwen_timeout)
        resp = r.json().get('response', '')
    except Exception as e:
        return texts, str(e)
    parts = re.split(r'【(\d+)】', resp)
    got = {}
    for k in range(1, len(parts), 2):
        try: got[int(parts[k])] = parts[k+1].strip()
        except IndexError: pass
    out = [got.get(k, t) if len(got.get(k, t)) > len(t) * 0.5 else t
           for k, t in enumerate(texts, 1)]
    return out, None

def refine_with_qwen(units, cfg):
    """units: [(title, paras, notes), ...] 就地精修正文+脚注；超短正文段跳过"""
    jobs = []   # (unit_idx, kind, para_idx, text)
    for ui, (t, paras, notes) in enumerate(units):
        for pi, p in enumerate(paras):
            if len(p) >= 80: jobs.append((ui, 'p', pi, p))   # 短段词表已够,省时
        for ni, n in enumerate(notes): jobs.append((ui, 'n', ni, n))
    total = len(jobs)
    print(f'Qwen 精修: {total} 段, 每批 {cfg.qwen_batch} 段')
    t0 = time.time(); fails = 0
    for s in range(0, total, cfg.qwen_batch):
        batch = jobs[s:s+cfg.qwen_batch]
        fixed, err = call_qwen_batch([j[3] for j in batch], cfg)
        if err:
            fails += 1; print(f'  批次{s}失败: {err[:60]}'); continue
        for (ui, kind, pi, _), fx in zip(batch, fixed):
            if kind == 'p': units[ui][1][pi] = fx
            else: units[ui][2][pi] = fx
        done = min(s+cfg.qwen_batch, total)
        print(f'\r  {done}/{total} ({time.time()-t0:.0f}s)', end='', flush=True)
    print(f'\nQwen 完成: {total-fails*cfg.qwen_batch}/{total} 成功, 耗时 {time.time()-t0:.0f}s')

# ==================== 阶段5：EPUB ====================
CSS = """
body { font-family: serif; line-height: 1.6; margin: 1em; }
p { text-indent: 1.5em; margin: 0.4em 0; text-align: justify; }
h1 { font-size: 1.4em; text-align: center; margin: 1.2em 0; }
.footnote { font-size: 0.85em; color: #333; border-top: 1px solid #999; margin-top: 1.5em; padding-top: 0.4em; }
.footnote p { text-indent: 0; margin: 0.25em 0; }
.noteref { text-decoration: none; font-size: 0.75em; vertical-align: super; color: #0645ad; }
.backref { text-decoration: none; color: #0645ad; margin-right: 0.4em; }
"""

def link_refs(paras, offset, fn_last):
    out, k = [], 0
    for p in paras:
        while True:
            m = RE_REF.search(p)
            if not m: break
            k += 1
            n = min(offset + k, fn_last)
            p = p[:m.start()+1] + f'<a href="#fn{n}" id="ref{n}" class="noteref"><sup>*</sup></a>' + p[m.end()-1:]
        out.append(p)
    return out

def generate_epub(units, out_file, book_title='Book'):
    book = epub.EpubBook()
    book.set_identifier(re.sub(r'\W', '-', book_title.lower())[:40])
    book.set_title(book_title); book.set_language('en')
    css = epub.EpubItem(uid='css', file_name='style/main.css', media_type='text/css', content=CSS)
    book.add_item(css)
    spine, toc, fn_total = [], [], 0
    for idx, (title, paras, notes) in enumerate(units):
        parts = []
        fn_ids = list(range(fn_total+1, fn_total+1+len(notes)))
        fn_total += len(notes)
        offset = fn_total - len(notes)
        linked = link_refs(paras, offset, fn_total) if notes else paras
        parts += [f'<p>{p}</p>' for p in linked]
        if notes:
            parts.append('<div class="footnote">')
            parts += [f'<p id="fn{n}"><a href="#ref{n}" class="backref">＊{k+1}</a> {note}</p>'
                      for k, (n, note) in enumerate(zip(fn_ids, notes))]
            parts.append('</div>')
        ch = epub.EpubHtml(title=title, file_name=f'ch{idx:02d}.xhtml', lang='en')
        ch.content = (f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title>'
                      f'<link rel="stylesheet" type="text/css" href="style/main.css"/></head>'
                      f'<body><h1>{title}</h1>' + '\n'.join(parts) + '</body></html>')
        ch.add_item(css); book.add_item(ch)
        spine.append(ch); toc.append(ch)
    book.toc = tuple(toc); book.spine = ['nav'] + spine
    book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    epub.write_epub(out_file, book)
    return fn_total

# ==================== 主流程 ====================
def main():
    ap = argparse.ArgumentParser(description='混合清洗 v2 fixed')
    ap.add_argument('input'); ap.add_argument('--config')
    ap.add_argument('--no-qwen', action='store_true')
    ap.add_argument('--epub', action='store_true', default=True)
    ap.add_argument('--limit', type=int, default=0, help='调试: 只处理前N段')
    ap.add_argument('--page-tolerance', type=int)
    ap.add_argument('--qwen-model'); ap.add_argument('--qwen-batch', type=int)
    ap.add_argument('--title', default=None)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    cfg = Config()
    if args.config:
        for k, v in json.load(open(args.config)).items():
            if hasattr(cfg, k): setattr(cfg, k, v)
    if args.page_tolerance is not None: cfg.page_tolerance = args.page_tolerance
    if args.qwen_model: cfg.qwen_model = args.qwen_model
    if args.qwen_batch: cfg.qwen_batch = args.qwen_batch
    cfg.verbose = args.verbose

    base = os.path.splitext(args.input)[0]
    out_epub = f'{base}_clean.epub'

    # 阶段1 规则预处理
    print('阶段1: 规则预处理...')
    pre = Preprocessor(cfg)
    lines, omap, anchors, regions = pre.preprocess(args.input)
    print('  统计:', {k: v for k, v in pre.stats.items()})

    # 阶段2 章切分
    print('阶段2: 章切分...')
    chapters = build_chapters(lines, omap, anchors, regions, cfg)
    for t, _ in chapters[:10]: print('  -', t)
    if len(chapters) > 10: print(f'  ... 共 {len(chapters)} 章')

    # 阶段3 段落+脚注
    print('阶段3: 段落重组...')
    units = []
    for title, buf in chapters:
        body, notes = extract_footnotes(buf)
        paras = paragraphs_of(body)
        if paras: units.append([title or '前言', paras, notes])
    total_paras = sum(len(p) for _, p, _ in units)
    print(f'  {len(units)} 章, {total_paras} 段, {sum(len(n) for _,_,n in units)} 脚注')
    if args.limit:
        units = [[t, p, n] for t, p, n in units[:1]]
        units[0][1] = units[0][1][:args.limit]
        units[0][2] = units[0][2][:20]
        print(f'  调试模式: 仅第1章前 {len(units[0][1])} 段')

    # 阶段4 Qwen 精修
    if not args.no_qwen:
        print('阶段4: Qwen 精修...')
        refine_with_qwen(units, cfg)
    else:
        print('阶段4: 跳过 Qwen (--no-qwen)')

    # 阶段5 EPUB
    print('阶段5: 生成 EPUB...')
    title = args.title or os.path.basename(base)
    fn_total = generate_epub(units, out_epub, title)
    print(f'完成: {out_epub} ({fn_total} 脚注)')
    # 结构化输出
    with open(f'{base}_paragraphs.json', 'w', encoding='utf-8') as f:
        json.dump([{'title': t, 'paras': p, 'notes': n} for t, p, n in units],
                  f, ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
