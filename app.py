from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import uuid
from datetime import datetime
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'zoombot-secret-2024'
CORS(app, origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

sessions = {}
colab_workers = {}

INDIAN_NAMES = [
    "Aarav Sharma", "Vivaan Patel", "Aditya Singh", "Vihaan Gupta",
    "Arjun Mehta", "Sai Kumar", "Reyansh Joshi", "Ayaan Khan",
    "Krishna Verma", "Ishaan Rao", "Ananya Iyer", "Diya Nair",
    "Priya Reddy", "Kavya Pillai", "Meera Mishra", "Riya Bose",
    "Pooja Yadav", "Sneha Jain", "Tanya Dubey", "Neha Chauhan"
]

ENGLISH_NAMES = [
    "James Wilson", "Emma Johnson", "Liam Brown", "Olivia Davis",
    "Noah Martinez", "Ava Anderson", "William Taylor", "Sophia Thomas",
    "Benjamin Moore", "Isabella Jackson", "Lucas White", "Mia Harris",
    "Henry Martin", "Charlotte Garcia", "Alexander Lee", "Amelia Walker",
    "Mason Hall", "Harper Allen", "Ethan Young", "Evelyn King"
]

def get_names(mode, count, custom_names=None):
    if mode == 'indian':
        pool = INDIAN_NAMES
    elif mode == 'english':
        pool = ENGLISH_NAMES
    elif mode == 'custom' and custom_names:
        pool = custom_names
    else:
        pool = INDIAN_NAMES
    names = []
    for i in range(count):
        name = pool[i % len(pool)]
        if i >= len(pool):
            name = f"{name} {i // len(pool) + 1}"
        names.append(name)
    return names

def log_to_clients(message, level="INFO", session_id=None):
    timestamp = datetime.now().strftime("%H:%M:%S")
    data = {"timestamp": timestamp, "level": level, "message": message, "session_id": session_id}
    socketio.emit('log', data)
    print(f"[{timestamp}] [{level}] {message}")

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "sessions": len(sessions), "workers": len(colab_workers)})

@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    return jsonify({"sessions": list(sessions.values())})

@app.route('/api/workers', methods=['GET'])
def get_workers():
    return jsonify({"workers": list(colab_workers.values())})

@app.route('/api/terminate/<session_id>', methods=['POST'])
def terminate_session(session_id):
    if session_id == 'all':
        for sid in list(sessions.keys()):
            _terminate_session(sid)
        return jsonify({"message": "All sessions terminated"})
    elif session_id in sessions:
        _terminate_session(session_id)
        return jsonify({"message": f"Session {session_id} terminated"})
    else:
        return jsonify({"error": "Session not found"}), 404

def _terminate_session(session_id):
    if session_id not in sessions:
        return
    sessions[session_id]['status'] = 'terminated'
    for worker_id, worker in colab_workers.items():
        if worker.get('assigned_session') == session_id:
            socketio.emit('stop_bots', {"session_id": session_id}, room=worker['sid'])
            worker['status'] = 'idle'
            worker['assigned_session'] = None
    log_to_clients(f"Session {session_id} terminated", "WARN", session_id)

@socketio.on('connect')
def on_connect():
    log_to_clients(f"Client connected: {request.sid}", "INFO")

@socketio.on('disconnect')
def on_disconnect():
    for worker_id, worker in list(colab_workers.items()):
        if worker['sid'] == request.sid:
            log_to_clients(f"Colab Worker '{worker_id}' disconnected!", "WARN")
            del colab_workers[worker_id]
            socketio.emit('workers_update', {"workers": list(colab_workers.values())})
            return
    log_to_clients(f"Client disconnected: {request.sid}", "INFO")

@socketio.on('command')
def handle_command(data):
    meeting_code = data.get('meetingCode', '').strip()
    passcode     = data.get('passcode', '').strip()
    users        = int(data.get('users', 5))
    duration     = int(data.get('duration', 90))
    name_mode    = data.get('nameMode', 'indian')
    custom_names = data.get('customNames')
    headless     = data.get('headless', 'true') == 'true'

    if not meeting_code:
        emit('log', {"level": "ERROR", "message": "Meeting code is required!", "timestamp": datetime.now().strftime("%H:%M:%S")})
        return

    session_id = str(uuid.uuid4())[:8]
    names = get_names(name_mode, users, custom_names)

    sessions[session_id] = {
        "session_id": session_id,
        "meetingCode": meeting_code,
        "passcode": passcode,
        "users": users,
        "duration": duration,
        "nameMode": name_mode,
        "headless": headless,
        "status": "starting",
        "bots": [{"name": n, "status": "pending"} for n in names],
        "startTime": datetime.now().isoformat()
    }

    log_to_clients(f"New session {session_id} created - {users} bots for meeting {meeting_code}", "INFO", session_id)
    socketio.emit('session_created', {"session": sessions[session_id]})
    socketio.emit('instances_update', {"sessions": list(sessions.values())})
    _dispatch_to_workers(session_id, names, meeting_code, passcode, duration, headless)

