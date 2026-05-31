#!/usr/bin/env python3
"""
music_comment_mgr.py — 音乐备注管理应用（Web 版）
Usage: python music_comment_mgr.py ~/Music [--port 8765] [--no-browser]
Requires: pip install flask mutagen
"""

import os, sys, re, html, hashlib, threading, argparse, webbrowser, logging, subprocess, platform
from pathlib import Path
from typing import Optional
from flask import Flask, jsonify, request, make_response

from mutagen import File as MutagenFile
from mutagen.id3 import COMM
from mutagen.mp4 import MP4

# ── 抑制 Flask 请求日志 ───────────────────────────────────────────────────
logging.getLogger("werkzeug").setLevel(logging.ERROR)

AUDIO_EXTS = frozenset({".flac",".mp3",".m4a",".ogg",".opus",".ape",".wv",".wav",".aiff",".aif"})

# ── 乱码后缀清理 ──────────────────────────────────────────────────────────
def _has_cjk(s: str) -> bool:
    """字符串是否含 CJK / 日文 / 韩文字符（含半角片假名）。"""
    return any(
        '\u3000' <= c <= '\u303f' or  # CJK 标点
        '\u3040' <= c <= '\u30ff' or  # 平假名、全角片假名
        '\u4e00' <= c <= '\u9fff' or  # CJK 统一表意文字
        '\uac00' <= c <= '\ud7af' or  # 朝鲜谚文
        '\uff00' <= c <= '\uffef'      # 半角/全角字符（半角片假名在此区间）
        for c in s
    )

def _strip_garbled(text: str) -> str:
    """
    去除备注中的乱码重复段（两帧拼接或同帧内追加）。

    根因：部分软件写 tag 时同一文本写两遍——一遍 UTF-8，一遍 GBK/Shift-JIS
    字节当 Latin-1 追加。两个 COMM 帧经 \\n 拼接后在 HTML 里渲染成空格，
    造成"正确文本 乱码后缀"的视觉效果。

    两种情形均处理：
      A) 整段字符串本身即是乱码帧 → 返回空串（调用方过滤）
      B) 前半段干净 + 任意空白 + 乱码后缀 → 截断乱码部分
    """
    if not text or len(text) < 4:
        return text

    def _is_garbled(s: str) -> bool:
        """能否被某种 CJK 编码重新解码出 CJK 字符（即 Latin-1 视角下的乱码）。"""
        if not s or len(s) < 2:
            return False
        try:
            raw = s.encode('latin-1')
        except UnicodeEncodeError:
            return False   # 含 U+0100+ 真实 Unicode → 不是乱码
        if sum(1 for b in raw if b >= 0x80) < 2:
            return False   # 高字节不足，不像 CJK 多字节编码
        for enc in ('gbk', 'gb18030', 'big5', 'cp932', 'shift_jis', 'euc_jp', 'euc_kr'):
            try:
                # errors='ignore' 跳过末尾不完整的多字节序列，避免 UnicodeDecodeError
                if _has_cjk(raw.decode(enc, errors='ignore')):
                    return True
            except Exception:
                continue
        return False

    # 情形 A：整段就是乱码（独立的 COMM 帧全是 GBK/Shift-JIS as Latin-1）
    if _is_garbled(text):
        return ''

    # 情形 B：干净前缀 + 任意空白（含 \n）+ 乱码后缀
    # 使用 \s+ 匹配所有空白，包括两帧拼接产生的 \n
    for m in re.finditer(r'\s+', text):
        if _is_garbled(text[m.end():]):
            return text[:m.start()].rstrip()

    return text


# ── Comment 读写 ──────────────────────────────────────────────────────────
def _is_id3(audio) -> bool:
    from mutagen.id3 import ID3
    return hasattr(audio, "tags") and isinstance(getattr(audio, "tags", None), ID3)

def read_comment(path: Path) -> Optional[str]:
    def _clean_frame(raw: str) -> str:
        """单帧清洗：去空白 + 乱码检测。"""
        return _strip_garbled(raw.strip())

    try:
        audio = MutagenFile(path, easy=False)
        if audio is None: return None
        if _is_id3(audio):
            # 逐帧清洗，过滤掉整帧都是乱码的帧，再合并
            frames = []
            for k, v in audio.tags.items():
                if k.startswith("COMM") and v.text:
                    c = _clean_frame(v.text[0])
                    if c:
                        frames.append(c)
            return "\n".join(frames) if frames else None
        if isinstance(audio, MP4):
            val = audio.tags.get("©cmt") if audio.tags else None
            if not val: return None
            c = _clean_frame(val[0])
            return c if c else None
        if audio.tags is not None:
            for key in ("comment","COMMENT","description","DESCRIPTION"):
                val = audio.tags.get(key)
                if val:
                    raw = "\n".join(val) if isinstance(val, list) else str(val)
                    c = _clean_frame(raw)
                    return c if c else None
        return None
    except: return None


def write_comment(path: Path, new_value: Optional[str]) -> bool:
    try:
        audio = MutagenFile(path, easy=False)
        if audio is None: return False
        if _is_id3(audio):
            for k in [k for k in audio.tags.keys() if k.startswith("COMM")]: del audio.tags[k]
            if new_value: audio.tags.add(COMM(encoding=3, lang="XXX", desc="", text=[new_value]))
            audio.save(); return True
        if isinstance(audio, MP4):
            if audio.tags is None: audio.add_tags()
            if new_value: audio.tags["©cmt"] = [new_value]
            else: audio.tags.pop("©cmt", None)
            audio.save(); return True
        if audio.tags is not None:
            for key in ("comment","COMMENT","description","DESCRIPTION"):
                if key in audio.tags: del audio.tags[key]
            if new_value: audio.tags["comment"] = [new_value]
            audio.save(); return True
        return False
    except: return False

# ── 应用状态 ──────────────────────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.lock    = threading.Lock()
        self.scanning= False
        self.done    = False
        self.scanned = 0
        self.root: Optional[Path] = None
        self.files: dict[str, dict] = {}   # id → file_info

G = AppState()
app = Flask(__name__)

def _fid(path: Path) -> str:
    return hashlib.md5(str(path).encode()).hexdigest()[:14]

def _do_scan(root: Path):
    with G.lock:
        G.scanning = True; G.done = False; G.scanned = 0; G.files.clear()
    count = 0; batch: dict[str, dict] = {}
    for dirpath, _, files in os.walk(root):
        for fname in sorted(files):
            p = Path(dirpath) / fname
            if p.suffix.lower() not in AUDIO_EXTS: continue
            count += 1
            comment = read_comment(p)
            if comment and comment.strip():
                fid = _fid(p)
                try: rel = str(p.relative_to(root))
                except: rel = str(p)
                batch[fid] = {"id": fid, "name": p.stem, "ext": p.suffix.lower(),
                              "path": str(p), "rel": rel, "comment": comment.strip()}
            if count % 30 == 0:
                with G.lock:
                    G.scanned = count
                    if batch: G.files.update(batch); batch.clear()
    with G.lock:
        G.scanned = count
        if batch: G.files.update(batch)
        G.scanning = False; G.done = True

def _start_scan(root: Path):
    G.root = root
    threading.Thread(target=_do_scan, args=(root,), daemon=True).start()

