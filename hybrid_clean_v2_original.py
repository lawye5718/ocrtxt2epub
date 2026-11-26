#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hybrid_clean_v2.py - 规则预处理 + Qwen 精修 OCR 文本，生成 EPUB
增强版：改进页码识别、页眉识别、通用性设计

用法:
  python hybrid_clean_v2.py input.txt --config config.json
  python hybrid_clean_v2.py input.txt --qwen --epub --page-tolerance 2
"""

import re
import json
import sys
import argparse
import difflib
from collections import Counter, defaultdict
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Set, Dict, Optional, Tuple

# ==================== 配置类 ====================

@dataclass
class Config:
    # 页码检测
    page_min_number: int = 1
    page_max_number: int = 9999
    page_tolerance: int = 1          # 允许跳过的页数（如缺1页仍算连续）
    page_min_sequence: int = 3       # 最少连续页数才认定为页码
    ocr_fixes: Dict[str, str] = field(default_factory=lambda: {
        'l': '1', 'I': '1', 'O': '0', 'o': '0', 'S': '5', 's': '5'
    })
    page_prefixes: List[str] = field(default_factory=lambda: [
        'p', 'pg', 'page', '—', '-', '—', '–'
    ])
    
    # 页眉检测
    header_max_words: int = 6
    header_min_freq: int = 2         # 出现次数阈值（降低以捕获低频页眉）
    header_similarity: float = 0.8   # 模糊匹配相似度
    header_nearby_distance: int = 2  # 页码上下行范围（扩大以捕获同行/隔行）
    
    # 脚注
    footnote_patterns: List[str] = field(default_factory=lambda: [
        r'^\d+[\.\)\]\s]',
        r'^[*\†\‡\§\¶\#]\s',
        r'^\(\d+\)',
        r'^\[\d+\]'
    ])
    
    # 通用
    chunk_size: int = 2000           # Qwen 分块大小
    verbose: bool = False

# ==================== 工具函数 ====================

def normalize_ocr(text: str, fixes: Dict[str, str]) -> str:
    """应用 OCR 常见字符替换"""
    for wrong, right in fixes.items():
        text = text.replace(wrong, right)
    return text

def parse_page_number(line: str, prefixes: List[str]) -> Optional[int]:
    """解析行中的页码，支持前缀和纯数字"""
    line = line.strip()
    if not line:
        return None
    # 尝试提取数字部分
    # 模式1: 纯数字
    m = re.match(r'^(\d+)$', line)
    if m:
        return int(m.group(1))
    # 模式2: 带前缀如 p. 23, page 23, - 23 -, —23—
    for prefix in prefixes:
        pattern = rf'^{re.escape(prefix)}[\s\.]*(\d+)\s*[—\-–]*$'
        m = re.match(pattern, line, re.IGNORECASE)
        if m:
            return int(m.group(1))
    # 模式3: 行末数字（如 "JAMES JOYCE 3"）
    m = re.search(r'(\d+)\s*$', line)
    if m:
        # 检查前面是否主要是字母（可能是页眉+页码同行）
        prefix_part = line[:m.start()].strip()
        if prefix_part and len(prefix_part.split()) <= 6:
            return int(m.group(1))
    return None

def roman_to_int(s: str) -> Optional[int]:
    """罗马数字转整数"""
    romans = {'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}
    total = 0
    prev = 0
    for c in reversed(s.lower()):
        if c not in romans: return None
        if romans[c] < prev: total -= romans[c]
        else: total += romans[c]
        prev = romans[c]
    return total if total > 0 else None

# ==================== 阶段1：规则预处理 ====================

class Preprocessor:
    def __init__(self, config: Config):
        self.cfg = config
        self.stats = defaultdict(int)
    
    def detect_page_numbers(self, lines: List[str]) -> Set[int]:
        """
        增强页码检测：
        1. 收集所有候选页码（孤立数字行、带前缀、同行数字）
        2. 寻找连续递增序列（允许跳页）
        3. 支持罗马数字
        """
        candidates = []  # (line_index, number, original_text)
        for i, line in enumerate(lines):
            # 先尝试阿拉伯数字
            num = parse_page_number(line, self.cfg.page_prefixes)
            if num and self.cfg.page_min_number <= num <= self.cfg.page_max_number:
                candidates.append((i, num, line.strip()))
            else:
                # 尝试罗马数字
                stripped = line.strip()
                if len(stripped) < 10 and stripped.isalpha():
                    rnum = roman_to_int(stripped)
                    if rnum and 1 <= rnum <= 100:
                        candidates.append((i, rnum, stripped))
        
        if not candidates:
            return set()
        
        # 按行号排序
        candidates.sort(key=lambda x: x[0])
        
        # 寻找连续序列（允许容忍度内的跳页）
        page_indices = set()
        sequences = []
        current_seq = []
        
        for i in range(len(candidates)):
            if not current_seq:
                current_seq.append(candidates[i])
            else:
                last_num = current_seq[-1][1]
                curr_num = candidates[i][1]
                if curr_num == last_num + 1 or curr_num <= last_num + self.cfg.page_tolerance + 1:
                    current_seq.append(candidates[i])
                else:
                    if len(current_seq) >= self.cfg.page_min_sequence:
                        sequences.append(current_seq)
                    current_seq = [candidates[i]]
        if len(current_seq) >= self.cfg.page_min_sequence:
            sequences.append(current_seq)
        
        # 合并所有序列中的行索引
        for seq in sequences:
            for item in seq:
                page_indices.add(item[0])
        
        self.stats['page_lines_removed'] = len(page_indices)
        return page_indices
    
    def detect_headers(self, lines: List[str], page_indices: Set[int]) -> Set[int]:
        """
        增强页眉检测：
        1. 收集页码附近行作为候选
        2. 使用模糊匹配聚类相似文本
        3. 高频簇判定为页眉
        """
        # 收集候选行（页码上下 distance 行内）
        candidates = []
        for idx in page_indices:
            for offset in range(-self.cfg.header_nearby_distance, self.cfg.header_nearby_distance + 1):
                i = idx + offset
                if 0 <= i < len(lines) and i not in page_indices:
                    text = lines[i].strip()
                    if text and len(text.split()) <= self.cfg.header_max_words:
                        candidates.append((i, text))
        
        if not candidates:
            return set()
        
        # 聚类相似文本
        clusters = []
        for i, text in candidates:
            matched = False
            for cluster in clusters:
                # 用 difflib 计算相似度
                if difflib.SequenceMatcher(None, text.lower(), cluster['text'].lower()).ratio() >= self.cfg.header_similarity:
                    cluster['indices'].add(i)
                    cluster['count'] += 1
                    matched = True
                    break
            if not matched:
                clusters.append({'text': text, 'indices': {i}, 'count': 1})
        
        # 选择高频簇
        header_indices = set()
        for cluster in clusters:
            if cluster['count'] >= self.cfg.header_min_freq:
                header_indices.update(cluster['indices'])
        
        self.stats['header_lines_removed'] = len(header_indices)
        return header_indices
    
    def clean_decorative(self, lines: List[str]) -> List[str]:
        """删除装饰线"""
        patterns = [
            r'^[\d\s]{5,}$',
            r'^[○●◎⊙⊕⊗⑥⑤④③②①⑨⑧⑦⑩\s]+$',
            r'^[\-_=~*•·]+$',
            r'∞',
            r'^[\W_]+$'
        ]
        cleaned = []
        for line in lines:
            if any(re.search(p, line) for p in patterns):
                self.stats['decorative_removed'] += 1
                continue
            cleaned.append(line)
        return cleaned
    
    def merge_hyphenated(self, lines: List[str]) -> List[str]:
        """合并连字符换行"""
        result = []
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            if line.endswith('-') and i+1 < len(lines):
                next_line = lines[i+1].strip()
                if next_line and not parse_page_number(next_line, self.cfg.page_prefixes):
                    result.append(line[:-1] + next_line)
                    i += 2
                    self.stats['hyphens_merged'] += 1
                    continue
            result.append(line)
            i += 1
        return result
    
    def preprocess(self, input_file: str, output_file: str) -> List[str]:
        """执行预处理"""
        with open(input_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        # 1. 装饰线
        lines = self.clean_decorative(lines)
        # 2. 页码
        page_indices = self.detect_page_numbers(lines)
        # 3. 页眉
        header_indices = self.detect_headers(lines, page_indices)
        # 4. 过滤
        filtered = []
        for i, line in enumerate(lines):
            if i in page_indices or i in header_indices:
                continue
            filtered.append(line)
        # 5. 连字符合并
        filtered = self.merge_hyphenated(filtered)
        
        # 保存
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(filtered))
        
        if self.cfg.verbose:
            print(f"预处理统计: {dict(self.stats)}")
        return filtered

# ==================== 阶段2：段落分类 ====================

def classify_paragraphs(lines: List[str], config: Config) -> List[dict]:
    """分类段落为正文/脚注"""
    paragraphs = []
    current = []
    current_type = 'body'
    
    for line in lines:
        if not line.strip():
            if current:
                text = ' '.join(current).strip()
                if text:
                    paragraphs.append({'type': current_type, 'text': text})
                current = []
                current_type = 'body'
        else:
            # 检测脚注
            is_footnote = False
            for pattern in config.footnote_patterns:
                if re.match(pattern, line.strip()):
                    is_footnote = True
                    break
            if is_footnote:
                current_type = 'footnote'
            current.append(line.strip())
    
    if current:
        text = ' '.join(current).strip()
        if text:
            paragraphs.append({'type': current_type, 'text': text})
    
    return paragraphs

# ==================== 阶段3-4：Qwen 精修（略，同前版） ====================

def call_qwen(text: str, task: str = 'clean') -> str:
    """调用本地 Qwen（简化版）"""
    import requests
    prompt = f"修正OCR错字，只输出结果：\n{text}" if task == 'clean' else text
    try:
        r = requests.post('http://localhost:11434/api/generate',
                          json={'model': 'qwen2:7b', 'prompt': prompt, 'stream': False}, timeout=30)
        return r.json().get('response', text)
    except:
        return text

def refine_with_qwen(paragraphs: List[dict], mode: str = 'footnotes') -> List[dict]:
    if mode == 'footnotes':
        targets = [p for p in paragraphs if p['type'] == 'footnote']
    else:
        targets = paragraphs
    for p in targets:
        p['text'] = call_qwen(p['text'], task='clean')
    return paragraphs

# ==================== 阶段5：生成 EPUB ====================

def generate_epub(paragraphs: List[dict], output_file: str):
    """生成 EPUB（简化）"""
    html = '<html><body>\n'
    for p in paragraphs:
        if p['type'] == 'footnote':
            html += f'<aside class="footnote">{p["text"]}</aside>\n'
        else:
            html += f'<p>{p["text"]}</p>\n'
    html += '</body></html>'
    with open(output_file.replace('.epub', '.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"生成 HTML: {output_file.replace('.epub', '.html')}")

# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description='混合清洗脚本 v2')
    parser.add_argument('input', help='输入TXT')
    parser.add_argument('--config', help='配置文件JSON')
    parser.add_argument('--qwen', action='store_true')
    parser.add_argument('--epub', action='store_true')
    parser.add_argument('--page-tolerance', type=int, default=None, help='页码跳页容忍度')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    
    # 加载配置
    config = Config()
    if args.config:
        with open(args.config, 'r') as f:
            config_dict = json.load(f)
            for k, v in config_dict.items():
                if hasattr(config, k):
                    setattr(config, k, v)
    if args.page_tolerance is not None:
        config.page_tolerance = args.page_tolerance
    if args.verbose:
        config.verbose = True
    
    # 阶段1
    preprocessor = Preprocessor(config)
    intermediate = 'intermediate.txt'
    preprocessor.preprocess(args.input, intermediate)
    
    # 阶段2
    with open(intermediate, 'r') as f:
        lines = f.readlines()
    paragraphs = classify_paragraphs(lines, config)
    
    # 阶段3-4
    if args.qwen:
        paragraphs = refine_with_qwen(paragraphs, mode='footnotes')
    
    # 阶段5
    if args.epub:
        generate_epub(paragraphs, 'output.epub')
    
    # 保存结构化
    with open('paragraphs.json', 'w', encoding='utf-8') as f:
        json.dump(paragraphs, f, ensure_ascii=False, indent=2)
    print("完成！结果在 paragraphs.json")

if __name__ == '__main__':
    main()