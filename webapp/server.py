#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp/server.py - OCR TXT → EPUB 清洗服务
上传 TXT → 规则清洗（hybrid_clean_v2_fixed 管线）→ EPUB 下载
进度实时查询：GET /api/progress/{task_id}
启动: uvicorn server:app --host 0.0.0.0 --port 8765
"""
import os, sys, time, uuid, threading, shutil, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ocr2epub 根目录

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from hybrid_clean_v2_fixed import (
    Config, Preprocessor, build_chapters, paragraphs_of,
    extract_footnotes, generate_epub, is_noise, RE_FN_MARK,
)

WORK_DIR = Path(__file__).parent / 'tasks'
WORK_DIR.mkdir(exist_ok=True)
UPLOAD_LIMIT = 100 * 1024 * 1024  # 100MB

app = FastAPI(title='OCR TXT → EPUB 清洗服务')
_tasks: dict = {}   # task_id -> dict(state, stage, percent, eta_s, ...)


def run_pipeline(task_id: str, txt_path: Path, out_path: Path):
    st = _tasks[task_id]
    st.update(state='running', stage='准备', percent=1, eta_s=None)
    t0 = time.time()
    cfg = Config()
    try:
        # 阶段权重: 预处理 0-55 / 章切分 55-62 / 段落 62-90 / EPUB 90-100
        def cb(stage, done, total):
            if total <= 0: return
            st['stage'] = stage
            st['detail'] = f'{done}/{total}'
            # 预处理内部按行进度映射到 2-55%
            frac = min(done / total, 1.0)
            st['percent'] = max(2, int(2 + frac * 53))
            elapsed = time.time() - t0
            st['eta_s'] = int(elapsed / max(frac, 0.01) * (1 - frac)) if frac > 0.05 else None

        pre = Preprocessor(cfg, progress_cb=cb)
        lines, omap, anchors, regions = pre.preprocess(str(txt_path))

        st.update(stage='章切分', percent=57, detail='')
        chapters = build_chapters(lines, omap, anchors, regions, cfg)

        st.update(stage='段落重组', percent=64, eta_s=None)
        units = []
        n_ch = len(chapters)
        for k, (title, buf) in enumerate(chapters):
            body, notes = extract_footnotes(buf)
            paras = paragraphs_of(body)
            if paras:
                units.append([title or '前言', paras, notes])
            if k % 3 == 0 or k == n_ch - 1:
                st['percent'] = 64 + int((k + 1) / n_ch * 24)
                st['detail'] = f'{k + 1}/{n_ch} 章'
                elapsed = time.time() - t0
                frac = st['percent'] / 100
                st['eta_s'] = int(elapsed / frac * (1 - frac)) if frac > 0.05 else None

        st.update(stage='生成EPUB', percent=91, detail='')
        total_fn = generate_epub(units, str(out_path), Path(txt_path).stem)
        st.update(state='done', stage='完成', percent=100, eta_s=0,
                  chapters=len(units),
                  paras=sum(len(p) for _, p, _ in units),
                  footnotes=total_fn,
                  elapsed_s=int(time.time() - t0),
                  download=f'/api/download/{task_id}')
    except Exception as e:
        st.update(state='error', stage='失败',
                  error=f'{type(e).__name__}: {e}',
                  trace=traceback.format_exc()[-800:])


@app.post('/api/upload')
async def upload(file: UploadFile = File(...)):
    name = file.filename or 'book.txt'
    if not name.lower().endswith('.txt'):
        raise HTTPException(400, '仅支持 .txt 文件')
    task_id = uuid.uuid4().hex[:12]
    d = WORK_DIR / task_id
    d.mkdir()
    txt = d / 'input.txt'
    size = 0
    with open(txt, 'wb') as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > UPLOAD_LIMIT:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(400, '文件超过 100MB 限制')
            f.write(chunk)
    if size == 0:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, '空文件')
    out = d / 'output.epub'
    _tasks[task_id] = {'state': 'queued', 'stage': '排队中', 'percent': 0,
                       'filename': name, 'size': size, 'eta_s': None}
    threading.Thread(target=run_pipeline, args=(task_id, txt, out), daemon=True).start()
    return {'task_id': task_id, 'size': size}


@app.get('/api/progress/{task_id}')
def progress(task_id: str):
    st = _tasks.get(task_id)
    if not st:
        raise HTTPException(404, '任务不存在')
    return {k: st.get(k) for k in
            ('state', 'stage', 'percent', 'eta_s', 'detail', 'error',
             'chapters', 'paras', 'footnotes', 'elapsed_s', 'download')}


@app.get('/api/download/{task_id}')
def download(task_id: str):
    out = WORK_DIR / task_id / 'output.epub'
    if not out.exists():
        raise HTTPException(404, 'EPUB 未生成')
    st = _tasks.get(task_id, {})
    name = Path(st.get('filename', 'book')).stem
    return FileResponse(out, media_type='application/epub+zip',
                        filename=f'{name}_规则清洗版.epub')


@app.get('/api/tasks')
def tasks():
    return {k: v.get('state') for k, v in _tasks.items()}


@app.get('/', response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / 'index.html').read_text(encoding='utf-8')


# 静态资源（无 JS/CSS 外链，前端全部内联，占位路由）
@app.get('/favicon.ico')
def favicon():
    return HTMLResponse('', status_code=204)