# ── 工具 ──────────────────────────────────────────────────────────────────
def _filter_files(files: list[dict], q: str, field: str = 'all',
                  exclude: str = '') -> list[dict]:
    # 正向筛选：包含关键词
    if q:
        ql = q.lower()
        if field == 'name':    files = [f for f in files if ql in f["name"].lower()]
        elif field == 'comment': files = [f for f in files if ql in f["comment"].lower()]
        elif field == 'path':  files = [f for f in files if ql in f["rel"].lower()]
        else: files = [f for f in files if ql in f["name"].lower()
                       or ql in f["comment"].lower() or ql in f["rel"].lower()]
    # 反向过滤：排除备注含特定关键词的条目
    if exclude:
        el = exclude.lower()
        files = [f for f in files if el not in f["comment"].lower()]
    return files

def _sort_files(files: list[dict], sort: str, order: str) -> list[dict]:
    rev = order == "desc"
    if sort == "ext":     key = lambda f: (f["ext"], f["name"].lower())
    elif sort == "comment": key = lambda f: f["comment"].lower()
    else:                 key = lambda f: f["name"].lower()
    return sorted(files, key=key, reverse=rev)

def _make_pat(pattern: str, is_regex: bool, case_sensitive: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    pat_str = pattern if is_regex else re.escape(pattern)
    return re.compile(pat_str, flags)

# ── API ───────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    with G.lock:
        return jsonify(scanning=G.scanning, done=G.done,
                       scanned=G.scanned, with_comments=len(G.files))

@app.route("/api/files")
def api_files():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(10, min(200, int(request.args.get("per_page", 50))))
    q        = request.args.get("q", "").strip()
    field    = request.args.get("field", "all")
    exclude  = request.args.get("exclude", "").strip()
    sort     = request.args.get("sort", "name")
    order    = request.args.get("order", "asc")
    with G.lock: files = list(G.files.values())
    files = _filter_files(files, q, field, exclude)
    files = _sort_files(files, sort, order)
    total = len(files); pages = max(1, (total + per_page - 1) // per_page)
    page  = min(page, pages)
    chunk = files[(page - 1) * per_page : page * per_page]
    return jsonify(files=chunk, page=page, pages=pages, total=total, per_page=per_page)

@app.route("/api/match-pattern", methods=["POST"])
def api_match_pattern():
    d = request.json or {}
    pattern = d.get("pattern", "").strip()
    if not pattern: return jsonify(count=0, examples=[])
    try: pat = _make_pat(pattern, d.get("is_regex", False), d.get("case_sensitive", False))
    except re.error as e: return jsonify(error=str(e)), 400
    with G.lock: files = list(G.files.values())
    matched = [f for f in files if pat.search(f["comment"])]
    return jsonify(count=len(matched), examples=[f["name"] for f in matched[:8]])

@app.route("/api/clear", methods=["POST"])
def api_clear():
    ids = (request.json or {}).get("ids", [])
    with G.lock: targets = {i: G.files[i] for i in ids if i in G.files}
    cleared, failed = [], []
    for fid, info in targets.items():
        if write_comment(Path(info["path"]), None):
            cleared.append(fid)
            with G.lock: G.files.pop(fid, None)
        else: failed.append(fid)
    return jsonify(cleared=cleared, failed=failed)

@app.route("/api/clear-pattern", methods=["POST"])
def api_clear_pattern():
    d = request.json or {}
    pattern = d.get("pattern", "").strip()
    if not pattern: return jsonify(error="no pattern"), 400
    try: pat = _make_pat(pattern, d.get("is_regex", False), d.get("case_sensitive", False))
    except re.error as e: return jsonify(error=str(e)), 400
    with G.lock: matched = {i: f for i, f in G.files.items() if pat.search(f["comment"])}
    cleared, failed = [], []
    for fid, info in matched.items():
        if write_comment(Path(info["path"]), None):
            cleared.append(fid)
            with G.lock: G.files.pop(fid, None)
        else: failed.append(fid)
    return jsonify(cleared=cleared, failed=failed, matched=len(matched))

@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    if G.root and not G.scanning:
        _start_scan(G.root)
        return jsonify(ok=True)
    return jsonify(ok=False, reason="scanning" if G.scanning else "no root")

@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    path_str = (request.json or {}).get("path", "")
    if not path_str: return jsonify(error="no path"), 400
    p = Path(path_str)
    if not p.exists(): return jsonify(error="file not found"), 404
    try:
        sys_name = platform.system()
        if sys_name == "Windows":
            # /select, 和路径必须是同一个参数，用 shell=True 最稳妥
            subprocess.Popen(f'explorer /select,"{str(p)}"', shell=True)
        elif sys_name == "Darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/edit-comment", methods=["POST"])
def api_edit_comment():
    d = request.json or {}
    fid = d.get("id", "")
    new_comment = d.get("comment", "").strip()
    with G.lock:
        if fid not in G.files:
            return jsonify(error="not found"), 404
        info = G.files[fid]
    if write_comment(Path(info["path"]), new_comment or None):
        with G.lock:
            if new_comment:
                G.files[fid]["comment"] = new_comment
            else:
                G.files.pop(fid, None)
        return jsonify(ok=True, new_comment=new_comment)
    return jsonify(error="write failed"), 500

@app.route("/api/remove-keyword", methods=["POST"])
def api_remove_keyword():
    """从匹配备注中删除关键词（不清空整条备注）。"""
    d = request.json or {}
    pattern = d.get("pattern", "").strip()
    if not pattern: return jsonify(error="no pattern"), 400
    try: pat = _make_pat(pattern, d.get("is_regex", False), d.get("case_sensitive", False))
    except re.error as e: return jsonify(error=str(e)), 400
    with G.lock: snapshot = list(G.files.items())
    updated, failed = [], []
    for fid, info in snapshot:
        if not pat.search(info["comment"]): continue
        new_c = pat.sub("", info["comment"]).strip()
        if write_comment(Path(info["path"]), new_c or None):
            with G.lock:
                if new_c: G.files[fid]["comment"] = new_c
                else:     G.files.pop(fid, None)
            updated.append(fid)
        else:
            failed.append(fid)
    return jsonify(updated=updated, failed=failed)

# ── HTML 模板 ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="app-root" content="__ROOT__">
<title>音乐备注管理器</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Azeret+Mono:wght@300;400;500&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080c14;--bg2:#0c1220;--surface:#101828;--surface2:#141e30;
  --border:#1c2a40;--border2:#263852;
  --accent:#00d4ff;--accent-glow:rgba(0,212,255,.12);--accent-dim:rgba(0,212,255,.5);
  --danger:#ff3b5c;--danger-glow:rgba(255,59,92,.12);--danger-dim:rgba(255,59,92,.7);
  --warning:#f5a623;--success:#00e09e;
  --text:#b8cce0;--text-dim:#5e7590;--text-muted:#2e3f55;
  --ui:'Outfit',sans-serif;--mono:'Azeret Mono',monospace;
  --r:6px;--r2:10px;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--ui)}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg2)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
::selection{background:var(--accent-glow);color:var(--accent)}

