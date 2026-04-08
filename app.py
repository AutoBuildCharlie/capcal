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
from flask import Flask, jsonify, request, send_from_directory, send_file
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')

ROOT       = Path(__file__).parent
UPLOAD_DIR = ROOT / 'uploads'
OUTPUT_DIR = ROOT / 'outputs'
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY', '')

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
    result = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error'] + args,
        capture_output=True
    )
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
    words = []
    try:
        alts = resp.json()['results']['channels'][0]['alternatives']
        for w in alts[0].get('words', []):
            words.append({
                'word':      w['word'],
                'start':     round(w['start'], 3),
                'end':       round(w['end'], 3),
                'is_filler': w.get('type') == 'filler',
            })
    except Exception:
        pass
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
    'neon':    {'font': 'Arial Black', 'size': 76, 'primary': '&H00F848F0', 'outline': '&H004C1D95', 'back': '&H80000000', 'bold': -1},
    'shadow':  {'font': 'Arial',       'size': 72, 'primary': '&H00FFFFFF', 'outline': '&H00000000', 'back': '&HC0000000', 'bold': -1},
    'pill':    {'font': 'Arial Black', 'size': 68, 'primary': '&H00FFFFFF', 'outline': '&H004C1D95', 'back': '&HFF200050', 'bold': -1},
    'outline': {'font': 'Arial Black', 'size': 76, 'primary': '&H00000000', 'outline': '&H00FFFFFF', 'back': '&H00000000', 'bold': -1},
}


def generate_ass(words, style='classic', pos_x=0.5, pos_y=0.85) -> str:
    s = CAPTION_STYLES.get(style, CAPTION_STYLES['classic'])
    # Convert position % to ASS coordinates (1080x1920)
    ass_x = round(pos_x * 1080)
    ass_y = round(pos_y * 1920)
    pos_tag = f'{{\\an5\\pos({ass_x},{ass_y})}}'

    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{s['font']},{s['size']},{s['primary']},&H000000FF,{s['outline']},{s['back']},{s['bold']},0,0,0,100,100,0,0,1,3,1,5,10,10,0,1\n\n"
        f"[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = ''
    for p in group_words(words):
        t0 = format_ass_time(p['start'])
        t1 = format_ass_time(p['end'])
        lines += f"Dialogue: 0,{t0},{t1},Default,,0,0,0,,{pos_tag}{p['text']}\n"
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
        run_ffmpeg(['-i', str(input_path), '-ss', str(segments[0][0]),
                    '-to', str(segments[0][1]), '-c', 'copy', '-y', str(output_path)])
        off = segments[0][0]
        return [dict(w, start=max(0, w['start']-off), end=max(0, w['end']-off))
                for w in words if w['end'] > segments[0][0] and w['start'] < segments[0][1]]

    tmp = output_path.parent / 'tmp_segs'
    tmp.mkdir(exist_ok=True)
    parts, new_words, t_off = [], [], 0.0

    for i, (s0, s1) in enumerate(segments):
        part = tmp / f'seg_{i}.mp4'
        run_ffmpeg(['-i', str(input_path), '-ss', str(s0), '-to', str(s1),
                    '-c:v', 'libx264', '-c:a', 'aac', '-y', str(part)])
        for w in words:
            if s0 <= w['start'] < s1:
                new_words.append(dict(w,
                    start=round(t_off + w['start'] - s0, 3),
                    end=round(t_off + min(w['end'], s1) - s0, 3)))
        t_off += s1 - s0
        parts.append(part)

    concat = tmp / 'list.txt'
    concat.write_text('\n'.join(f"file '{p}'" for p in parts))
    run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(concat),
                '-c', 'copy', '-y', str(output_path)])
    shutil.rmtree(tmp, ignore_errors=True)
    return new_words


# ── Processing pipeline ────────────────────────────────────────────────────