def _dispatch_to_workers(session_id, names, meeting_code, passcode, duration, headless):
    available = [w for w in colab_workers.values() if w['status'] == 'idle']
    if not available:
        log_to_clients("No Colab workers available! Please connect a worker from Colab.", "ERROR", session_id)
        sessions[session_id]['status'] = 'no_workers'
        socketio.emit('instances_update', {"sessions": list(sessions.values())})
        return
    chunk_size = max(1, len(names) // len(available))
    chunks = [names[i:i+chunk_size] for i in range(0, len(names), chunk_size)]
    for i, worker in enumerate(available):
        if i >= len(chunks):
            break
        chunk = chunks[i]
        worker['status'] = 'busy'
        worker['assigned_session'] = session_id
        payload = {
            "session_id": session_id,
            "meetingCode": meeting_code,
            "passcode": passcode,
            "names": chunk,
            "duration": duration,
            "headless": headless
        }
        socketio.emit('run_bots', payload, room=worker['sid'])
        log_to_clients(f"Dispatched {len(chunk)} bots to worker '{worker['worker_id']}'", "INFO", session_id)
    sessions[session_id]['status'] = 'running'
    socketio.emit('instances_update', {"sessions": list(sessions.values())})

@socketio.on('terminate_session')
def handle_terminate(data):
    session_id = data.get('session_id', 'all')
    if session_id == 'all':
        for sid in list(sessions.keys()):
            _terminate_session(sid)
        log_to_clients("All sessions terminated by user", "WARN")
    else:
        _terminate_session(session_id)
    socketio.emit('instances_update', {"sessions": list(sessions.values())})

@socketio.on('get_instances')
def handle_get_instances():
    emit('instances_update', {"sessions": list(sessions.values())})

@socketio.on('worker_register')
def handle_worker_register(data):
    worker_id = data.get('worker_id', f"worker-{request.sid[:6]}")
    max_bots  = data.get('max_bots', 10)
    colab_workers[worker_id] = {
        "worker_id": worker_id,
        "sid": request.sid,
        "status": "idle",
        "max_bots": max_bots,
        "assigned_session": None,
        "registered_at": datetime.now().isoformat()
    }
    log_to_clients(f"Colab Worker '{worker_id}' registered (max {max_bots} bots)", "SUCCESS")
    socketio.emit('workers_update', {"workers": list(colab_workers.values())})
    emit('worker_registered', {"message": "Registered successfully", "worker_id": worker_id})

@socketio.on('bot_status_update')
def handle_bot_status(data):
    session_id = data.get('session_id')
    name       = data.get('name')
    status     = data.get('status')
    message    = data.get('message', '')
    if session_id in sessions:
        for bot in sessions[session_id]['bots']:
            if bot['name'] == name:
                bot['status'] = status
                break
    level = "SUCCESS" if status == "joined" else ("ERROR" if status == "failed" else "INFO")
    log_to_clients(f"Bot '{name}' -> {status} {message}", level, session_id)
    socketio.emit('bot_update', data)
    socketio.emit('instances_update', {"sessions": list(sessions.values())})

@socketio.on('worker_done')
def handle_worker_done(data):
    worker_id  = data.get('worker_id')
    session_id = data.get('session_id')
    if worker_id in colab_workers:
        colab_workers[worker_id]['status'] = 'idle'
        colab_workers[worker_id]['assigned_session'] = None
    if session_id in sessions:
        all_done = all(b['status'] in ['joined', 'failed', 'left'] for b in sessions[session_id]['bots'])
        if all_done:
            sessions[session_id]['status'] = 'completed'
    log_to_clients(f"Worker '{worker_id}' finished session {session_id}", "SUCCESS", session_id)
    socketio.emit('instances_update', {"sessions": list(sessions.values())})
    socketio.emit('workers_update', {"workers": list(colab_workers.values())})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"Zoom Bot Backend starting on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)