/* Layout */
.app{display:flex;flex-direction:column;height:100vh;overflow:hidden}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;height:52px;background:var(--bg);
  border-bottom:1px solid var(--border);flex-shrink:0;gap:12px}
.hdr-left{display:flex;align-items:baseline;gap:14px;min-width:0}
.app-name{font-family:var(--mono);font-weight:500;font-size:15px;
  color:var(--accent);letter-spacing:.04em;white-space:nowrap}
.dir-path{font-family:var(--mono);font-size:11px;color:var(--text-dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.hdr-right{display:flex;align-items:center;gap:10px;flex-shrink:0}
.scan-badge{display:flex;align-items:center;gap:6px;font-size:12px;
  font-family:var(--mono);color:var(--text-dim)}
.scan-dot{width:7px;height:7px;border-radius:50%;background:var(--text-muted);flex-shrink:0}
.scan-dot.active{background:var(--accent);animation:pulse 1.1s ease-in-out infinite}
.scan-dot.done{background:var(--success)}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}
.btn-rescan{font-size:11px;font-family:var(--mono);color:var(--text-dim);
  background:none;border:1px solid var(--border);border-radius:var(--r);
  padding:4px 10px;cursor:pointer;transition:.15s}
.btn-rescan:hover{border-color:var(--border2);color:var(--text)}

/* Stats bar */
.stats{display:flex;align-items:center;gap:0;background:var(--bg2);
  border-bottom:1px solid var(--border);padding:0 20px;height:38px;flex-shrink:0}
.stat{display:flex;align-items:baseline;gap:6px;padding:0 16px}
.stat:first-child{padding-left:0}
.stat-num{font-family:var(--mono);font-size:16px;font-weight:500;color:var(--text)}
.stat-num.accent{color:var(--accent)}
.stat-num.danger{color:var(--danger)}
.stat-label{font-size:11px;color:var(--text-dim)}
.stat-sep{width:1px;height:16px;background:var(--border)}

/* Toolbar */
.toolbar{display:flex;align-items:center;gap:10px;padding:10px 20px;
  background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap}
.search-wrap{position:relative;flex:1;min-width:200px;max-width:380px}
.field-sel{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:7px 10px;font-family:var(--ui);font-size:12px;color:var(--text-dim);
  outline:none;cursor:pointer;flex-shrink:0;transition:.15s}
.field-sel:focus{border-color:var(--accent-dim);color:var(--text)}
/* 排除关键词输入框 */
.exclude-wrap{position:relative;min-width:148px;max-width:240px;flex-shrink:0}
.exclude-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);
  color:var(--danger-dim);font-size:13px;pointer-events:none;line-height:1}
.exclude-input{width:100%;background:var(--surface);
  border:1px solid color-mix(in srgb, var(--danger) 30%, var(--border));
  border-radius:var(--r);padding:7px 28px 7px 30px;font-family:var(--ui);
  font-size:13px;color:var(--text);outline:none;transition:.15s}
.exclude-input:focus{border-color:var(--danger-dim);background:var(--surface2)}
.exclude-input::placeholder{color:var(--text-muted)}
.exclude-input.active{border-color:var(--danger);
  background:color-mix(in srgb, var(--danger) 5%, var(--surface))}
.exclude-clear{position:absolute;right:8px;top:50%;transform:translateY(-50%);
  background:none;border:none;color:var(--danger-dim);cursor:pointer;
  font-size:13px;padding:2px 3px;display:none}
.exclude-clear.show{display:block}
.exclude-clear:hover{color:var(--danger)}
.search-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);
  color:var(--text-dim);font-size:15px;pointer-events:none}
.search-input{width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:7px 32px 7px 34px;font-family:var(--ui);
  font-size:13px;color:var(--text);outline:none;transition:.15s}
.search-input:focus{border-color:var(--accent-dim);background:var(--surface2)}
.search-input::placeholder{color:var(--text-muted)}
.search-clear{position:absolute;right:9px;top:50%;transform:translateY(-50%);
  background:none;border:none;color:var(--text-dim);cursor:pointer;
  font-size:14px;padding:2px 4px;display:none}
.search-clear.show{display:block}
.toolbar-actions{display:flex;align-items:center;gap:8px;margin-left:auto}

/* Buttons */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;
  border-radius:var(--r);font-family:var(--ui);font-size:13px;font-weight:500;
  cursor:pointer;border:1px solid transparent;transition:.15s;white-space:nowrap}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:var(--accent-glow);border-color:var(--accent-dim);color:var(--accent)}
