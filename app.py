#!/usr/bin/env python3
# --- Mock Beartype for Python 3.14 compatibility ---
import sys
import typing
from unittest.mock import MagicMock

sys.modules['beartype.typing'] = typing

class MockBeartype(MagicMock):
    def __call__(self, obj=None, *args, **kwargs):
        if obj is not None:
            return obj
        def decorator(f):
            return f
        return decorator

mock_beartype_mod = MockBeartype()
mock_beartype_mod.beartype = lambda obj=None, *args, **kwargs: obj if obj is not None else (lambda f: f)
sys.modules['beartype'] = mock_beartype_mod
# ----------------------------------------------------

import os
import shutil
import threading
import subprocess
import uuid
import io
import zipfile
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

try:
    from audio_separator.separator import Separator  # noqa: F401
    AUDIO_SEPARATOR_AVAILABLE = True
except ImportError:
    AUDIO_SEPARATOR_AVAILABLE = False

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
        if split_stems and downloaded_file:
            if stem_mode in ('two', 'bs_roformer', 'kim_vocal') and AUDIO_SEPARATOR_AVAILABLE:
                model_filename = 'model_bs_roformer_ep_317_sdr_12.9755.ckpt' if stem_mode in ('two', 'bs_roformer') else 'Kim_Vocal_2.onnx'
                model_dir_name = 'bs_roformer' if stem_mode in ('two', 'bs_roformer') else 'kim_vocal'
                
                update_task(task_id, status='splitting',
                            progress=f'Splitting stems using {model_dir_name.replace("_", " ").title()} AI — this can take a moment...')
                
                try:
                    song_name = Path(downloaded_file).stem
                    output_dir = DOWNLOADS_DIR / model_dir_name / song_name
                    output_dir.mkdir(parents=True, exist_ok=True)
                    
                    separator = Separator(
                        output_dir=str(output_dir),
                        output_format=fmt.upper(),
                        log_level=10
                    )
                    separator.load_model(model_filename=model_filename)
                    generated_files = separator.separate(downloaded_file)
                    
                    for filename in generated_files:
                        full_path = output_dir / filename
                        suffix = full_path.suffix.lower()
                        
                        if '(Vocals)' in filename:
                            new_name = f'vocals{suffix}'
                        elif '(Instrumental)' in filename:
                            new_name = f'instrumental{suffix}'
                        else:
                            new_name = filename.lower()
                            
                        if full_path.exists():
                            new_path = output_dir / new_name
                            if new_path.exists():
                                new_path.unlink()
                            full_path.rename(new_path)
                            
                    stem_files = [
                        str((Path(model_dir_name) / song_name / f.name))
                        for f in output_dir.iterdir() if f.is_file() and not f.name.startswith('.')
                    ]
                except Exception as ex:
                    print(f"Audio-separator failed: {ex}")
                    update_task(task_id, progress=f"Separation failed: {ex}")
            elif DEMUCS_AVAILABLE:
                update_task(task_id, status='splitting',
                            progress='Splitting stems with Demucs — this can take several minutes...')

                demucs_cmd = [sys.executable, '-m', 'demucs', '--mp3']
                if stem_mode == 'two':
                    demucs_cmd += ['--two-stems', 'vocals']
                elif stem_mode in ('six', 'demucs_6s'):
                    demucs_cmd += ['-n', 'htdemucs_6s']
                # 'all' or 'demucs_4s' uses the default htdemucs model (4 stems)
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
    return render_template('index.html', demucs_available=DEMUCS_AVAILABLE, audio_separator_available=AUDIO_SEPARATOR_AVAILABLE)


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
    split_stems = bool(data.get('split_stems')) and (DEMUCS_AVAILABLE or AUDIO_SEPARATOR_AVAILABLE)
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
    songs = {}

    def get_or_create_song(name):
        if name not in songs:
            songs[name] = {
                'song_name': name,
                'main_file': None,
                'stems': [],
                'has_stems': False,
                'mtime': 0
            }
        return songs[name]

    if DOWNLOADS_DIR.exists():
        for f in DOWNLOADS_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in audio_exts:
                song_name = f.stem
                song = get_or_create_song(song_name)
                song['main_file'] = {
                    'name': f.name,
                    'path': f.name,
                    'type': 'main'
                }
                song['mtime'] = max(song['mtime'], f.stat().st_mtime)

        for model_dir_name in ('htdemucs', 'htdemucs_6s', 'bs_roformer', 'kim_vocal'):
            model_dir = DOWNLOADS_DIR / model_dir_name
            if model_dir.exists():
                for song_dir in model_dir.iterdir():
                    if song_dir.is_dir():
                        song_name = song_dir.name
                        song = get_or_create_song(song_name)
                        song['mtime'] = max(song['mtime'], song_dir.stat().st_mtime)
                        
                        for stem_file in sorted(song_dir.iterdir()):
                            if stem_file.is_file() and stem_file.suffix.lower() in audio_exts and not stem_file.name.startswith('.'):
                                song['stems'].append({
                                    'name': stem_file.stem,
                                    'path': str(stem_file.relative_to(DOWNLOADS_DIR)),
                                    'type': 'stem'
                                })
                                song['has_stems'] = True
                                song['mtime'] = max(song['mtime'], stem_file.stat().st_mtime)

    # Sort songs by mtime descending
    sorted_songs = sorted(songs.values(), key=lambda x: x['mtime'], reverse=True)
    
    # Remove mtime before sending JSON
    for song in sorted_songs:
        song.pop('mtime', None)
        
    return jsonify(sorted_songs)


