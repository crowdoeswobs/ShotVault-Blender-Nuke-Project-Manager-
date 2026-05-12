import sys
import os
import re
import json
import subprocess
import threading
import socket
import time

from flask import Flask, jsonify, request, send_from_directory
import webview

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# ── File watcher ───────────────────────────────────────────────────────────────
_observer       = None
_rescan_pending = False
_rescan_timer   = None

def _schedule_rescan():
    global _rescan_pending, _rescan_timer
    _rescan_pending = True
    if _rescan_timer:
        _rescan_timer.cancel()
    _rescan_timer = threading.Timer(2.0, _do_pending_rescan)
    _rescan_timer.daemon = True
    _rescan_timer.start()

def _do_pending_rescan():
    global _rescan_pending
    _rescan_pending = True  # JS polls /api/rescan_pending to pick this up

class _ShotWatcher(FileSystemEventHandler):
    def on_created(self, event):  _schedule_rescan()
    def on_deleted(self, event):  _schedule_rescan()
    def on_moved(self, event):    _schedule_rescan()

def start_watching(path):
    global _observer
    stop_watching()
    if not WATCHDOG_AVAILABLE or not path or not os.path.isdir(path):
        return
    _observer = Observer()
    _observer.schedule(_ShotWatcher(), path, recursive=True)
    _observer.daemon = True
    _observer.start()

def stop_watching():
    global _observer
    if _observer:
        try: _observer.stop(); _observer.join(timeout=1)
        except Exception: pass
        _observer = None

# ── Path helpers for PyInstaller ──────────────────────────────────────────────
def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def data_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(data_dir(), 'shotvault_config.json')

STATUS_DEFAULTS = {
    'animation': 'waiting',
    'render':    'not_started',
    'comp':      'waiting_renders',
    'finished':  'in_progress',
}

_current_root = ''  # set when a project is scanned

def status_file():
    if _current_root and os.path.isdir(_current_root):
        return os.path.join(_current_root, 'shotvault_status.json')
    return os.path.join(data_dir(), 'shotvault_status.json')

def load_status():
    path = status_file()
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_status(data):
    with open(status_file(), 'w') as f:
        json.dump(data, f, indent=2)

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    defaults = {'blender_path': '', 'nuke_path': '', 'last_root': '', 'theme': 'dark', 'recent_roots': []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__,
            static_folder=resource_path('static'),
            template_folder=resource_path('templates'))
app.config['JSON_SORT_KEYS'] = False

_window = None  # set after webview.create_window

@app.route('/')
def index():
    return send_from_directory(resource_path('templates'), 'index.html')

@app.route('/api/config', methods=['GET'])
def api_get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def api_save_config():
    cfg = load_config()
    cfg.update(request.json)
    save_config(cfg)
    return jsonify({'ok': True})

@app.route('/api/rescan_pending', methods=['GET'])
def api_rescan_pending():
    global _rescan_pending
    if _rescan_pending:
        _rescan_pending = False
        return jsonify({'pending': True})
    return jsonify({'pending': False})

@app.route('/api/scan', methods=['POST'])
def api_scan():
    global _current_root
    root = request.json.get('root', '').strip()
    if not root or not os.path.isdir(root):
        return jsonify({'error': 'Invalid folder path'}), 400

    _current_root = root

    cfg = load_config()
    cfg['last_root'] = root
    recents = [r for r in cfg.get('recent_roots', []) if r != root]
    cfg['recent_roots'] = [root] + recents[:7]  # keep max 8
    save_config(cfg)

    sequences = []
    shots_dir = os.path.join(root, 'shots')
    scan_root = shots_dir if os.path.isdir(shots_dir) else root

    try:
        seq_names = sorted(
            e for e in os.listdir(scan_root)
            if os.path.isdir(os.path.join(scan_root, e)) and not e.startswith('.')
        )
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403

    for seq_name in seq_names:
        seq_path = os.path.join(scan_root, seq_name)
        shots = []
        try:
            shot_names = sorted(
                e for e in os.listdir(seq_path)
                if os.path.isdir(os.path.join(seq_path, e))
                and not e.startswith('.')
                and e.lower() != 'common'
            )
        except PermissionError:
            continue

        for shot_name in shot_names:
            shot_path = os.path.join(seq_path, shot_name)
            work_path = os.path.join(shot_path, 'work')
            files = []
            if os.path.isdir(work_path):
                collect_files(work_path, files, shot_path)
            shots.append({'shot': shot_name, 'path': shot_path, 'files': files})

        if shots:
            sequences.append({
                'seq': seq_name,
                'shots': shots,
                'shot_count': len(shots),
                'file_count': sum(len(s['files']) for s in shots)
            })

    # Watch the shots folder for changes
    start_watching(scan_root)

    return jsonify({
        'root': root,
        'sequences': sequences,
        'total_shots': sum(s['shot_count'] for s in sequences),
        'total_files': sum(s['file_count'] for s in sequences),
    })

_BACKUP_RE = re.compile(r'\.blend\d+$', re.IGNORECASE)

def _is_project_file(name):
    if _BACKUP_RE.search(name):
        return False
    if name.endswith('.blend'):
        return True
    if name.endswith('.nk') or name.endswith('.nknc'):
        return True
    return False

def collect_files(base_path, results, shot_root):
    try:
        entries = sorted(os.listdir(base_path))
    except PermissionError:
        return
    for name in entries:
        if name.startswith('.'):
            continue
        full = os.path.join(base_path, name)
        if os.path.isdir(full):
            collect_files(full, results, shot_root)
        elif _is_project_file(name):
            rel = os.path.relpath(full, shot_root)
            results.append({
                'name': name,
                'path': full,
                'rel': rel,
                'subpath': os.path.dirname(rel) or '',
                'size_kb': os.path.getsize(full) // 1024,
                'mtime': os.path.getmtime(full),
            })

