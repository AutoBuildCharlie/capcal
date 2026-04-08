"""
CapCal — AI Video Editor
Premium mobile PWA for short-form content creators.
"""

import os
import re
import uuid
import shutil
import subprocess
import threading
import httpx
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file, Response
from werkzeug.exceptions import RequestEntityTooLarge
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB cap

ROOT       = Path(__file__).parent
UPLOAD_DIR = ROOT / 'uploads'
OUTPUT_DIR = ROOT / 'outputs'
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY', '')

# UUID v4 validation — prevents path traversal via crafted job_id
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
def valid_job_id(jid: str) -> bool:
    return bool(_UUID_RE.match(jid))


# ── File magic bytes validation ────────────────────────────────────────────

def _check_magic(file_storage, read_bytes=12):
    file_storage.seek(0)
    header = file_storage.read(read_bytes)
    file_storage.seek(0)
    return header

def is_valid_video(file_storage) -> bool:
    h = _check_magic(file_storage)
    if len(h) >= 8 and h[4:8] == b'ftyp':   # MP4/MOV/M4A
        return True
    return h[:4] in (b'RIFF', b'\x1a\x45\xdf\xa3',
                     b'\x00\x00\x01\xb3', b'\x00\x00\x01\xba')

def is_valid_audio(file_storage) -> bool:
    h = _check_magic(file_storage)
    if len(h) >= 8 and h[4:8] == b'ftyp':   # M4A
        return True
    return (h[:3] == b'ID3' or h[:4] in (b'RIFF', b'OggS', b'fLaC')
            or h[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2'))


# ── In-memory job store ────────────────────────────────────────────────────
_jobs = {}
_lock = threading.Lock()

def job_get(jid):
    with _lock:
        return dict(_jobs.get(jid, {}))

def job_set(jid, **kw):
    with _lock:
        if jid not in _jobs:
            _jobs[jid] = {}
        _jobs[jid].update(kw)


# ── FFmpeg helpers ─────────────────────────────────────────────────────────

def run_ffmpeg(args):
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
            capture_output=True
        )
    except FileNotFoundError:
        raise RuntimeError('FFmpeg is not installed or not on PATH. Install FFmpeg and restart.')
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode())


def extract_audio(video_path: Path, audio_path: Path):
    run_ffmpeg(['-i', str(video_path),
                '-vn', '-acodec', 'pcm_s16le',
                '-ar', '16000', '-ac', '1',
                '-y', str(audio_path)])


# ── Transcription ──────────────────────────────────────────────────────────

def transcribe(audio_path: Path) -> list:
    if not DEEPGRAM_API_KEY:
        return []
    with open(audio_path, 'rb') as f:
        data = f.read()
    resp = httpx.post(
        'https://api.deepgram.com/v1/listen',
        headers={'Authorization': f'Token {DEEPGRAM_API_KEY}',
                 'Content-Type': 'audio/wav'},
        params={'model': 'nova-3', 'smart_format': 'true',
                'punctuate': 'true', 'filler_words': 'true', 'words': 'true'},
        content=data,
        timeout=120.0,
    )
    resp.raise_for_status()   # raises on 4xx/5xx — surfaces bad API key, rate limits, etc.
    words = []
    try:
        alts = resp.json()['results']['channels'][0]['alternatives']
        for w in alts[0].get('words', []):
            # Sanitize to prevent ASS subtitle injection
            word_text = re.sub(r'[{}\n\r]', '', w.get('word', ''))
            words.append({
                'word':      word_text,
                'start':     round(float(w['start']), 3),
                'end':       round(float(w['end']), 3),
                'is_filler': w.get('type') == 'filler',
            })
    except Exception as e:
        raise RuntimeError(f'Deepgram parse error: {e} | body: {resp.text[:200]}')
    return words


# ── Caption helpers ────────────────────────────────────────────────────────