def do_process(job_id: str, options: dict):
    try:
        job_dir    = UPLOAD_DIR / job_id
        out_dir    = OUTPUT_DIR / job_id
        out_dir.mkdir(exist_ok=True)
        current    = job_dir / 'original.mp4'
        words      = job_get(job_id).get('words', [])

        def step(msg, pct):
            job_set(job_id, step=msg, progress=pct)

        # 1 — Trim
        step('Trimming video...', 10)
        t_start = float(options.get('trim_start', 0))
        t_end   = options.get('trim_end')
        if t_start > 0 or t_end:
            trimmed = job_dir / 'trimmed.mp4'
            args = ['-i', str(current)]
            if t_start: args += ['-ss', str(t_start)]
            if t_end:   args += ['-to', str(t_end)]
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
        music_path = job_dir / 'music'
        if music_path.exists() and options.get('add_music'):
            step('Adding music...', 58)
            music_out = job_dir / 'with_music.mp4'
            run_ffmpeg(['-i', str(current), '-i', str(music_path),
                        '-filter_complex',
                        '[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.25',
                        '-c:v', 'copy', '-y', str(music_out)])
            current = music_out

        # 5 — Burn captions
        if options.get('captions') and words:
            step('Burning captions...', 68)
            ass_path = job_dir / 'captions.ass'
            pos      = options.get('caption_pos', {'x': 0.5, 'y': 0.85})
            ass_path.write_text(generate_ass(
                words,
                options.get('caption_style', 'classic'),
                float(pos.get('x', 0.5)),
                float(pos.get('y', 0.85)),
            ))
            cap_out  = job_dir / 'captioned.mp4'
            ass_safe = str(ass_path).replace('\\', '/').replace(':', '\\:')
            run_ffmpeg(['-i', str(current), '-vf', f"ass='{ass_safe}'",
                        '-c:a', 'copy', '-y', str(cap_out)])
            current = cap_out

        # 6 — Aspect ratio
        aspect = options.get('aspect_ratio', '9:16')
        scale_filter = None
        if aspect == '1:1':
            scale_filter = 'scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black'
        elif aspect == '16:9':
            scale_filter = 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black'

        # 7 — Final encode
        step('Exporting...', 82)
        quality_map = {
            'low':    ('28', 'ultrafast'),
            'medium': ('23', 'fast'),
            'high':   ('18', 'slow'),
        }
        crf, preset = quality_map.get(options.get('quality', 'medium'), ('23', 'fast'))
        title = re.sub(r'[^\w\s-]', '', options.get('title', 'capcal')).strip().replace(' ', '_') or 'capcal'
        final = out_dir / f'{title}.mp4'

        vf_args = []
        if scale_filter:
            vf_args = ['-vf', scale_filter]

        run_ffmpeg(['-i', str(current)] + vf_args + [
                    '-c:v', 'libx264', '-preset', preset, '-crf', crf,
                    '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart',
                    '-y', str(final)])

        job_set(job_id, status='done', progress=100, step='Done', filename=final.name)

    except Exception as e:
        job_set(job_id, status='error', error=str(e), step='Error')


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
    video  = request.files['video']
    job_id = str(uuid.uuid4())
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
    job = job_get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(job)


@app.route('/api/upload-music/<job_id>', methods=['POST'])
def upload_music(job_id):
    if 'music' not in request.files:
        return jsonify({'error': 'No music file'}), 400
    job_dir = UPLOAD_DIR / job_id
    if not job_dir.exists():
        return jsonify({'error': 'Job not found'}), 404
    request.files['music'].save(str(job_dir / 'music'))
    return jsonify({'status': 'ok'})


@app.route('/api/process/<job_id>', methods=['POST'])
def process(job_id):
    job = job_get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') == 'processing':
        return jsonify({'error': 'Already processing'}), 400
    options = request.json or {}
    job_set(job_id, status='processing', step='Starting...', progress=5)
    threading.Thread(target=do_process, args=(job_id, options), daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/download/<job_id>')
def download(job_id):
    job = job_get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Not ready'}), 404
    path = OUTPUT_DIR / job_id / job.get('filename', 'capcal.mp4')
    if not path.exists():
        return jsonify({'error': 'File not found'}), 404
    return send_file(str(path), as_attachment=True, download_name=path.name)


@app.route('/api/thumbnail/<job_id>')
def thumbnail(job_id):
    t    = float(request.args.get('t', 0))
    path = UPLOAD_DIR / job_id / 'original.mp4'
    if not path.exists():
        return jsonify({'error': 'Not found'}), 404
    result = subprocess.run(
        ['ffmpeg', '-ss', str(t), '-i', str(path),
         '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
        capture_output=True
    )
    if not result.stdout:
        return jsonify({'error': 'Could not extract frame'}), 500
    from flask import Response
    return Response(result.stdout, mimetype='image/png')


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup(job_id):
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