@app.route('/api/launch', methods=['POST'])
def api_launch():
    file_path = request.json.get('path', '')
    file_type = request.json.get('file_type', 'blend')
    cfg = load_config()

    if not file_path or not os.path.isfile(file_path):
        return jsonify({'error': 'File not found: ' + file_path}), 400

    if file_type == 'nuke':
        exe = cfg.get('nuke_path', '').strip()
        if not exe:
            return jsonify({'error': 'Nuke path not configured. Open Settings to set it.'}), 400
        exe_label = 'Nuke'
    else:
        exe = cfg.get('blender_path', '').strip()
        if not exe:
            return jsonify({'error': 'Blender path not configured. Open Settings to set it.'}), 400
        exe_label = 'Blender'

    if not os.path.isfile(exe):
        import shutil
        found = shutil.which('blender' if file_type != 'nuke' else 'nuke')
        if found:
            exe = found
        else:
            return jsonify({'error': f'{exe_label} not found at: {exe}'}), 400

    try:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        subprocess.Popen([exe, file_path], creationflags=flags)
        return jsonify({'ok': True, 'launched': os.path.basename(file_path)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/whoami', methods=['GET'])
def api_whoami():
    import getpass
    try:
        name = getpass.getuser()
    except Exception:
        name = 'there'
    return jsonify({'name': name})

@app.route('/api/status', methods=['GET'])
def api_get_status():
    return jsonify(load_status())

@app.route('/api/status', methods=['POST'])
def api_set_status():
    """Body: { key: "seq/shot", field: "animation", value: "in_progress" }"""
    body  = request.json
    key   = body.get('key', '').strip()
    field = body.get('field', '').strip()
    value = body.get('value', '').strip()
    if not key or not field:
        return jsonify({'error': 'key and field required'}), 400
    data = load_status()
    if key not in data:
        data[key] = dict(STATUS_DEFAULTS)
    data[key][field] = value
    save_status(data)
    return jsonify({'ok': True})

# ── Dialogs via pywebview ─────────────────────────────────────────────────────
@app.route('/api/browse_folder', methods=['GET'])
def api_browse_folder():
    if _window is None:
        return jsonify({'path': ''})
    result = _window.create_file_dialog(webview.FOLDER_DIALOG)
    return jsonify({'path': result[0] if result else ''})

@app.route('/api/browse_file', methods=['GET'])
def api_browse_file():
    if _window is None:
        return jsonify({'path': ''})
    if sys.platform == 'win32':
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('Executable (*.exe)', 'All files (*.*)')
        )
    else:
        result = _window.create_file_dialog(webview.OPEN_DIALOG)
    return jsonify({'path': result[0] if result else ''})

@app.route('/api/generate_structure', methods=['POST'])
def api_generate_structure():
    data       = request.json
    base_path  = data.get('path', '').strip()
    project    = data.get('project', '').strip()
    sequences  = data.get('sequences', [])

    if not base_path or not project:
        return jsonify({'error': 'Path and project code required'}), 400

    root = os.path.join(base_path, project.lower())

    try:
        # shots/
        # shots/common/ at root level
        for sub in ['plates', 'comp/scripts', 'cg']:
            os.makedirs(os.path.join(root, 'shots', 'common', *sub.split('/')), exist_ok=True)

        for seq in sequences:
            seq_name  = seq.get('seqName', '').strip().lower()
            shot_name = seq.get('shotName', '').strip().lower()
            count     = int(seq.get('count', 1))
            seq_idx   = seq.get('seqIdx', 1)
            seq_num   = f"{seq_idx*10:03d}"
            seq_folder = f"{seq_name}{seq_num}" if seq_name else seq_num

            # common folder per sequence
            os.makedirs(os.path.join(root, 'shots', seq_folder, 'common', 'plates'), exist_ok=True)

            for i in range(1, count + 1):
                shot_num   = f"{i*10:03d}"
                shot_folder = f"{shot_name}{shot_num}" if shot_name else shot_num
                shot_path = os.path.join(root, 'shots', seq_folder, shot_folder)
                for sub in ['work/cg', 'work/comp',
                            'publish/cg/main', 'publish/cg/fog',
                            'publish/comp', 'publish/cam', 'publish/plates']:
                    os.makedirs(os.path.join(shot_path, *sub.split('/')), exist_ok=True)

        # assets/
        for sub in ['chr', 'env', 'models/textures', 'textures']:
            os.makedirs(os.path.join(root, 'assets', *sub.split('/')), exist_ok=True)

        # edit/
        for sub in ['audio', 'picture']:
            os.makedirs(os.path.join(root, 'edit', sub), exist_ok=True)

        return jsonify({'ok': True, 'root': root})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/open_folder', methods=['POST'])
def api_open_folder():
    path = request.json.get('path', '')
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    try:
        if sys.platform == 'win32':
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Main ──────────────────────────────────────────────────────────────────────
def find_free_port():
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def start_flask(port):
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)  # silence Flask request logs
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    port = find_free_port()

    flask_thread = threading.Thread(target=start_flask, args=(port,), daemon=True)
    flask_thread.start()
    time.sleep(0.4)  # let Flask bind before the window loads

    # Create the native desktop window — no browser involved
    _window = webview.create_window(
        title='ShotVault',
        url=f'http://127.0.0.1:{port}',
        width=1280,
        height=800,
        min_size=(800, 500),
    )

    # Blocks here until the window is closed — then the daemon Flask thread dies too
    webview.start(debug=False)
    sys.exit(0)