.btn-primary:not(:disabled):hover{background:rgba(0,212,255,.2)}
.btn-danger{background:var(--danger-glow);border-color:var(--danger-dim);color:var(--danger)}
.btn-danger:not(:disabled):hover{background:rgba(255,59,92,.2)}
.btn-warn{background:rgba(245,166,35,.1);border-color:rgba(245,166,35,.4);color:var(--warning)}
.btn-warn:not(:disabled):hover{background:rgba(245,166,35,.18)}
.btn-ghost{background:none;border-color:var(--border2);color:var(--text-dim)}
.btn-ghost:hover{border-color:var(--text-dim);color:var(--text)}
.btn-icon{padding:7px 10px;font-size:15px}
.badge{display:inline-flex;align-items:center;justify-content:center;
  min-width:18px;height:18px;border-radius:9px;background:var(--danger);
  color:#fff;font-size:10px;font-family:var(--mono);padding:0 4px;line-height:1}

/* Table container */
.tbl-wrap{flex:1;overflow:auto;position:relative}

/* Scan placeholder */
.scan-ph{display:flex;align-items:center;justify-content:center;
  height:100%;flex-direction:column;gap:20px}
.scan-bars{display:flex;align-items:flex-end;gap:5px;height:40px}
.scan-bars span{width:5px;border-radius:3px;background:var(--accent);
  animation:bar 1.2s ease-in-out infinite}
.scan-bars span:nth-child(1){height:40%;animation-delay:0s}
.scan-bars span:nth-child(2){height:70%;animation-delay:.15s}
.scan-bars span:nth-child(3){height:100%;animation-delay:.3s}
.scan-bars span:nth-child(4){height:70%;animation-delay:.45s}
.scan-bars span:nth-child(5){height:40%;animation-delay:.6s}
@keyframes bar{0%,100%{opacity:.3;transform:scaleY(.5)}50%{opacity:1;transform:scaleY(1)}}
.scan-ph-text{font-family:var(--mono);font-size:13px;color:var(--text-dim)}

/* Data table */
table.dt{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}
table.dt thead{position:sticky;top:0;z-index:5;background:var(--surface)}
table.dt th{padding:9px 12px;text-align:left;font-size:11px;font-weight:500;
  color:var(--text-dim);letter-spacing:.06em;text-transform:uppercase;
  border-bottom:1px solid var(--border);user-select:none}
table.dt th.sortable{cursor:pointer}
table.dt th.sortable:hover{color:var(--text)}
table.dt th.sort-asc .si::after{content:'↑';color:var(--accent)}
table.dt th.sort-desc .si::after{content:'↓';color:var(--accent)}
table.dt th .si{margin-left:4px;font-size:10px;color:var(--text-muted)}
table.dt td{padding:10px 12px;border-bottom:1px solid var(--border);
  vertical-align:top;overflow:hidden}
table.dt tr{transition:background .1s}
table.dt tr:hover td{background:var(--surface)}
table.dt tr.selected td{background:rgba(0,212,255,.04)}
table.dt tr.selected td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.col-chk{width:40px}
.col-name{width:22%}
.col-ext{width:72px}
.col-comment{width:38%}
.col-path{width:auto}
.col-act{width:96px;text-align:right}

/* Cell content */
.cell-name{font-family:var(--mono);font-size:12.5px;color:var(--text);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ext-badge{display:inline-flex;align-items:center;padding:2px 7px;
  border-radius:4px;font-family:var(--mono);font-size:11px;font-weight:500;
  border:1px solid transparent;letter-spacing:.02em}
.comment-preview{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;line-height:1.55;color:var(--text);font-size:12.5px;
  cursor:pointer;word-break:break-all}
.comment-preview:hover{color:var(--accent)}
.cell-path{font-family:var(--mono);font-size:11px;color:var(--text-dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row-acts{display:flex;justify-content:flex-end;gap:3px;opacity:0;transition:.12s}
table.dt tr:hover .row-acts{opacity:1}
.row-btn{background:none;border:none;cursor:pointer;padding:3px 5px;
  border-radius:4px;font-size:13px;color:var(--text-muted);transition:.12s;line-height:1}
.row-btn:hover{background:var(--surface2)}
.row-btn.open:hover{color:var(--accent)}
.row-btn.edit:hover{color:var(--warning)}
.row-btn.del:hover{color:var(--danger);background:var(--danger-glow)}

/* Edit textarea */
.edit-textarea{width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:10px 12px;font-family:var(--mono);font-size:13px;
  color:var(--text);resize:vertical;min-height:100px;max-height:320px;outline:none;
  line-height:1.65;transition:.15s}
.edit-textarea:focus{border-color:var(--accent-dim)}
.char-count{font-family:var(--mono);font-size:11px;color:var(--text-muted);text-align:right;margin-top:4px}

/* Checkbox */
.chk{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}

/* Empty / no results */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100%;gap:12px;color:var(--text-dim)}
.empty-icon{font-size:36px;opacity:.3}
.empty-text{font-size:14px}

/* Pagination */
.pgn{display:flex;align-items:center;justify-content:space-between;
  padding:8px 20px;background:var(--bg2);border-top:1px solid var(--border);
  flex-shrink:0;gap:12px}
.pgn-info{font-family:var(--mono);font-size:12px;color:var(--text-dim)}
.pgn-nav{display:flex;align-items:center;gap:8px}
.pgn-btn{background:var(--surface);border:1px solid var(--border);color:var(--text-dim);
  width:32px;height:32px;border-radius:var(--r);cursor:pointer;font-size:14px;
  display:flex;align-items:center;justify-content:center;transition:.12s}
.pgn-btn:hover:not(:disabled){border-color:var(--accent-dim);color:var(--accent)}
.pgn-btn:disabled{opacity:.35;cursor:not-allowed}
.pgn-input-wrap{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim)}
.pgn-input{width:48px;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:4px 6px;font-family:var(--mono);font-size:12px;
  color:var(--text);text-align:center;outline:none}
.pgn-input:focus{border-color:var(--accent-dim)}
.per-page-sel{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:4px 6px;font-size:12px;color:var(--text-dim);
  outline:none;cursor:pointer}

/* Modal */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);
  display:flex;align-items:center;justify-content:center;z-index:100;padding:20px}
.modal{background:var(--surface2);border:1px solid var(--border2);border-radius:var(--r2);
  width:100%;max-width:520px;overflow:hidden;animation:slideUp .2s ease}
.modal.wide{max-width:640px}
@keyframes slideUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
.modal-hdr{display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;border-bottom:1px solid var(--border)}
.modal-title{font-weight:600;font-size:14px;color:var(--text)}
.modal-close{background:none;border:none;color:var(--text-dim);cursor:pointer;
  font-size:18px;padding:0 2px;line-height:1;transition:.12s}
.modal-close:hover{color:var(--text)}
.modal-body{padding:20px;display:flex;flex-direction:column;gap:14px}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;
  padding:14px 20px;border-top:1px solid var(--border);background:var(--surface)}
.form-label{font-size:12px;color:var(--text-dim);margin-bottom:5px;display:block}
.form-input{width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:8px 11px;font-family:var(--ui);font-size:13px;
  color:var(--text);outline:none;transition:.15s}
.form-input:focus{border-color:var(--accent-dim)}
.form-row{display:flex;gap:20px}
.toggle-wrap{display:flex;align-items:center;gap:7px;font-size:13px;
  color:var(--text-dim);cursor:pointer;user-select:none}
