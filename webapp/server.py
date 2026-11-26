#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webapp/server.py - PDF 上传 → MinerU 解析 → mineru2epub 转换 → EPUB 下载
进度: GET /api/progress/{task_id}  (阶段/百分比/ETA)
下载: GET /api/download/{task_id}
启动: python3 -m uvicorn server:app --host 0.0.0.0 --port 8765
"""
import os, re, sys, time, uuid, threading, shutil, traceback, subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ocr2epub 根目录
from mineru2epub import convert_mineru_to_epub

MINERU_BIN = Path.home() / 'tools/mineru_env/.venv/bin/mineru'
WORK_DIR = Path(__file__).parent / 'tasks'
WORK_DIR.mkdir(exist_ok=True)
UPLOAD_LIMIT = 200 * 1024 * 1024  # 200MB

app = FastAPI(title='PDF → MinerU → EPUB')
_tasks: dict = {}   # task_id -> dict


def count_pdf_pages(pdf: Path) -> int:
    try:
        import pypdf
        return len(pypdf.PdfReader(str(pdf)).pages)
    except Exception:
        return 0


def run_pipeline(task_id: str, pdf: Path, out_epub: Path):
    st = _tasks[task_id]
    t0 = time.time()
    env = {**os.environ, 'MINERU_MODEL_SOURCE': 'modelscope'}
    workdir = pdf.parent / 'mineru_out'

    def set_p(stage, pct, detail='', eta=None):
        st.update(stage=stage, percent=pct, detail=detail, eta_s=eta)

    try:
        st.update(state='running', stage='检查PDF', percent=1, eta_s=None)
        pages = count_pdf_pages(pdf)
        st['pages'] = pages

        # ── 阶段1: MinerU 解析 (2-80%) ──
        cmd = [str(MINERU_BIN), '-p', str(pdf), '-o', str(workdir), '-b', 'vlm-transformers']
        set_p('MinerU解析', 2, f'{pages} 页', eta=pages * 10 if pages else None)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env, bufsize=1)
        # 进度状态机：VLM 逐页(可能分批) → 5-60%，区块后处理 → 60-80%
        done_pages, cur_tot, phase = 0, None, 'vlm'
        last_pct = 2
        for line in proc.stdout:
            m = re.search(r'(\d+)%\|[^|]*\|\s*(\d+)/(\d+)', line)
            if not m:
                continue
            pct, n, tot = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if tot > pages or phase == 'post':       # 区块后处理
                phase = 'post'
                last_pct = 60 + int(pct * 0.18)
                detail = f'{n}/{tot} 块'
            elif tot == pages:                        # 单批全页
                detail = f'{n}/{tot} 页'
                last_pct = 5 + int(pct * 0.55)
            else:                                     # VLM 分批: 累计页数
                if tot != cur_tot:
                    done_pages += cur_tot or 0
                    cur_tot = tot
                total_done = min(done_pages + n, pages)
                detail = f'{total_done}/{pages} 页'
                last_pct = 5 + int(total_done / max(pages, 1) * 55)
            elapsed = time.time() - t0
            frac = (last_pct - 2) / 78
            st['eta_s'] = int(elapsed / max(frac, 0.01) * (1 - frac)) if frac > 0.05 else None
            set_p('MinerU解析', last_pct, detail, st['eta_s'])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f'MinerU 解析失败 (exit {proc.returncode})')

        # ── 找 content_list.json ──
        cl = list(workdir.rglob('*_content_list.json'))
        if not cl:
            raise RuntimeError('MinerU 未生成 content_list.json')
        content_list = cl[0]

        # ── 阶段2: 转 EPUB (82-97%) ──
        title = re.sub(r'\s*\((?:z-library[^)]*|1lib[^)]*|z-lib[^)]*)\)', '', pdf.stem).strip()
        set_p('转换EPUB', 85, '')
        rep = convert_mineru_to_epub(str(content_list), str(out_epub),
                                     title=title, author='Unknown')
        set_p('完成', 100, '')
        st.update(state='done', percent=100, eta_s=0,
                  elapsed_s=int(time.time() - t0),
                  download=f'/api/download/{task_id}',
                  audit=rep)

    except Exception as e:
        st.update(state='error', stage='失败',
                  error=f'{type(e).__name__}: {e}',
                  trace=traceback.format_exc()[-800:])


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
                raise HTTPException(400, '文件超过 200MB 限制')
            f.write(chunk)
    if size == 0:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, '空文件')
    _tasks[task_id] = {'state': 'queued', 'stage': '排队中', 'percent': 0,
                       'filename': name, 'size': size, 'eta_s': None}
    threading.Thread(target=run_pipeline, args=(task_id, pdf, d / 'output.epub'),
                     daemon=True).start()
    return {'task_id': task_id, 'size': size}


@app.get('/api/progress/{task_id}')
def progress(task_id: str):
    st = _tasks.get(task_id)
    if not st:
        raise HTTPException(404, '任务不存在')
    return dict(st)


@app.get('/api/download/{task_id}')
def download(task_id: str):
    out = WORK_DIR / task_id / 'output.epub'
    if not out.exists():
        raise HTTPException(404, 'EPUB 未生成')
    st = _tasks.get(task_id, {})
    name = Path(st.get('filename', 'book')).stem
    name = re.sub(r'\s*\((?:z-library[^)]*|1lib[^)]*|z-lib[^)]*)\)', '', name).strip()
    return FileResponse(out, media_type='application/epub+zip',
                        filename=f'{name}.epub')


@app.get('/', response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / 'index.html').read_text(encoding='utf-8')


@app.get('/favicon.ico')
def favicon():
    return HTMLResponse('', status_code=204)