def format_ass_time(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def group_words(words, max_words=4, max_gap=0.6):
    if not words:
        return []
    phrases, buf = [], [words[0]]
    for w in words[1:]:
        gap = w['start'] - buf[-1]['end']
        if len(buf) >= max_words or gap > max_gap:
            phrases.append(buf)
            buf = []
        buf.append(w)
    if buf:
        phrases.append(buf)
    return [{'start': g[0]['start'], 'end': g[-1]['end'],
              'text': ' '.join(x['word'] for x in g)} for g in phrases]


CAPTION_STYLES = {
    'classic': {'font': 'Arial',       'size': 72, 'primary': '&H00FFFFFF', 'outline': '&H00000000', 'back': '&H80000000', 'bold': -1},
    'gold':    {'font': 'Arial Black', 'size': 76, 'primary': '&H0037B4F5', 'outline': '&H00200050', 'back': '&HA0000000', 'bold': -1},
    'bold':    {'font': 'Arial Black', 'size': 84, 'primary': '&H00FFFFFF', 'outline': '&H004C1D95', 'back': '&H80000000', 'bold': -1},
    'minimal': {'font': 'Arial',       'size': 58, 'primary': '&H00FFFFFF', 'outline': '&H00000000', 'back': '&H00000000', 'bold':  0},
    'neon':    {'font': 'Arial Black', 'size': 76, 'primary': '&HF848F0',   'outline': '&H004C1D95', 'back': '&H80000000', 'bold': -1},
    'shadow':  {'font': 'Arial',       'size': 72, 'primary': '&H00FFFFFF', 'outline': '&H00000000', 'back': '&HC0000000', 'bold': -1},
    'pill':    {'font': 'Arial Black', 'size': 68, 'primary': '&H00FFFFFF', 'outline': '&H004C1D95', 'back': '&HFF200050', 'bold': -1},
    'outline': {'font': 'Arial Black', 'size': 76, 'primary': '&H00000000', 'outline': '&H00FFFFFF', 'back': '&H00000000', 'bold': -1},
}

# Output resolution per aspect ratio — used for ASS PlayRes and scale filters
_ASPECT_RES = {'9:16': (1080, 1920), '1:1': (1080, 1080), '16:9': (1920, 1080)}


def generate_ass(words, style='classic', pos_x=0.5, pos_y=0.85,
                 res_x=1080, res_y=1920) -> str:
    s = CAPTION_STYLES.get(style, CAPTION_STYLES['classic'])
    ass_x = round(pos_x * res_x)
    ass_y = round(pos_y * res_y)
    pos_tag = f'{{\\an5\\pos({ass_x},{ass_y})}}'

    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {res_x}\nPlayResY: {res_y}\nWrapStyle: 0\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{s['font']},{s['size']},{s['primary']},&H000000FF,{s['outline']},{s['back']},{s['bold']},0,0,0,100,100,0,0,1,3,1,5,10,10,0,1\n\n"
        f"[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = ''
    for p in group_words(words):
        t0 = format_ass_time(p['start'])
        t1 = format_ass_time(p['end'])
        # Second-layer ASS injection guard
        safe_text = p['text'].replace('{', '').replace('}', '').replace('\n', ' ').replace('\r', '')
        lines += f"Dialogue: 0,{t0},{t1},Default,,0,0,0,,{pos_tag}{safe_text}\n"
    return header + lines


# ── Silence removal ────────────────────────────────────────────────────────

def remove_silences(input_path: Path, output_path: Path,
                    words: list, min_gap: float = 0.5) -> list:
    if not words:
        shutil.copy(input_path, output_path)
        return words

    buf = 0.15
    segments, seg = [], [max(0, words[0]['start'] - buf), words[0]['end'] + buf]
    for w in words[1:]:
        gap = w['start'] - seg[1] + buf
        if gap > min_gap:
            segments.append(tuple(seg))
            seg = [max(0, w['start'] - buf), w['end'] + buf]
        else:
            seg[1] = w['end'] + buf
    segments.append(tuple(seg))

    if len(segments) == 1:
        s0, s1 = segments[0]
        # Input-side seek + re-encode (not stream copy) to avoid keyframe snap
        run_ffmpeg(['-ss', str(s0), '-i', str(input_path),
                    '-t', str(s1 - s0),
                    '-c:v', 'libx264', '-c:a', 'aac', '-y', str(output_path)])
        return [dict(w, start=max(0, w['start']-s0), end=max(0, w['end']-s0))
                for w in words if w['end'] > s0 and w['start'] < s1]

    tmp = output_path.parent / 'tmp_segs'
    tmp.mkdir(exist_ok=True)
    parts, new_words, t_off = [], [], 0.0

    for i, (s0, s1) in enumerate(segments):
        part = tmp / f'seg_{i}.mp4'
        # Input-side seek for each segment
        run_ffmpeg(['-ss', str(s0), '-i', str(input_path), '-t', str(s1 - s0),
                    '-c:v', 'libx264', '-c:a', 'aac', '-y', str(part)])
        for w in words:
            if s0 <= w['start'] < s1:
                new_words.append(dict(w,
                    start=round(t_off + w['start'] - s0, 3),
                    end=round(t_off + min(w['end'], s1) - s0, 3)))
        t_off += s1 - s0
        parts.append(part)

    concat = tmp / 'list.txt'
    # Use .as_posix() — backslashes in concat list break FFmpeg on Windows
    concat.write_text('\n'.join(f"file '{p.as_posix()}'" for p in parts))
    run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(concat),
                '-c', 'copy', '-y', str(output_path)])
    shutil.rmtree(tmp, ignore_errors=True)
    return new_words