.toggle-wrap input{accent-color:var(--accent);width:14px;height:14px;cursor:pointer}
.pattern-preview{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  padding:10px 12px;font-size:12px;color:var(--text-dim);min-height:60px}
.preview-count{font-family:var(--mono);font-size:14px;color:var(--accent);margin-bottom:6px}
.preview-count.none{color:var(--text-muted)}
.preview-count.err{color:var(--danger)}
.preview-list{display:flex;flex-direction:column;gap:2px}
.preview-item{font-family:var(--mono);font-size:11.5px;color:var(--text-dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.comment-full-box{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);
  padding:12px;font-family:var(--mono);font-size:12.5px;color:var(--text);
  line-height:1.7;white-space:pre-wrap;word-break:break-all;max-height:220px;overflow-y:auto}
.comment-meta-row{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:12px;color:var(--text-dim)}
.comment-meta-row span{font-family:var(--mono)}
.confirm-msg{font-size:14px;line-height:1.7;color:var(--text)}
.confirm-msg strong{color:var(--danger);font-family:var(--mono)}

/* Toast */
.toast-stack{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;
  gap:8px;z-index:200;pointer-events:none}
.toast{display:flex;align-items:center;gap:10px;padding:11px 16px;
  border-radius:var(--r);font-size:13px;border:1px solid transparent;
  animation:toastIn .2s ease;min-width:220px;pointer-events:auto;
  box-shadow:0 4px 20px rgba(0,0,0,.4)}
.toast.out{animation:toastOut .2s ease forwards}
@keyframes toastIn{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:translateX(0)}}
@keyframes toastOut{from{opacity:1}to{opacity:0;transform:translateX(12px)}}
.toast-success{background:rgba(0,224,158,.1);border-color:rgba(0,224,158,.3);color:var(--success)}
.toast-error{background:var(--danger-glow);border-color:rgba(255,59,92,.3);color:var(--danger)}
.toast-info{background:var(--accent-glow);border-color:rgba(0,212,255,.3);color:var(--accent)}
</style>
</head>
<body>
<div class="app">
  <!-- Header -->
  <div class="hdr">
    <div class="hdr-left">
      <div class="app-name">⬡ COMMENT MGR</div>
      <div class="dir-path" id="dirPath">__ROOT__</div>
    </div>
    <div class="hdr-right">
      <div class="scan-badge">
        <div class="scan-dot" id="scanDot"></div>
        <span id="scanText">初始化...</span>
      </div>
      <button class="btn-rescan" id="btnRescan" style="display:none" onclick="rescan()">重新扫描</button>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat">
      <span class="stat-num" id="stTotal">—</span>
      <span class="stat-label">含备注</span>
    </div>
    <div class="stat-sep"></div>
    <div class="stat">
      <span class="stat-num" id="stFiltered">—</span>
      <span class="stat-label">当前筛选</span>
    </div>
    <div class="stat-sep"></div>
    <div class="stat">
      <span class="stat-num accent" id="stSelected">0</span>
      <span class="stat-label">已选中</span>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar">
    <div class="search-wrap">
      <span class="search-icon">⌕</span>
      <input id="searchInput" class="search-input" placeholder="搜索..." autocomplete="off">
      <button class="search-clear" id="searchClear" onclick="clearSearch()">✕</button>
    </div>
    <select id="fieldSel" class="field-sel" onchange="onFieldChange(this.value)">
      <option value="all">全字段</option>
      <option value="name">曲名</option>
      <option value="comment">备注</option>
      <option value="path">路径</option>
    </select>
    <div class="exclude-wrap">
      <span class="exclude-icon">⊘</span>
      <input id="excludeInput" class="exclude-input" placeholder="排除含..." autocomplete="off"
             title="排除备注中含该关键词的结果">
      <button class="exclude-clear" id="excludeClear" onclick="clearExclude()">✕</button>
    </div>
    <div class="toolbar-actions">
      <button id="btnClearSel" class="btn btn-danger" disabled onclick="doConfirmClearSelected()">
        清空已选 <span id="selBadge" class="badge" style="display:none">0</span>
      </button>
      <button class="btn btn-warn" onclick="showPatternModal()">关键词操作</button>
    </div>
  </div>

  <!-- Table wrap -->
  <div class="tbl-wrap" id="tblWrap">
    <div class="scan-ph" id="scanPh">
      <div class="scan-bars"><span></span><span></span><span></span><span></span><span></span></div>
      <div class="scan-ph-text" id="scanPhText">正在扫描音乐库...</div>
    </div>
    <table class="dt" id="dataTable" style="display:none">
      <thead>
        <tr>
          <th class="col-chk"><input type="checkbox" class="chk" id="selectAll" onchange="toggleSelectAll(this.checked)"></th>
          <th class="col-name sortable" data-sort="name">曲名 <span class="si"></span></th>
          <th class="col-ext">格式</th>
          <th class="col-comment sortable" data-sort="comment">备注 <span class="si"></span></th>
          <th class="col-path sortable" data-sort="name">路径 <span class="si"></span></th>
          <th class="col-act">操作</th>
        </tr>
      </thead>
      <tbody id="tblBody"></tbody>
    </table>
    <div class="empty" id="emptyState" style="display:none">
      <div class="empty-icon">◎</div>
      <div class="empty-text" id="emptyText">没有找到含备注的文件</div>
    </div>
  </div>

  <!-- Pagination -->
  <div class="pgn" id="pgnBar" style="display:none">
    <div class="pgn-input-wrap">
      每页
      <select class="per-page-sel" id="perPageSel" onchange="onPerPageChange()">
        <option value="30">30</option><option value="50" selected>50</option>
        <option value="100">100</option><option value="200">200</option>
      </select>
      条
    </div>
    <div class="pgn-nav">
      <button class="pgn-btn" id="btnPrev" onclick="goPage(S.page-1)" disabled>‹</button>
      <span class="pgn-info" id="pgnInfo">— / —</span>
      <button class="pgn-btn" id="btnNext" onclick="goPage(S.page+1)" disabled>›</button>
    </div>
    <div class="pgn-input-wrap">
      跳至第
      <input type="number" class="pgn-input" id="pgInput" min="1"
        onkeydown="if(event.key==='Enter')goPage(+this.value)">
      页
    </div>
  </div>
</div>

<!-- Overlay -->
<div class="overlay" id="overlay" style="display:none" onclick="onOverlayClick(event)">

  <!-- Comment detail modal -->
  <div class="modal wide" id="mComment" style="display:none">
    <div class="modal-hdr">
      <div class="modal-title" id="mCommentTitle">备注详情</div>
      <button class="modal-close" onclick="hideModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="comment-meta-row" id="mCommentMeta"></div>
      <div>
        <div class="form-label">完整备注内容</div>
        <div class="comment-full-box" id="mCommentBody"></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-primary" id="mCommentEdit">编辑备注</button>
      <button class="btn btn-danger" id="mCommentClear">清空备注</button>
      <button class="btn btn-ghost" onclick="hideModal()">关闭</button>
    </div>
  </div>

  <!-- Pattern modal -->
  <div class="modal" id="mPattern" style="display:none">
    <div class="modal-hdr">
      <div class="modal-title">关键词操作</div>
      <button class="modal-close" onclick="hideModal()">✕</button>
    </div>
    <div class="modal-body">
      <div>
        <label class="form-label">关键词</label>
        <input id="patInput" class="form-input" placeholder="购买自 · 试听版本">
      </div>
      <div class="form-row">
        <label class="toggle-wrap"><input type="checkbox" id="patCase"> 区分大小写</label>
        <label class="toggle-wrap"><input type="checkbox" id="patRegex"> 正则模式</label>
      </div>
      <div>
        <div class="form-label">匹配预览</div>
        <div class="pattern-preview" id="patPreview">
          <div class="preview-count none">输入关键词查看匹配文件</div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-warn" id="patRemoveBtn" disabled onclick="doRemoveKeyword()" title="从备注中删除关键词，保留其余内容">删除关键词</button>
      <button class="btn btn-danger" id="patClearBtn" disabled onclick="doPatternClear()" title="清空含此关键词的整条备注">清空匹配备注</button>
      <button class="btn btn-ghost" onclick="hideModal()">取消</button>
    </div>
  </div>

  <!-- Edit comment modal -->
  <div class="modal wide" id="mEdit" style="display:none">
    <div class="modal-hdr">
      <div class="modal-title" id="editTitle">编辑备注</div>
      <button class="modal-close" onclick="hideModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="comment-meta-row" id="editMeta"></div>
      <div>
        <div class="form-label">备注内容（留空则清空备注）</div>
        <textarea id="editTextarea" class="edit-textarea" oninput="onEditInput()"></textarea>
        <div class="char-count" id="editCharCount">0 字符</div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-primary" onclick="doSaveEdit()">确认保存</button>
      <button class="btn btn-ghost" onclick="hideModal()">取消</button>
    </div>
  </div>

  <!-- Confirm modal -->
  <div class="modal" id="mConfirm" style="display:none">
    <div class="modal-hdr">
      <div class="modal-title">确认操作</div>
    </div>
    <div class="modal-body">
      <div class="confirm-msg" id="confirmMsg"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-danger" id="confirmOkBtn">确认</button>
      <button class="btn btn-ghost" onclick="hideModal()">取消</button>
    </div>
  </div>

</div>

<!-- Toast container -->
<div class="toast-stack" id="toastStack"></div>

