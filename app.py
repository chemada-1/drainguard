import socket
import threading
import sqlite3
import json
import requests
import time
from datetime import datetime
from io import BytesIO
from fpdf import FPDF
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, send_file

app = Flask(__name__)
app.secret_key = 'capstone_secure_key_123'

# --- CONFIGURATION ---
UDP_IP = "0.0.0.0"
UDP_PORT = 8080
DB_NAME = "drainguard.db"
SEMAPHORE_API_KEY = 'YOUR_API_KEY_HERE' 
TARGET_PHONE_NUMBER = '09123456789'

# --- GLOBALS ---
latest_telemetry = {}
sms_sent = {}
last_known_status = {}
last_receiver_heartbeat = time.time()
receiver_ip = None  

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  date_time TEXT, 
                  node_id TEXT,
                  distance REAL,
                  battery INTEGER,
                  rssi INTEGER,
                  status TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS registered_nodes
                 (node_id TEXT PRIMARY KEY,
                  node_name TEXT)''')
    
    c.execute("SELECT COUNT(*) FROM registered_nodes")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO registered_nodes (node_id, node_name) VALUES ('1', 'Main Canal')")
        
    conn.commit()
    conn.close()

def log_event_to_db(node_id, distance, battery, rssi, status):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("INSERT INTO history_logs (date_time, node_id, distance, battery, rssi, status) VALUES (?, ?, ?, ?, ?, ?)", 
              (current_time, node_id, distance, battery, rssi, status))
    conn.commit()
    conn.close()

def send_semaphore_sms(message):
    if SEMAPHORE_API_KEY == 'YOUR_API_KEY_HERE':
        return
    payload = {'apikey': SEMAPHORE_API_KEY, 'number': TARGET_PHONE_NUMBER, 'message': message}
    try:
        requests.post('https://api.semaphore.co/api/v4/messages', data=payload)
    except Exception as e:
        print(f"[!] SMS Failed: {e}")

# --- UDP LISTENER ---
def udp_listener():
    global sms_sent, last_known_status, latest_telemetry, last_receiver_heartbeat, receiver_ip
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    print(f"[*] LoRa UDP Gateway active on port {UDP_PORT}")
    
    while True:
        data, addr = sock.recvfrom(1024)
        try:
            payload = json.loads(data.decode('utf-8'))
            raw_data = payload.get('data', '') 
            rssi = payload.get('rssi', 0)
            
            if "SYS:HEARTBEAT" in raw_data:
                last_receiver_heartbeat = time.time()
                receiver_ip = addr[0] 
                continue
            
            if "DIST:" in raw_data:
                node_id = "1"
                dist_val = 0.0
                batt_val = 0
                
                parts = raw_data.split()
                for part in parts:
                    if part.startswith("NODE:"): node_id = part.split(":")[1]
                    elif part.startswith("DIST:"): dist_val = float(part.split(":")[1])
                    elif part.startswith("BATT:"): batt_val = int(part.split(":")[1])
                
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("SELECT node_name FROM registered_nodes WHERE node_id=?", (node_id,))
                registered_node = c.fetchone()
                conn.close()
                
                if not registered_node:
                    continue 

                if dist_val < 30:
                    status = "CRITICAL FLOOD"
                elif dist_val > 140:
                    status = "DRY"
                else:
                    status = "NORMAL"

                latest_telemetry[node_id] = {
                    "distance": dist_val,
                    "battery": batt_val,
                    "rssi": rssi,
                    "status": status,
                    "timestamp": datetime.now().strftime('%H:%M:%S')
                }

                if node_id not in last_known_status:
                    last_known_status[node_id] = "NORMAL"
                    sms_sent[node_id] = False

                if status != last_known_status[node_id]:
                    log_event_to_db(node_id, dist_val, batt_val, rssi, status)
                    last_known_status[node_id] = status

                if status == "CRITICAL FLOOD" and not sms_sent[node_id]:
                    send_semaphore_sms(f"DRAINGUARD ALERT (Node {node_id}): Threshold crossed! Current clearance: {dist_val}cm. Status: {status}.")
                    sms_sent[node_id] = True
                elif status == "NORMAL" and sms_sent[node_id]:
                    sms_sent[node_id] = False
                    
        except Exception as e:
            print(f"[!] Payload parse error: {e}")

# --- WEB ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['username'] == 'kennydumo243@gmail.com' and request.form['password'] == 'admin123':
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            error = 'Invalid Credentials.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/history')
def history():
    if not session.get('logged_in'): return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM history_logs ORDER BY id DESC LIMIT 50")
    logs = c.fetchall()
    conn.close()
    return render_template('history.html', logs=logs)

@app.route('/nodes', methods=['GET', 'POST'])
def manage_nodes():
    if not session.get('logged_in'): return redirect(url_for('login'))
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    if request.method == 'POST':
        action = request.form.get('action')
        n_id = request.form.get('node_id')
        
        if action == 'add':
            n_name = request.form.get('node_name')
            c.execute("INSERT OR REPLACE INTO registered_nodes (node_id, node_name) VALUES (?, ?)", (n_id, n_name))
        elif action == 'delete':
            c.execute("DELETE FROM registered_nodes WHERE node_id=?", (n_id,))
            if n_id in latest_telemetry:
                del latest_telemetry[n_id]
        conn.commit()
        
    c.execute("SELECT node_id, node_name FROM registered_nodes ORDER BY node_id ASC")
    nodes = c.fetchall()
    conn.close()
    return render_template('register.html', nodes=nodes)

@app.route('/receiver')
def manage_receiver():
    if not session.get('logged_in'): return redirect(url_for('login'))
    is_online = (time.time() - last_receiver_heartbeat) <= 15
    status = "ONLINE" if is_online else "OFFLINE"
    return render_template('receiver.html', receiver_ip=receiver_ip, status=status)

@app.route('/export_pdf')
def export_pdf():
    if not session.get('logged_in'): 
        return redirect(url_for('login'))
    
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT date_time, node_id, distance, battery, rssi, status FROM history_logs ORDER BY id DESC")
    logs = c.fetchall()
    conn.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "DrainGuard System Event History", ln=True, align='C')
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 10, "Generated on: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ln=True, align='C')
    pdf.ln(10)
    
    pdf.set_font("Arial", 'B', 10)
    pdf.set_fill_color(200, 220, 255)
    pdf.cell(45, 10, "Date & Time", border=1, fill=True)
    pdf.cell(20, 10, "Node", border=1, fill=True)
    pdf.cell(30, 10, "Clearance", border=1, fill=True)
    pdf.cell(20, 10, "Health", border=1, fill=True)
    pdf.cell(20, 10, "RSSI", border=1, fill=True)
    pdf.cell(55, 10, "System Status", border=1, fill=True, ln=True)
    
    pdf.set_font("Arial", size=9)
    for row in logs:
        health_status = "WEAK" if int(row[4]) < -100 else "GOOD"
        
        pdf.cell(45, 10, str(row[0]), border=1)
        pdf.cell(20, 10, f"Node-{row[1]}", border=1)
        pdf.cell(30, 10, f"{row[2]} cm", border=1)
        pdf.cell(20, 10, health_status, border=1)
        pdf.cell(20, 10, f"{row[4]} dBm", border=1)
        pdf.cell(55, 10, str(row[5]), border=1, ln=True)
        
    pdf_buffer = BytesIO()
    pdf.output(pdf_buffer)
    pdf_buffer.seek(0)
    
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"drainguard_history_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype="application/pdf"
    )

@app.route('/api/nodes')
def api_get_nodes():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT node_id, node_name FROM registered_nodes ORDER BY node_id ASC")
    nodes = [{"id": r[0], "name": r[1]} for r in c.fetchall()]
    conn.close()
    return jsonify(nodes)

@app.route('/api/receiver/config', methods=['POST'])
def api_receiver_config():
    if not session.get('logged_in'): 
        return jsonify({"error": "Unauthorized"}), 401
        
    if not receiver_ip or (time.time() - last_receiver_heartbeat > 15):
        return jsonify({"error": "Receiver offline or IP unknown"}), 503
        
    config_data = request.json
    try:
        esp_url = f"http://{receiver_ip}/api/config"
        resp = requests.post(esp_url, json=config_data, timeout=5)
        
        if resp.status_code == 200:
            return jsonify({"success": True, "message": "Configuration sent. ESP32 rebooting."})
        else:
            return jsonify({"error": "ESP32 rejected the configuration."}), 400
            
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history')
def api_history():
    node_id = request.args.get('node', '1')
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT date_time, distance FROM history_logs WHERE node_id=? ORDER BY id DESC LIMIT 50", (node_id,))
    rows = c.fetchall()
    conn.close()
    
    rows.reverse()
    return jsonify({
        "timestamps": [row[0] for row in rows],
        "distances": [row[1] for row in rows]
    })

@app.route('/api/data')
def api_data():
    if time.time() - last_receiver_heartbeat > 15:
        return jsonify({"error": "RECEIVER_DISCONNECTED"}), 503

    node_id = request.args.get('node', '1')
    data = latest_telemetry.get(node_id, {
        "distance": 0.0,
        "battery": 0,
        "rssi": 0,
        "status": "OFFLINE",
        "timestamp": "--:--:--"
    })
    return jsonify(data)

if __name__ == '__main__':
    init_db()
    udp_thread = threading.Thread(target=udp_listener, daemon=True)
    udp_thread.start()
    app.run(host='0.0.0.0', port=5000, debug=False)