import threading, time
from flask import Flask, jsonify, request, send_from_directory, g
from flask_socketio import SocketIO, emit
import pymysql
import tm1637
import requests
from config import CLK, DIO, DB_HOST, DB_USER, DB_PASS, DB_NAME, API_KEY, DISPLAY_INTERVAL

# === TM1637 setup ===
tm = tm1637.TM1637(clk=CLK, dio=DIO)

# === Flask + SocketIO setup ===
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# === DB connection per request ===
def get_db():
    if 'db' not in g:
        g.db = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, autocommit=True)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()



from gpiozero import LED
# --- GPIO setup ---
led_ok = LED(27)
led_error = LED(17)

# === API do aktualizacji stanu węzłów ===
@app.route("/api/update", methods=["POST"])
def update_node():
    led_ok.on()  # sygnalizacja rozpoczęcia
    key = request.headers.get("X-API-KEY")
    if key != API_KEY:
        led_error.on()
        return "Unauthorized", 401

    data = request.json
    hostname = data.get("hostname")
    agent_hostname = data.get("agent_hostname")
    host_type = data.get("host_type")
    node_type = data.get("type")
    containers = data.get("containers", [])
    host_status = data.get("host_status", {}) or {}

    cpu_percent = host_status.get("cpu_percent")
    memory_percent = host_status.get("memory_percent")
    disk_percent = host_status.get("disk_percent")
    uptime = host_status.get("uptime")
    ip = host_status.get("ip")
    docker_version = host_status.get("docker_version")
    cpu_temp = host_status.get("cpu_temp")

    db = get_db()
    with db.cursor() as cursor:
        # Sprawdź, czy węzeł istnieje
        cursor.execute("SELECT id FROM nodes WHERE hostname=%s", (hostname,))
        result = cursor.fetchone()

        if result:
            node_id = result[0]
            cursor.execute("""
                UPDATE nodes SET 
                    type=%s, status='online', last_seen=NOW(),
                    cpu_percent=%s, memory_percent=%s, disk_percent=%s,
                    uptime=%s, ip=%s, docker_version=%s, cpu_temp=%s, agent_hostname=%s, host_type=%s
                WHERE id=%s
            """, (node_type, cpu_percent, memory_percent, disk_percent,
                  uptime, ip, docker_version, cpu_temp, agent_hostname, host_type, node_id))
        else:
            cursor.execute("""
                INSERT INTO nodes 
                    (hostname, type, status, last_seen,
                     cpu_percent, memory_percent, disk_percent,
                     uptime, ip, docker_version, cpu_temp, agent_hostname, host_type)
                VALUES (%s,%s,'online',NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (hostname, node_type, cpu_percent, memory_percent, disk_percent,
                  uptime, ip, docker_version, cpu_temp, agent_hostname, host_type))
            node_id = cursor.lastrowid

            # --- Alerty hosta ---
        if cpu_percent and cpu_percent > 80:
            emit_alert(node_id, f"CPU > 80% ({cpu_percent}%)")
        if memory_percent and memory_percent > 85:
            emit_alert(node_id, f"RAM > 85% ({memory_percent}%)")
        if disk_percent and disk_percent > 70:
            emit_alert(node_id, f"Dysk > 70% ({disk_percent}%)")


        # Usuń stare kontenery i wstaw aktualne
        cursor.execute("DELETE FROM services WHERE node_id=%s", (node_id,))
        for c in containers:
            # --- Alert kontenera tylko jeśli nie działa
            #if c.get("status") != "running":
            #    emit_alert(node_id, f"Kontener {c.get('name')} zatrzymany (status: {c.get('status')})")


            ports = ",".join(c.get("ports", []))
            cursor.execute("""
                INSERT INTO services (node_id, name, port, status)
                VALUES (%s, %s, %s, %s)
            """, (node_id, c.get("name"), ports, c.get("status")))

    db.commit()
    socketio.emit("update_nodes")
    led_ok.off()
    led_error.off()
    return "OK", 200


# === API do usuwania serwera ===
@app.route("/api/delete_node", methods=["POST"])
def delete_node():
    """Usuwa serwer i wszystkie jego kontenery z bazy danych"""
    key = request.headers.get("X-API-KEY")
    if key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    hostname = data.get("hostname")
    if not hostname:
        return jsonify({"error": "Missing hostname"}), 400

    db = get_db()
    with db.cursor() as cursor:
        cursor.execute("SELECT id FROM nodes WHERE hostname=%s", (hostname,))
        result = cursor.fetchone()
        if not result:
            return jsonify({"error": f"Node '{hostname}' not found"}), 404

        node_id = result[0]
        # Usuń powiązane usługi
        cursor.execute("DELETE FROM services WHERE node_id=%s", (node_id,))
        # Usuń sam węzeł
        cursor.execute("DELETE FROM nodes WHERE id=%s", (node_id,))

    db.commit()
    socketio.emit("update_nodes")
    return jsonify({"message": f"Node '{hostname}' deleted successfully"}), 200


def emit_alert(node_id, message, level='info'):
    """Dodaj alert do bazy i emituj przez Socket.IO"""
    db = get_db()
    with db.cursor() as cursor:
        cursor.execute("INSERT INTO alerts (node_id, message, level, created_at) VALUES (%s,%s,%s,NOW())",
                       (node_id, message, level))
    db.commit()
    socketio.emit('new_alert', {'node_id': node_id, 'message': message, 'level': level})

# === API do czyszczenia alertów ===
@app.route("/api/clear_alerts", methods=["POST"])
def clear_alerts():
    key = request.headers.get("X-API-KEY")
    if key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    hostname = data.get("hostname")  # jeśli podamy hostname, czyścimy tylko dla tego węzła

    db = get_db()
    with db.cursor() as cursor:
        if hostname:
            cursor.execute("SELECT id FROM nodes WHERE hostname=%s", (hostname,))
            result = cursor.fetchone()
            if not result:
                return jsonify({"error": f"Node '{hostname}' not found"}), 404
            node_id = result[0]
            cursor.execute("DELETE FROM alerts WHERE node_id=%s", (node_id,))
        else:
            cursor.execute("DELETE FROM alerts")  # globalnie

    db.commit()
    socketio.emit("alerts_cleared", {"hostname": hostname})  # powiadom front
    return jsonify({"message": f"Alerty {'dla ' + hostname if hostname else 'wszystkie'} zostały wyczyszczone"}), 200




@socketio.on('get_fullstatus')
def handle_get_fullstatus():
    db = get_db()
    nodes = {}

    with db.cursor(pymysql.cursors.DictCursor) as cursor:
        # Pobierz wszystkie węzły
        cursor.execute("SELECT * FROM nodes ORDER BY agent_hostname")
        for node in cursor.fetchall():
            nodes[node['id']] = {
                "hostname": node['hostname'],
                "agent_hostname": node['agent_hostname'],
                "host_type": node['host_type'],
                "type": node['type'],
                "status": node.get('status', 'unknown'),
                "last_seen": node['last_seen'].strftime("%Y-%m-%d %H:%M:%S") if node['last_seen'] else None,
                "host_info": {
                    "cpu_percent": node.get('cpu_percent'),
                    "memory_percent": node.get('memory_percent'),
                    "disk_percent": node.get('disk_percent'),
                    "uptime": node.get('uptime'),
                    "ip": node.get('ip'),
                    "docker_version": node.get('docker_version'),
                    "cpu_temp": node.get('cpu_temp')
                },
                "services": [],
                "alerts": []
            }

        # Pobierz wszystkie serwisy / kontenery
        cursor.execute("SELECT * FROM services ORDER BY node_id, name")
        for svc in cursor.fetchall():
            if svc['node_id'] in nodes:
                nodes[svc['node_id']]['services'].append({
                    "id": svc['id'],
                    "name": svc['name'],
                    "status": svc['status'],
                    "port": svc.get('port'),
                    "health": svc.get('health'),
                    "created": svc.get('created'),
                    "ip_addresses": json.loads(svc.get('ip_addresses')) if svc.get('ip_addresses') else [],
                    "volumes": json.loads(svc.get('volumes')) if svc.get('volumes') else []
                })

        # Pobierz alerty powiązane z węzłami
        cursor.execute("SELECT * FROM alerts ORDER BY node_id, created_at DESC")
        for alert in cursor.fetchall():
            nid = alert['node_id']
            if nid in nodes:
                nodes[nid]['alerts'].append({
                    "id": alert['id'],
                    "message": alert['message'],
                    "level": alert.get('level', 'info'),
                    "created_at": alert['created_at'].strftime("%Y-%m-%d %H:%M:%S") if alert['created_at'] else None
                })

    # Wyślij całość do frontendu przez Socket.IO
    emit('fullstatus', list(nodes.values()))



# === Restart kontenera przez SocketIO POST ===
@app.route("/api/restart_container", methods=["POST"])
def restart_container():
    data = request.json
    hostname = data.get("hostname")
    container_name = data.get("container_name")

    if not hostname or not container_name:
        return jsonify({"error": "hostname i container_name wymagane"}), 400

    db = get_db()
    with db.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute("SELECT hostname FROM nodes WHERE hostname=%s", (hostname,))
        node = cursor.fetchone()
        if not node:
            return jsonify({"error": "Node nie znaleziony"}), 404

    try:
        agent_url = f"http://{hostname}:5000/api/restart"
        res = requests.post(agent_url, json={"container_name": container_name},
                            headers={"X-API-KEY": API_KEY}, timeout=5)
        if res.status_code == 200:
            return jsonify({"status":"ok","message":f"Kontener {container_name} restartowany"})
        else:
            return jsonify({"status":"fail","message":res.text}), 500
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

# === TM1637 display thread ===
def display_loop():
    while True:
        try:
            with app.app_context():  # <-- tutaj kontekst aplikacji
                db = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                                     database=DB_NAME, autocommit=True)
                with db.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM nodes WHERE status='online'")
                    online_nodes = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM alerts")
                    alerts_count = cursor.fetchone()[0]
                db.close()
                tm.numbers(online_nodes, alerts_count)
        except Exception as e:
            print("Display error:", e)
        time.sleep(DISPLAY_INTERVAL)


# === Dashboard ===
@app.route("/dashboard.html")
def dashboard():
    return send_from_directory(".", "dashboard.html")

if __name__ == "__main__":
    threading.Thread(target=display_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=8080)