# ── Processing pipeline ────────────────────────────────────────────────────

def do_process(job_id: str, options: dict):
    try:
        job_dir = UPLOAD_DIR / job_id
        out_dir = OUTPUT_DIR / job_id
        out_dir.mkdir(exist_ok=True)
        current = job_dir / 'original.mp4'
        words   = job_get(job_id).get('words', [])

        def step(msg, pct):
            job_set(job_id, step=msg, progress=pct)

        # 1 — Trim
        step('Trimming video...', 10)
        t_start = float(options.get('trim_start', 0))
        t_end   = options.get('trim_end')
        if t_start > 0 or t_end:
            trimmed = job_dir / 'trimmed.mp4'
            # Input-side seek (-ss before -i) for frame-accurate trim
            args = []
            if t_start:
                args += ['-ss', str(t_start)]
            args += ['-i', str(current)]
            if t_end:
                args += ['-t', str(float(t_end) - t_start)]
            args += ['-c', 'copy', '-y', str(trimmed)]
            run_ffmpeg(args)
            current = trimmed
            te = float(t_end) if t_end else float('inf')
            words = [dict(w, start=max(0, w['start']-t_start),
                          end=max(0, w['end']-t_start))
                     for w in words if w['end'] > t_start and w['start'] < te]

        # 2 — Remove silences
        if options.get('remove_silences') and words:
            step('Removing silences...', 28)
            desi = job_dir / 'desilenced.mp4'
            words = remove_silences(current, desi, words)
            current = desi

        # 3 — Noise reduction
        if options.get('noise_reduction'):
            step('Cleaning audio...', 45)
            den = job_dir / 'denoised.mp4'
            run_ffmpeg(['-i', str(current), '-af', 'afftdn=nf=-25',
                        '-c:v', 'copy', '-y', str(den)])
            current = den

        # 4 — Add music
        music_ext  = job_get(job_id).get('music_ext', '')
        music_path = job_dir / f'music{music_ext}'
        if music_path.exists() and options.get('add_music'):
            step('Adding music...', 58)
            music_out = job_dir / 'with_music.mp4'
            run_ffmpeg(['-i', str(current), '-i', str(music_path),
                        '-filter_complex',
                        '[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.25',
                        '-c:v', 'copy', '-y', str(music_out)])
            current = music_out

        # 5 — Final encode: combine scale + captions in one pass (avoids double-encode)
        step('Exporting...', 70)
        aspect = options.get('aspect_ratio', '9:16')
        res_x, res_y = _ASPECT_RES.get(aspect, (1080, 1920))

        vf_parts = []
        if aspect == '1:1':
            vf_parts.append('scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black')
        elif aspect == '16:9':
            vf_parts.append('scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black')

        if options.get('captions') and words:
            step('Burning captions...', 78)
            ass_path = job_dir / 'captions.ass'
            pos = options.get('caption_pos', {'x': 0.5, 'y': 0.85})
            try:
                pos_x = max(0.0, min(1.0, float(pos.get('x', 0.5))))
                pos_y = max(0.0, min(1.0, float(pos.get('y', 0.85))))
            except (ValueError, TypeError):
                pos_x, pos_y = 0.5, 0.85
            ass_path.write_text(generate_ass(
                words,
                options.get('caption_style', 'classic'),
                pos_x, pos_y,
                res_x=res_x, res_y=res_y,
            ))
            # Backslashes → forward slashes first, then escape drive colon
            ass_safe = str(ass_path).replace('\\', '/').replace(':', '\\:')
            vf_parts.append(f"ass='{ass_safe}'")

        quality_map = {
            'low':    ('28', 'ultrafast'),
            'medium': ('23', 'fast'),
            'high':   ('18', 'slow'),
        }
        crf, preset = quality_map.get(options.get('quality', 'medium'), ('23', 'fast'))
        title = re.sub(r'[^\w\s-]', '', options.get('title', 'capcal')).strip()[:100].replace(' ', '_') or 'capcal'
        final = out_dir / f'{title}.mp4'

        step('Exporting...', 82)
        vf_args = ['-vf', ','.join(vf_parts)] if vf_parts else []
        run_ffmpeg(['-i', str(current)] + vf_args + [
                    '-c:v', 'libx264', '-preset', preset, '-crf', crf,
                    '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart',
                    '-y', str(final)])

        job_set(job_id, status='done', progress=100, step='Done', filename=final.name)

    except Exception as e:
        job_set(job_id, status='error', error=str(e), step='Error')