@app.route('/zip/all')
def download_all_zip():
    memory_file = io.BytesIO()
    has_files = False
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(DOWNLOADS_DIR):
            for file in files:
                if file.startswith('.'):
                    continue
                file_path = Path(root) / file
                archive_name = file_path.relative_to(DOWNLOADS_DIR)
                zipf.write(file_path, arcname=archive_name)
                has_files = True
                
    if not has_files:
        abort(404, "No files found to download")
        
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name="stemsplit_all_files.zip"
    )


@app.route('/zip/stems/<path:song_name>')
def download_stems_zip(song_name):
    if '..' in song_name or song_name.startswith('/'):
        abort(400, "Invalid song name")
        
    memory_file = io.BytesIO()
    has_files = False
    audio_exts = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus'}
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for model_dir_name in ('htdemucs', 'htdemucs_6s', 'bs_roformer', 'kim_vocal'):
            song_dir = DOWNLOADS_DIR / model_dir_name / song_name
            if song_dir.exists() and song_dir.is_dir():
                for f in song_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in audio_exts and not f.name.startswith('.'):
                        zipf.write(f, arcname=f.name)
                        has_files = True
                    
    if not has_files:
        abort(404, "No stems found for this song")
        
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{song_name}_stems.zip"
    )


@app.route('/zip/song/<path:song_name>')
def download_song_zip(song_name):
    if '..' in song_name or song_name.startswith('/'):
        abort(400, "Invalid song name")
        
    memory_file = io.BytesIO()
    has_files = False
    audio_exts = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus'}
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Main file
        for ext in audio_exts:
            main_file = DOWNLOADS_DIR / f"{song_name}{ext}"
            if main_file.exists():
                zipf.write(main_file, arcname=main_file.name)
                has_files = True
                break
                
        # Stems
        for model_dir_name in ('htdemucs', 'htdemucs_6s', 'bs_roformer', 'kim_vocal'):
            song_dir = DOWNLOADS_DIR / model_dir_name / song_name
            if song_dir.exists() and song_dir.is_dir():
                for f in song_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in audio_exts and not f.name.startswith('.'):
                        zipf.write(f, arcname=f"stems/{f.name}")
                        has_files = True
                    
    if not has_files:
        abort(404, "No files found for this song")
        
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{song_name}.zip"
    )


@app.route('/song/<path:song_name>', methods=['DELETE'])
def delete_song(song_name):
    if '..' in song_name or song_name.startswith('/'):
        abort(400, "Invalid song name")
        
    # Delete main files
    audio_exts = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus'}
    for ext in audio_exts:
        main_file = DOWNLOADS_DIR / f"{song_name}{ext}"
        if main_file.exists():
            main_file.unlink()
            
    # Delete stem directories
    for model_dir_name in ('htdemucs', 'htdemucs_6s', 'bs_roformer', 'kim_vocal'):
        song_dir = DOWNLOADS_DIR / model_dir_name / song_name
        if song_dir.exists() and song_dir.is_dir():
            shutil.rmtree(song_dir)
            
    return jsonify({'ok': True})



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
    """Wipe all Demucs/audio-separator output directories."""
    import shutil as _shutil
    for model_dir in ('htdemucs', 'htdemucs_6s', 'bs_roformer', 'kim_vocal'):
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
    print(f'StemSplit starting...')
    print(f'Open your browser to:  http://localhost:5050')
    print(f'Files saved to:        {DOWNLOADS_DIR}')
    print(f'Demucs engine:         {"available" if DEMUCS_AVAILABLE else "not installed"}')
    print(f'Audio-separator:       {"available" if AUDIO_SEPARATOR_AVAILABLE else "not installed"}')
    app.run(host='0.0.0.0', port=5050, debug=False)

