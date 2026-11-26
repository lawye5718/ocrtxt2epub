#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp/server.py - PDF 批量上传 → MinerU 解析 → EPUB（断点续传版）
特性:
  1. 批量上传多 PDF，服务端队列串行处理（MPS 显存一次一个）
  2. 断点续传: 状态持久化 tasks/<id>/state.json；重启后
     - output.epub 已存在 → 直接标记完成
     - *_content_list.json 已存在 → 跳过解析，仅重新转换
     - 否则从解析重新开始
  3. GET /api/tasks 全量任务列表，前端刷新后自动恢复
启动: python3 -m uvicorn server:app --host 0.0.0.0 --port 8765
"""
import json, os, re, sys, time, uuid, threading, queue, shutil, traceback, subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mineru2epub import convert_mineru_to_epub

MINERU_BIN = Path.home() / 'tools/mineru_env/.venv/bin/mineru'
WORK_DIR = Path(__file__).parent / 'tasks'
WORK_DIR.mkdir(exist_ok=True)
UPLOAD_LIMIT = 200 * 1024 * 1024

app = FastAPI(title='PDF → MinerU → EPUB (batch)')
_tasks: dict = {}          # task_id -> dict（内存镜像，重启从磁盘恢复）
_q: queue.Queue = queue.Queue()
_worker = None
_dirty = {}                # 写盘节流
LAST_WRITE = 0


def clean_name(stem: str) -> str:
    return re.sub(r'\s*\((?:z-library[^)]*|1lib[^)]*|z-lib[^)]*)\)', '', stem).strip()


def save_state(task_id: str, force=False):
    """状态持久化（0.5s 节流）"""
    global LAST_WRITE
    now = time.time()
    if not force and now - LAST_WRITE < 0.5:
        _dirty[task_id] = True
        return
    LAST_WRITE = now
    _dirty.pop(task_id, None)
    try:
        with open(WORK_DIR / task_id / 'state.json', 'w', encoding='utf-8') as f:
            json.dump(_tasks[task_id], f, ensure_ascii=False)
    except Exception:
        pass


def flush_dirty():
    for tid in list(_dirty):
        if tid in _tasks:
            save_state(tid, force=True)


def set_p(task_id, stage, pct, detail='', eta=None, state=None):
    st = _tasks[task_id]
    st.update(stage=stage, percent=pct, detail=detail, eta_s=eta)
    if state:
        st['state'] = state
    save_state(task_id)


def run_pipeline(task_id: str):
    """worker 线程主流程（支持断点）"""
    st = _tasks[task_id]
    d = WORK_DIR / task_id
    pdf = d / 'input.pdf'
    out_epub = d / 'output.epub'
    t0 = time.time()
    env = {**os.environ, 'MINERU_MODEL_SOURCE': 'modelscope'}

    try:
        pages = st.get('pages') or 0
        content_list = None

        # ── 断点1: EPUB 已生成 → 直接完成 ──
        if out_epub.exists() and st.get('state') == 'done':
            return

        # ── 断点2: content_list 已存在 → 跳过解析 ──
        old_out = list(d.rglob('*_content_list.json')) + \
                  list((d / 'mineru_out').rglob('*_content_list.json')) if (d / 'mineru_out').exists() else \
                  list(d.rglob('*_content_list.json'))
        old_out = [p for p in old_out if p.exists()]
        if old_out:
            content_list = old_out[0]
            set_p(task_id, '转换EPUB', 82, '检测到解析缓存，跳过解析')

        if content_list is None:
            # ── 阶段1: MinerU 解析 (2-80%) ──
            if not pages:
                try:
                    import pypdf
                    pages = len(pypdf.PdfReader(str(pdf)).pages)
                except Exception:
                    pages = 0
                st['pages'] = pages
            set_p(task_id, 'MinerU解析', 2, f'{pages} 页' if pages else '',
                  eta=pages * 10 if pages else None, state='running')
            cmd = [str(MINERU_BIN), '-p', str(pdf), '-o', str(d / 'mineru_out'),
                   '-b', 'vlm-transformers']
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
            done_pages, cur_tot, phase, last_pct = 0, None, 'vlm', 2
            for line in proc.stdout:
                m = re.search(r'(\d+)%\|[^|]*\|\s*(\d+)/(\d+)', line)
                if not m:
                    continue
                pct, n, tot = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if tot > pages or phase == 'post':
                    phase = 'post'
                    last_pct = 60 + int(pct * 0.18)
                    detail = f'{n}/{tot} 块'
                elif tot == pages:
                    detail = f'{n}/{tot} 页'
                    last_pct = 5 + int(pct * 0.55)
                else:
                    if tot != cur_tot:
                        done_pages += cur_tot or 0
                        cur_tot = tot
                    total_done = min(done_pages + n, pages)
                    detail = f'{total_done}/{pages} 页'
                    last_pct = 5 + int(total_done / max(pages, 1) * 55)
                elapsed = time.time() - t0
                frac = (last_pct - 2) / 78
                set_p(task_id, 'MinerU解析', last_pct, detail,
                      eta=int(elapsed / max(frac, 0.01) * (1 - frac)) if frac > 0.05 else None)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f'MinerU 解析失败 (exit {proc.returncode})')
            cls = list((d / 'mineru_out').rglob('*_content_list.json'))
            if not cls:
                raise RuntimeError('MinerU 未生成 content_list.json')
            content_list = cls[0]

        # ── 阶段2: 转 EPUB (82-98%) ──
        title = clean_name(pdf.stem)
        set_p(task_id, '转换EPUB', 85, '', state='running')
        rep = convert_mineru_to_epub(str(content_list), str(out_epub),
                                     title=title, author='Unknown')
        set_p(task_id, '完成', 100, '', eta=0, state='done')
        st.update(elapsed_s=int(time.time() - t0),
                  download=f'/api/download/{task_id}', audit=rep)
        save_state(task_id, force=True)

    except Exception as e:
        st.update(state='error', stage='失败',
                  error=f'{type(e).__name__}: {e}',
                  trace=traceback.format_exc()[-800:])
        save_state(task_id, force=True)


def worker_loop():
    global _worker
    while True:
        task_id = _q.get()
        try:
            run_pipeline(task_id)
        finally:
            flush_dirty()
            _q.task_done()


def start_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=worker_loop, daemon=True)
        _worker.start()


def resume_tasks():
    """启动恢复：磁盘 state.json → 内存；未完成任务重新入队"""
    if not WORK_DIR.exists():
        return
    for d in sorted(WORK_DIR.iterdir()):
        sj = d / 'state.json'
        if not sj.exists():
            continue
        try:
            st = json.load(open(sj, encoding='utf-8'))
        except Exception:
            continue
        tid = d.name
        _tasks[tid] = st
        if st.get('state') in ('queued', 'running'):
            if not (d / 'input.pdf').exists():
                st['state'] = 'error'; st['error'] = 'input.pdf 丢失'
                continue
            st['state'] = 'queued'
            st['stage'] = '排队中(断点恢复)'
            save_state(tid, force=True)
            _q.put(tid)


@app.on_event('startup')
def on_startup():
    resume_tasks()
    start_worker()


@app.post('/api/upload')
async def upload(file: UploadFile = File(...)):
    name = file.filename or 'book.pdf'
    if not name.lower().endswith('.pdf'):
        raise HTTPException(400, '仅支持 .pdf 文件')
    task_id = uuid.uuid4().hex[:12]
    d = WORK_DIR / task_id
    d.mkdir()
    pdf = d / 'input.pdf'
    size = 0
    with open(pdf, 'wb') as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > UPLOAD_LIMIT:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(400, f'{name} 超过 200MB 限制')
            f.write(chunk)
    if size == 0:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f'{name} 是空文件')
    _tasks[task_id] = {'state': 'queued', 'stage': '排队中', 'percent': 0,
                       'filename': name, 'size': size, 'eta_s': None, 'pages': None}
    save_state(task_id, force=True)
    start_worker()
    _q.put(task_id)
    return {'task_id': task_id, 'size': size, 'filename': name}


@app.get('/api/progress/{task_id}')
def progress(task_id: str):
    st = _tasks.get(task_id)
    if not st:
        raise HTTPException(404, '任务不存在')
    return dict(st)


@app.get('/api/tasks')
def tasks():
    """全量任务（前端刷新恢复用），按时间倒序"""
    items = [{'task_id': k, **{kk: v.get(kk) for kk in
             ('state', 'stage', 'percent', 'eta_s', 'detail', 'error', 'pages',
              'filename', 'size', 'elapsed_s', 'download', 'audit')}}
             for k, v in _tasks.items()]
    return items[::-1]


@app.get('/api/download/{task_id}')
def download(task_id: str):
    out = WORK_DIR / task_id / 'output.epub'
    if not out.exists():
        raise HTTPException(404, 'EPUB 未生成')
    st = _tasks.get(task_id, {})
    name = clean_name(Path(st.get('filename', 'book')).stem)
    return FileResponse(out, media_type='application/epub+zip',
                        filename=f'{name}.epub')


@app.get('/', response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / 'index.html').read_text(encoding='utf-8')


@app.get('/favicon.ico')
def favicon():
    return HTMLResponse('', status_code=204)