# ── Error handlers ─────────────────────────────────────────────────────────

@app.errorhandler(RequestEntityTooLarge)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum upload size is 500 MB.'}), 413


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def sw():
    return send_from_directory('static', 'sw.js')


@app.route('/api/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify({'error': 'No video file'}), 400
    video = request.files['video']
    if not is_valid_video(video):
        return jsonify({'error': 'Invalid video file format'}), 400
    job_id  = str(uuid.uuid4())
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir()
    video.save(str(job_dir / 'original.mp4'))
    job_set(job_id, status='transcribing', step='Analyzing audio...', progress=0, words=[])

    def transcribe_bg():
        try:
            ap = job_dir / 'audio.wav'
            extract_audio(job_dir / 'original.mp4', ap)
            words = transcribe(ap)
            job_set(job_id, status='ready', words=words, step='Ready')
        except Exception as e:
            job_set(job_id, status='error', error=str(e))

    threading.Thread(target=transcribe_bg, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/job/<job_id>')
def get_job(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = job_get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(job)


@app.route('/api/upload-music/<job_id>', methods=['POST'])
def upload_music(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    if 'music' not in request.files:
        return jsonify({'error': 'No music file'}), 400
    job_dir = UPLOAD_DIR / job_id
    if not job_dir.exists():
        return jsonify({'error': 'Job not found'}), 404
    music_file = request.files['music']
    if not is_valid_audio(music_file):
        return jsonify({'error': 'Invalid audio file format'}), 400
    ext = Path(music_file.filename).suffix or '.mp3'
    music_file.save(str(job_dir / f'music{ext}'))
    job_set(job_id, music_ext=ext)
    return jsonify({'status': 'ok'})


@app.route('/api/process/<job_id>', methods=['POST'])
def process(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = job_get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') in ('transcribing', 'processing'):
        return jsonify({'error': 'Job not ready yet'}), 400
    options = request.get_json(silent=True) or {}
    job_set(job_id, status='processing', step='Starting...', progress=5)
    threading.Thread(target=do_process, args=(job_id, options), daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/download/<job_id>')
def download(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = job_get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Not ready'}), 404
    path = OUTPUT_DIR / job_id / job.get('filename', 'capcal.mp4')
    if not path.exists():
        return jsonify({'error': 'File not found'}), 404
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route('/api/thumbnail/<job_id>')
def thumbnail(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    try:
        t = float(request.args.get('t', 0))
        if t < 0:
            t = 0.0
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid timestamp'}), 400
    path = UPLOAD_DIR / job_id / 'original.mp4'
    if not path.exists():
        return jsonify({'error': 'Not found'}), 404
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-ss', str(t), '-i', str(path),
             '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
            capture_output=True
        )
    except FileNotFoundError:
        return jsonify({'error': 'FFmpeg not installed'}), 500
    if result.returncode != 0 or not result.stdout:
        app.logger.error('Thumbnail extraction failed: %s', result.stderr.decode())
        return jsonify({'error': 'Could not extract frame'}), 500
    return Response(result.stdout, mimetype='image/png')


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
    if not valid_job_id(job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    job = job_get(job_id)
    if job.get('status') == 'processing':
        return jsonify({'error': 'Job still processing'}), 409
    shutil.rmtree(UPLOAD_DIR / job_id, ignore_errors=True)
    shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
    with _lock:
        _jobs.pop(job_id, None)
    return jsonify({'status': 'cleaned'})


if __name__ == '__main__':
    print('=' * 50)
    print('  CAPCAL — AI Video Editor')
    print('=' * 50)
    print('  Open: http://localhost:5001')
    print('  Stop: Ctrl+C')
    print()
    port = int(os.getenv('PORT', 5001))
    app.run(debug=False, port=port, host='0.0.0.0')