<script>
const ROOT = document.querySelector('meta[name="app-root"]').content;
const EXT_STYLE = {
  '.flac':{bg:'rgba(0,224,158,.12)',c:'#00e09e',b:'rgba(0,224,158,.28)'},
  '.ape' :{bg:'rgba(0,224,158,.08)',c:'#00c480',b:'rgba(0,224,158,.2)'},
  '.wv'  :{bg:'rgba(0,224,158,.08)',c:'#00c480',b:'rgba(0,224,158,.2)'},
  '.wav' :{bg:'rgba(180,180,200,.08)',c:'#9ca3af',b:'rgba(180,180,200,.18)'},
  '.aiff':{bg:'rgba(180,180,200,.08)',c:'#9ca3af',b:'rgba(180,180,200,.18)'},
  '.mp3' :{bg:'rgba(77,171,247,.12)',c:'#4dabf7',b:'rgba(77,171,247,.28)'},
  '.m4a' :{bg:'rgba(167,139,250,.12)',c:'#a78bfa',b:'rgba(167,139,250,.28)'},
  '.ogg' :{bg:'rgba(245,166,35,.12)',c:'#f5a623',b:'rgba(245,166,35,.28)'},
  '.opus':{bg:'rgba(245,166,35,.12)',c:'#f5a623',b:'rgba(245,166,35,.28)'},
};
// 文件对象缓存：id → file，renderTable 每次刷新，onclick 只传 id
const FILE_MAP = new Map();

const S = {
  page:1, perPage:50, q:'', field:'all', exclude:'', sort:'name', order:'asc',
  selected:new Set(), pageIds:[],
  total:0, pages:0, scanning:true, scanned:0, withComments:0,
};
let _pollTimer=null, _searchTimer=null, _patTimer=null, _excludeTimer=null;
let _activeFile=null, _confirmCb=null, _patMatchCount=0;

// ── Init ────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('th.sortable').forEach(th => {
    th.addEventListener('click', () => onSort(th.dataset.sort));
  });
  document.getElementById('searchInput').addEventListener('input', e => {
    const v = e.target.value;
    document.getElementById('searchClear').classList.toggle('show', v.length > 0);
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => { S.q = v; S.page = 1; fetchFiles(); }, 300);
  });
  document.getElementById('excludeInput').addEventListener('input', e => {
    const v = e.target.value;
    const btn = document.getElementById('excludeClear');
    const inp = document.getElementById('excludeInput');
    btn.classList.toggle('show', v.length > 0);
    inp.classList.toggle('active', v.length > 0);
    clearTimeout(_excludeTimer);
    _excludeTimer = setTimeout(() => { S.exclude = v; S.page = 1; fetchFiles(); }, 300);
  });
  document.getElementById('patInput').addEventListener('input', () => {
    clearTimeout(_patTimer);
    _patTimer = setTimeout(previewPattern, 400);
  });
  document.getElementById('patCase').addEventListener('change', () => {
    clearTimeout(_patTimer); _patTimer = setTimeout(previewPattern, 200);
  });
  document.getElementById('patRegex').addEventListener('change', () => {
    clearTimeout(_patTimer); _patTimer = setTimeout(previewPattern, 200);
  });
  pollStatus();
});

// ── Status polling ───────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const d = await fetchJSON('/api/status');
    S.scanning = d.scanning; S.scanned = d.scanned; S.withComments = d.with_comments;
    updateScanUI(d.done);
    if (d.with_comments > 0 || d.done) await fetchFiles();
    if (!d.done) _pollTimer = setTimeout(pollStatus, 1500);
  } catch(e) {
    console.error('[pollStatus]', e);
    _pollTimer = setTimeout(pollStatus, 2000);
  }
}

function updateScanUI(done) {
  const dot = document.getElementById('scanDot');
  const txt = document.getElementById('scanText');
  const btn = document.getElementById('btnRescan');
  if (S.scanning) {
    dot.className = 'scan-dot active';
    txt.textContent = `扫描中… 已检查 ${S.scanned.toLocaleString()} 个文件，找到 ${S.withComments} 个含备注`;
    btn.style.display = 'none';
    document.getElementById('scanPhText').textContent =
      `正在扫描… 已检查 ${S.scanned.toLocaleString()} 个文件，找到 ${S.withComments} 个含备注`;
  } else {
    dot.className = 'scan-dot done';
    txt.textContent = `扫描完成，共 ${S.scanned.toLocaleString()} 个文件`;
    btn.style.display = '';
  }
}

// ── Files ────────────────────────────────────────────────────────────────
async function fetchFiles() {
  try {
    const params = new URLSearchParams({
      page: S.page, per_page: S.perPage, q: S.q, field: S.field,
      exclude: S.exclude, sort: S.sort, order: S.order
    });
    const d = await fetchJSON('/api/files?' + params);
    if (!d || !Array.isArray(d.files)) throw new Error('invalid response from /api/files');
    S.total = d.total; S.pages = d.pages; S.page = d.page;
    S.pageIds = d.files.map(f => f.id);
    renderTable(d.files);
    renderPagination();
    updateStats();
    updateSelectAll();
    updateActionBar();
  } catch(e) {
    console.error('[fetchFiles]', e);
    const ph = document.getElementById('scanPh');
    const phTxt = document.getElementById('scanPhText');
    if (ph) ph.style.display = '';
    if (phTxt) phTxt.textContent = '数据加载失败，请检查控制台或刷新页面（' + e.message + '）';
  }
}

function renderTable(files) {
  const ph = document.getElementById('scanPh');
  const tbl = document.getElementById('dataTable');
  const empty = document.getElementById('emptyState');
  const pgn = document.getElementById('pgnBar');
  const emptyText = document.getElementById('emptyText');

  if (S.scanning && files.length === 0 && !S.withComments) {
    ph.style.display = ''; tbl.style.display = 'none';
    empty.style.display = 'none'; pgn.style.display = 'none'; return;
  }
  ph.style.display = 'none';
  if (files.length === 0) {
    tbl.style.display = 'none'; empty.style.display = '';
    pgn.style.display = 'none';
    emptyText.textContent = S.q ? `"${S.q}" 没有匹配结果` : '所有备注已清空';
    return;
  }
  tbl.style.display = ''; empty.style.display = 'none'; pgn.style.display = '';

  // 更新缓存，onclick 只需传 id，避免 JSON 双引号破坏 HTML 属性
  FILE_MAP.clear();
  files.forEach(f => FILE_MAP.set(f.id, f));

  const rows = files.map(f => {
    const es = EXT_STYLE[f.ext] || {bg:'rgba(100,100,120,.1)',c:'#888',b:'rgba(100,100,120,.2)'};
    const sel = S.selected.has(f.id);
    const cmt = escHtml(f.comment);
    const name = escHtml(f.name);
    const rel  = escHtml(f.rel);
    const id   = f.id;   // 纯十六进制，无需额外转义
    return `<tr class="${sel?'selected':''}" data-id="${id}">
      <td class="col-chk"><input type="checkbox" class="chk row-chk" data-id="${id}" ${sel?'checked':''}
        onchange="toggleSelect('${id}', this.checked)"></td>
      <td class="col-name"><div class="cell-name" title="${name}">${name}</div></td>
      <td class="col-ext"><span class="ext-badge" style="background:${es.bg};color:${es.c};border-color:${es.b}">${f.ext}</span></td>
      <td class="col-comment"><div class="comment-preview" onclick="showCommentModal('${id}')">${cmt}</div></td>
      <td class="col-path"><div class="cell-path" title="${rel}">${rel}</div></td>
      <td class="col-act"><div class="row-acts">
        <button class="row-btn open" title="打开所在文件夹" onclick="doOpenFolder('${id}')">📂</button>
      </div></td>
    </tr>`;
  }).join('');
  document.getElementById('tblBody').innerHTML = rows;
}

