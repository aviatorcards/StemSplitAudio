#!/usr/bin/env python3
import os
import sys
import shutil
import threading
import subprocess
import uuid
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, abort, make_response

app = Flask(__name__)


@app.after_request
def no_cache_html(response):
    if response.content_type.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

DOWNLOADS_DIR = Path(__file__).parent / 'downloads'
DOWNLOADS_DIR.mkdir(exist_ok=True)

YT_DLP = shutil.which('yt-dlp') or os.path.expanduser('~/.local/bin/yt-dlp')

try:
    import demucs  # noqa: F401
    DEMUCS_AVAILABLE = True
except ImportError:
    DEMUCS_AVAILABLE = False

tasks = {}
tasks_lock = threading.Lock()


def update_task(task_id, **kwargs):
    with tasks_lock:
        if task_id in tasks:
            tasks[task_id].update(kwargs)


def run_download(task_id, url, fmt, split_stems, stem_mode):
    try:
        update_task(task_id, status='downloading', progress='Starting download...')

        output_template = str(DOWNLOADS_DIR / '%(title)s.%(ext)s')
        cmd = [
            YT_DLP, '-x',
            '--audio-format', fmt,
            '--audio-quality', '0',
            '-o', output_template,
            '--no-playlist',
            '--newline',
            url
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)

        downloaded_file = None
        for line in proc.stdout:
            line = line.strip()
            if line:
                update_task(task_id, progress=line)
                if '[ExtractAudio] Destination:' in line:
                    downloaded_file = line.split('Destination: ', 1)[1].strip()
                elif '[download] Destination:' in line and not downloaded_file:
                    candidate = line.split('Destination: ', 1)[1].strip()
                    if any(candidate.lower().endswith(f'.{x}') for x in ('mp3', 'wav', 'flac', 'm4a', 'ogg', 'opus')):
                        downloaded_file = candidate

        proc.wait()

        if proc.returncode != 0:
            update_task(task_id, status='error',
                        error='Download failed. Please check that the URL is correct and try again.')
            return

        # Fallback: find the most recently modified audio file
        if not downloaded_file or not os.path.exists(downloaded_file):
            audio_exts = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus'}
            candidates = sorted(
                [f for f in DOWNLOADS_DIR.iterdir() if f.is_file() and f.suffix.lower() in audio_exts],
                key=lambda f: f.stat().st_mtime, reverse=True
            )
            if candidates:
                downloaded_file = str(candidates[0])

        stem_files = []
        if split_stems and downloaded_file and DEMUCS_AVAILABLE:
            update_task(task_id, status='splitting',
                        progress='Splitting stems — this can take several minutes...')

            demucs_cmd = [sys.executable, '-m', 'demucs', '--mp3']
            if stem_mode == 'two':
                demucs_cmd += ['--two-stems', 'vocals']
            elif stem_mode == 'six':
                demucs_cmd += ['-n', 'htdemucs_6s']  # vocals, drums, bass, other, guitar, piano
            # 'all' uses the default htdemucs model (4 stems)
            demucs_cmd += ['-o', str(DOWNLOADS_DIR), downloaded_file]

            stem_proc = subprocess.Popen(demucs_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
            for line in stem_proc.stdout:
                line = line.strip()
                if line:
                    update_task(task_id, progress=line)
            stem_proc.wait()

            # Find generated stem files — check both htdemucs and htdemucs_6s output dirs
            for model_dir_name in ('htdemucs_6s', 'htdemucs'):
                model_dir = DOWNLOADS_DIR / model_dir_name
                if model_dir.exists():
                    stem_dirs = sorted(model_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
                    if stem_dirs:
                        stem_files = [
                            str(f.relative_to(DOWNLOADS_DIR))
                            for f in sorted(stem_dirs[0].glob('*.mp3'))
                        ]
                        break

        with tasks_lock:
            tasks[task_id].update({
                'status': 'done',
                'progress': 'Complete!',
                'file': os.path.basename(downloaded_file) if downloaded_file else None,
                'stem_files': stem_files,
            })

    except Exception as e:
        update_task(task_id, status='error', error=str(e))


@app.route('/')
def index():
    return render_template('index.html', demucs_available=DEMUCS_AVAILABLE)


@app.route('/player/<path:filename>')
def player(filename):
    safe_path = (DOWNLOADS_DIR / filename).resolve()
    if not str(safe_path).startswith(str(DOWNLOADS_DIR.resolve())):
        abort(403)
    if not safe_path.exists():
        abort(404)
    return render_template('player.html', filename=filename, title=Path(filename).name)


@app.route('/download', methods=['POST'])
def start_download():
    data = request.get_json()
    url = (data.get('url') or '').strip()
    fmt = data.get('format', 'mp3').lower()
    split_stems = bool(data.get('split_stems')) and DEMUCS_AVAILABLE
    stem_mode = data.get('stem_mode', 'two')

    if not url:
        return jsonify({'error': 'Please enter a URL'}), 400
    if fmt not in ('mp3', 'wav', 'flac'):
        fmt = 'mp3'

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {'status': 'starting', 'progress': 'Preparing...', 'file': None,
                          'stem_files': [], 'error': None}

    thread = threading.Thread(target=run_download,
                              args=(task_id, url, fmt, split_stems, stem_mode), daemon=True)
    thread.start()
    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
def task_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(task)


@app.route('/files')
def list_files():
    audio_exts = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus'}
    files = []

    for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in audio_exts:
            files.append({'name': f.name, 'path': f.name, 'type': 'main'})

    for model_dir_name in ('htdemucs', 'htdemucs_6s'):
        model_dir = DOWNLOADS_DIR / model_dir_name
        if model_dir.exists():
            for song_dir in sorted(model_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if song_dir.is_dir():
                    for stem_file in sorted(song_dir.glob('*.mp3')):
                        files.append({
                            'name': f'{song_dir.name} — {stem_file.stem}',
                            'path': str(stem_file.relative_to(DOWNLOADS_DIR)),
                            'type': 'stem'
                        })

    return jsonify(files)


@app.route('/files/<path:filename>')
def serve_file(filename):
    safe_path = (DOWNLOADS_DIR / filename).resolve()
    if not str(safe_path).startswith(str(DOWNLOADS_DIR.resolve())):
        abort(403)
    if not safe_path.exists():
        abort(404)
    return send_file(safe_path, as_attachment=True)


@app.route('/stems', methods=['DELETE'])
def delete_all_stems():
    """Wipe all Demucs output directories."""
    import shutil as _shutil
    for model_dir in ('htdemucs', 'htdemucs_6s'):
        d = DOWNLOADS_DIR / model_dir
        if d.exists():
            _shutil.rmtree(d)
    return jsonify({'ok': True})


@app.route('/files/<path:filename>', methods=['DELETE'])
def delete_file(filename):
    safe_path = (DOWNLOADS_DIR / filename).resolve()
    if not str(safe_path).startswith(str(DOWNLOADS_DIR.resolve())):
        abort(403)
    if safe_path.exists():
        safe_path.unlink()
    return jsonify({'ok': True})


if __name__ == '__main__':
    print(f'Audio Downloader starting...')
    print(f'Open your browser to:  http://localhost:5050')
    print(f'Files saved to:        {DOWNLOADS_DIR}')
    print(f'Stem splitting:        {"available" if DEMUCS_AVAILABLE else "not installed"}')
    app.run(host='0.0.0.0', port=5050, debug=False)