// ── Selection ────────────────────────────────────────────────────────────
function toggleSelectAll(checked) {
  S.pageIds.forEach(id => { checked ? S.selected.add(id) : S.selected.delete(id); });
  document.querySelectorAll('.row-chk').forEach(cb => {
    cb.checked = checked;
    cb.closest('tr').classList.toggle('selected', checked);
  });
  updateStats(); updateActionBar();
}

function toggleSelect(id, checked) {
  checked ? S.selected.add(id) : S.selected.delete(id);
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (tr) tr.classList.toggle('selected', checked);
  updateStats(); updateSelectAll(); updateActionBar();
}

function updateSelectAll() {
  const sa = document.getElementById('selectAll');
  if (!S.pageIds.length) { sa.checked = false; sa.indeterminate = false; return; }
  const selOnPage = S.pageIds.filter(id => S.selected.has(id)).length;
  sa.checked = selOnPage === S.pageIds.length;
  sa.indeterminate = selOnPage > 0 && selOnPage < S.pageIds.length;
}

// ── Sort ─────────────────────────────────────────────────────────────────
function onSort(col) {
  if (S.sort === col) S.order = S.order === 'asc' ? 'desc' : 'asc';
  else { S.sort = col; S.order = 'asc'; }
  S.page = 1;
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.sort === S.sort) th.classList.add('sort-'+S.order);
  });
  fetchFiles();
}

// ── Pagination ───────────────────────────────────────────────────────────
function renderPagination() {
  document.getElementById('pgnInfo').textContent = `${S.page} / ${S.pages}`;
  document.getElementById('btnPrev').disabled = S.page <= 1;
  document.getElementById('btnNext').disabled = S.page >= S.pages;
  document.getElementById('pgInput').value = S.page;
  document.getElementById('pgInput').max = S.pages;
}

function goPage(n) {
  n = Math.max(1, Math.min(S.pages, Math.floor(n)));
  if (n !== S.page) { S.page = n; fetchFiles(); }
}

function onPerPageChange() {
  S.perPage = +document.getElementById('perPageSel').value;
  S.page = 1; fetchFiles();
}

// ── Stats & Actions ───────────────────────────────────────────────────────
function updateStats() {
  document.getElementById('stTotal').textContent = S.withComments.toLocaleString();
  document.getElementById('stFiltered').textContent = S.total.toLocaleString();
  document.getElementById('stSelected').textContent = S.selected.size;
}

function updateActionBar() {
  const n = S.selected.size;
  const btn = document.getElementById('btnClearSel');
  const badge = document.getElementById('selBadge');
  btn.disabled = n === 0;
  badge.style.display = n > 0 ? '' : 'none';
  badge.textContent = n;
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function rescan() {
  await fetchJSON('/api/rescan', {method:'POST'});
  S.selected.clear(); S.page = 1;
  pollStatus();
}

// ── Search / Exclude ──────────────────────────────────────────────────────
function onFieldChange(v) { S.field = v; S.page = 1; fetchFiles(); }

function clearSearch() {
  document.getElementById('searchInput').value = '';
  document.getElementById('searchClear').classList.remove('show');
  S.q = ''; S.page = 1; fetchFiles();
}

function clearExclude() {
  const inp = document.getElementById('excludeInput');
  inp.value = '';
  inp.classList.remove('active');
  document.getElementById('excludeClear').classList.remove('show');
  S.exclude = ''; S.page = 1; fetchFiles();
}

// ── Quick clear (row button) ──────────────────────────────────────────────
function doQuickClear(id) {
  const file = FILE_MAP.get(id);
  if (!file) return;
  showConfirm(`确认清空 <strong>${escHtml(file.name)}</strong> 的备注？`, async () => {
    const d = await fetchJSON('/api/clear', {method:'POST', body:{ids:[id]}});
    onClearResult(d, '已清空 1 条备注');
  });
}

// ── Open folder ───────────────────────────────────────────────────────────
async function doOpenFolder(id) {
  const file = FILE_MAP.get(id);
  if (!file) { toast('文件信息未找到，请刷新页面', 'error'); return; }
  try {
    const d = await fetchJSON('/api/open-folder', {method:'POST', body:{path: file.path}});
    if (!d.ok) toast(d.error || '无法打开文件夹', 'error');
  } catch { toast('无法打开文件夹', 'error'); }
}

// ── Edit comment modal ────────────────────────────────────────────────────
function showEditModal(id) {
  const file = FILE_MAP.get(id) || _activeFile;
  if (!file) return;
  _activeFile = file;
  document.getElementById('editTitle').textContent = '编辑备注 — ' + file.name;
  document.getElementById('editMeta').innerHTML =
    `<span style="color:var(--text-dim)">格式：</span><span>${escHtml(file.ext)}</span>
     <span style="color:var(--text-dim)">路径：</span><span style="color:var(--text-dim);font-size:11px">${escHtml(file.rel)}</span>`;
  const ta = document.getElementById('editTextarea');
  ta.value = file.comment;
  document.getElementById('editCharCount').textContent = file.comment.length + ' 字符';
  showModal('mEdit');
  setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 80);
}

function onEditInput() {
  const v = document.getElementById('editTextarea').value;
  document.getElementById('editCharCount').textContent = v.length + ' 字符';
}

async function doSaveEdit() {
  const newComment = document.getElementById('editTextarea').value.trim();
  const file = _activeFile;
  if (newComment === file.comment) { hideModal(); return; }
  const preview = newComment ? `修改为：「${newComment.slice(0,60)}${newComment.length>60?'…':''}」`
                              : '清空备注（内容将被删除）';
  showConfirm(`对 <strong>${escHtml(file.name)}</strong>：<br><br>${escHtml(preview)}`, async () => {
    const d = await fetchJSON('/api/edit-comment', {method:'POST',
      body:{id: file.id, comment: newComment}});
    if (d.ok) {
      toast(newComment ? '备注已更新' : '备注已清空', 'success');
      S.selected.delete(file.id);
      await fetchFiles();
    } else {
      toast('写入失败：' + (d.error || '未知错误'), 'error');
    }
  });
}

// ── Clear selected ────────────────────────────────────────────────────────
function doConfirmClearSelected() {
  const n = S.selected.size;
  showConfirm(`确认清空已选的 <strong>${n}</strong> 个文件的备注？此操作不可撤销。`, async () => {
    const ids = [...S.selected];
    const d = await fetchJSON('/api/clear', {method:'POST', body:{ids}});
    d.cleared.forEach(id => S.selected.delete(id));
    onClearResult(d, `已清空 ${d.cleared.length} 条备注`);
  });
}

// ── Pattern modal ─────────────────────────────────────────────────────────
function showPatternModal() {
  document.getElementById('patInput').value = '';
  document.getElementById('patCase').checked = false;
  document.getElementById('patRegex').checked = false;
  document.getElementById('patPreview').innerHTML = '<div class="preview-count none">输入关键词查看匹配文件</div>';
  document.getElementById('patClearBtn').disabled = true;
  document.getElementById('patRemoveBtn').disabled = true;
  _patMatchCount = 0;
  showModal('mPattern');
  setTimeout(() => document.getElementById('patInput').focus(), 80);
}

async function previewPattern() {
  const pattern = document.getElementById('patInput').value.trim();
  const prev = document.getElementById('patPreview');
  const clearBtn = document.getElementById('patClearBtn');
  const removeBtn = document.getElementById('patRemoveBtn');
  if (!pattern) {
    prev.innerHTML = '<div class="preview-count none">输入关键词查看匹配文件</div>';
    clearBtn.disabled = removeBtn.disabled = true; _patMatchCount = 0; return;
  }
  prev.innerHTML = '<div class="preview-count none">查询中…</div>';
  try {
    const d = await fetchJSON('/api/match-pattern', {method:'POST', body:{
      pattern, is_regex:document.getElementById('patRegex').checked,
      case_sensitive:document.getElementById('patCase').checked
    }});
    if (d.error) {
      prev.innerHTML = `<div class="preview-count err">正则错误：${escHtml(d.error)}</div>`;
      clearBtn.disabled = removeBtn.disabled = true; _patMatchCount = 0; return;
    }
    _patMatchCount = d.count;
    clearBtn.disabled = removeBtn.disabled = d.count === 0;
    const items = d.examples.map(n => `<div class="preview-item">• ${escHtml(n)}</div>`).join('');
    const more = d.count > d.examples.length
      ? `<div class="preview-item" style="color:var(--text-muted)">… 还有 ${d.count-d.examples.length} 个</div>` : '';
    prev.innerHTML = `<div class="preview-count ${d.count?'':'none'}">将匹配 ${d.count} 个文件</div>
      <div class="preview-list">${items}${more}</div>`;
  } catch {
    prev.innerHTML = `<div class="preview-count err">请求失败</div>`;
    clearBtn.disabled = removeBtn.disabled = true;
  }
}

async function doPatternClear() {
  if (_patMatchCount === 0) return;
  showConfirm(`确认<strong>清空</strong> ${_patMatchCount} 个文件的完整备注？`, async () => {
    const d = await fetchJSON('/api/clear-pattern', {method:'POST', body:{
      pattern: document.getElementById('patInput').value.trim(),
      is_regex: document.getElementById('patRegex').checked,
      case_sensitive: document.getElementById('patCase').checked,
    }});
    d.cleared.forEach(id => S.selected.delete(id));
    onClearResult(d, `已清空 ${d.cleared.length} 条备注`);
  });
}

async function doRemoveKeyword() {
  if (_patMatchCount === 0) return;
  const kw = document.getElementById('patInput').value.trim();
  showConfirm(`确认从 <strong>${_patMatchCount}</strong> 个文件的备注中<strong>删除关键词</strong>「${escHtml(kw)}」？<br><small style="color:var(--text-dim)">（保留备注其余内容，若删除后为空则清空整条备注）</small>`, async () => {
    const d = await fetchJSON('/api/remove-keyword', {method:'POST', body:{
      pattern: kw,
      is_regex: document.getElementById('patRegex').checked,
      case_sensitive: document.getElementById('patCase').checked,
    }});
    d.updated?.forEach(id => S.selected.delete(id));
    if ((d.updated?.length ?? 0) > 0 || (d.failed?.length ?? 0) > 0) {
      const msg = `已从 ${d.updated.length} 个备注中删除关键词` +
                  (d.failed?.length ? `，${d.failed.length} 个失败` : '');
      toast(msg, d.failed?.length ? 'info' : 'success');
      hideModal();
      await fetchFiles();
    } else {
      toast('没有文件被修改', 'info');
      hideModal();
    }
  });
}

// ── Comment detail modal ──────────────────────────────────────────────────
function showCommentModal(id) {
  const file = FILE_MAP.get(id);
  if (!file) return;
  _activeFile = file;
  document.getElementById('mCommentTitle').textContent = file.name;
  document.getElementById('mCommentBody').textContent = file.comment;
  document.getElementById('mCommentMeta').innerHTML =
    `<span style="color:var(--text-dim)">格式：</span><span>${escHtml(file.ext)}</span>
     <span style="color:var(--text-dim)">路径：</span><span style="color:var(--text-dim);font-size:11px">${escHtml(file.rel)}</span>`;
  document.getElementById('mCommentEdit').onclick = () => { hideModal(); showEditModal(file.id); };
  document.getElementById('mCommentClear').onclick = () => {
    showConfirm(`确认清空 <strong>${escHtml(file.name)}</strong> 的备注？`, async () => {
      const d = await fetchJSON('/api/clear', {method:'POST', body:{ids:[file.id]}});
      S.selected.delete(file.id);
      onClearResult(d, '已清空 1 条备注');
    });
  };
  showModal('mComment');
}

// ── Modals ────────────────────────────────────────────────────────────────
function showModal(id) {
  document.getElementById('overlay').style.display = '';
  ['mComment','mPattern','mConfirm','mEdit'].forEach(mid => {
    document.getElementById(mid).style.display = mid === id ? '' : 'none';
  });
}
function hideModal() {
  document.getElementById('overlay').style.display = 'none';
  _confirmCb = null;
}
function onOverlayClick(e) { if (e.target === e.currentTarget) hideModal(); }

function showConfirm(msg, cb) {
  document.getElementById('confirmMsg').innerHTML = msg;
  _confirmCb = cb;
  document.getElementById('confirmOkBtn').onclick = async () => { hideModal(); await cb(); };
  showModal('mConfirm');
}

// ── Clear result handling ─────────────────────────────────────────────────
async function onClearResult(d, successMsg) {
  if (d.cleared && d.cleared.length > 0) {
    toast(successMsg + (d.failed?.length ? `，${d.failed.length} 个失败` : ''), 'success');
    S.withComments = Math.max(0, S.withComments - d.cleared.length);
    if (S.page > 1 && S.withComments === 0) S.page = 1;
    await fetchFiles();
  } else if (d.failed?.length > 0) {
    toast(`${d.failed.length} 个文件写入失败`, 'error');
  } else {
    toast('没有文件被修改', 'info');
  }
  hideModal();
}

// ── Toast ─────────────────────────────────────────────────────────────────
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  const stack = document.getElementById('toastStack');
  stack.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 250); }, 3200);
}

// ── Utils ─────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function fetchJSON(url, opts={}) {
  const res = await fetch(url, {
    method: opts.method || 'GET',
    headers: opts.body ? {'Content-Type':'application/json'} : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}
</script>
</body>
</html>"""

@app.route("/")
def index():
    root_str = html.escape(str(G.root)) if G.root else "未设置"
    return make_response(HTML.replace("__ROOT__", root_str), 200,
                         {"Content-Type": "text/html; charset=utf-8"})

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="音乐备注管理应用 (Web)")
    ap.add_argument("dir", metavar="目录", help="音乐根目录")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}"); sys.exit(1)

    _start_scan(root)

    url = f"http://localhost:{args.port}"
    print(f"\n  ⬡  音乐备注管理器")
    print(f"  目录：{root}")
    print(f"  地址：{url}\n")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)

if __name__ == "__main__":
    main()
