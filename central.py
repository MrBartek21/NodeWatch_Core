import threading, time
from flask import Flask, jsonify, request, send_from_directory, g
from flask_socketio import SocketIO, emit
import pymysql
import tm1637
import requests
#from config import CLK, DIO, DB_HOST, DB_USER, DB_PASS, DB_NAME, API_KEY, DISPLAY_INTERVAL
from config import *
from gpiozero import LED
import psutil
import platform
import socket
from datetime import datetime

# === TM1637 setup ===
tm = tm1637.TM1637(clk=CLK, dio=DIO)

# === GPIO setup ===
led_ok = LED(27)
led_error = LED(17)
led = LED(6)

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




# === Funkcja do alertów ===
def emit_alert(node_id, message, level='info'):
    """Dodaj alert do bazy i emituj przez Socket.IO"""
    db = get_db()
    cursor = db.cursor(pymysql.cursors.DictCursor)
    try:
        # Wstaw alert
        cursor.execute("""
            INSERT INTO alerts (node_id, message, level, created_at)
            VALUES (%s, %s, %s, NOW())
        """, (node_id, message, level))
        db.commit()

        # Pobierz dokładny timestamp z bazy
        cursor.execute("SELECT created_at FROM alerts WHERE id = LAST_INSERT_ID()")
        created_at = cursor.fetchone()['created_at'].strftime("%Y-%m-%d %H:%M:%S")

        # Pobierz hostname dla node_id
        cursor.execute("SELECT hostname FROM nodes WHERE id=%s", (node_id,))
        hostname_row = cursor.fetchone()
        hostname = hostname_row['hostname'] if hostname_row else 'unknown'

        # Emituj event do wszystkich klientów
        socketio.emit('new_alert', {
            'node_id': node_id,
            'hostname': hostname,
            'message': message,
            'level': level,
            'created_at': created_at
        })
    finally:
        cursor.close()




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
    agent_version = data.get("AGENT_VERSION")
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
                    uptime=%s, ip=%s, docker_version=%s, cpu_temp=%s, agent_hostname=%s, host_type=%s, agent_version=%s
                WHERE id=%s
            """, (node_type, cpu_percent, memory_percent, disk_percent,
                  uptime, ip, docker_version, cpu_temp, agent_hostname, host_type, agent_version, node_id))
        else:
            cursor.execute("""
                INSERT INTO nodes 
                    (hostname, type, status, last_seen,
                     cpu_percent, memory_percent, disk_percent,
                     uptime, ip, docker_version, cpu_temp, agent_hostname, host_type, agent_version)
                VALUES (%s,%s,'online',NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (hostname, node_type, cpu_percent, memory_percent, disk_percent,
                  uptime, ip, docker_version, cpu_temp, agent_hostname, host_type, agent_version))
            node_id = cursor.lastrowid

            # --- Alerty hosta ---
        if cpu_percent and cpu_percent > 80:
            emit_alert(node_id, f"CPU > 80% ({cpu_percent}%)", 'danger')
        if memory_percent and memory_percent > 85:
            emit_alert(node_id, f"RAM > 85% ({memory_percent}%)", 'danger')
        if disk_percent and disk_percent > 70:
            emit_alert(node_id, f"Dysk > 70% ({disk_percent}%)", 'warning')


        # Usuń stare kontenery i wstaw aktualne
        cursor.execute("DELETE FROM services WHERE node_id=%s", (node_id,))
        for c in containers:
            # --- Alert kontenera tylko jeśli nie działa
            if c.get("status") == "stopped":
                emit_alert(node_id, f"Kontener {c.get('name')} zatrzymany (status: {c.get('status')})", 'danger')

            if c.get("status") == "restarting":
                emit_alert(node_id, f"Kontener {c.get('name')} restartuje się (status: {c.get('status')})", 'warning')

            if c.get("status") == "created":
                emit_alert(node_id, f"Kontener {c.get('name')} nie uruchomiony (status: {c.get('status')})", 'warning')


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
                "agent_version": node['agent_version'],
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


@app.route("/api/container_action", methods=["POST"])
def container_action_central():
    API_KEY = "TwojSekretnyKlucz"  # używane do komunikacji z agentem
    data = request.json
    hostname = data.get("hostname")        # tu podajesz IP hosta
    container_name = data.get("container_name")
    action = data.get("action")

    if not hostname or not container_name or not action:
        return jsonify({"error": "hostname, container_name i action są wymagane"}), 400

    agent_url = f"http://{hostname}:5000/api/container_action"
    try:
        res = requests.post(agent_url, json={
            "container_name": container_name,
            "action": action
        }, headers={"X-API-KEY": API_KEY}, timeout=5)

        if res.status_code == 200:
            return jsonify(res.json())
        else:
            return jsonify({"error": f"Agent zwrócił błąd: {res.text}"}), res.status_code
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Nie udało się połączyć z agentem: {str(e)}"}), 500



# === Funkcja zbierająca live dane systemowe ===
def gather_host_data():
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        memory_percent = memory.percent
        disk = psutil.disk_usage('/')
        disk_percent = disk.percent
        uptime = int(time.time() - psutil.boot_time())
        cpu_temp = None
        try:
            # odczyt temperatury w mC i konwersja na C
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                cpu_temp = int(f.read().strip()) / 1000.0
        except Exception as e:
            print("Nie udało się odczytać temperatury CPU:", e)
            cpu_temp = None
        
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except:
            ip = "n/a"
        finally:
            s.close()

        data = {
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "disk_percent": disk_percent,
            "ip": ip,
            "uptime": uptime,
            "cpu_temp": cpu_temp,
            "hostname": platform.node(),
            "os": platform.platform()
        }
        return data
    except Exception as e:
        print("Host data error:", e)
        return {}

# === Wątek emitujący dane live przez Socket.IO ===
def live_host_thread():
    while True:
        data = gather_host_data()
        socketio.emit('live_host_data', data, namespace='/')  # usuń broadcast
        socketio.sleep(2)  # zamiast time.sleep



# === TM1637 display thread ===
def display_loop():
    while True:
        try:
            with app.app_context():
                db = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, autocommit=True)
                with db.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM nodes WHERE status='online'")
                    online_nodes = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM alerts")
                    alerts_count = cursor.fetchone()[0]
                db.close()
                
                tm.numbers(online_nodes % 100, alerts_count % 100)
                time.sleep(DISPLAY_INTERVAL)

        except Exception as e:
            print("Display error:", e)
            time.sleep(DISPLAY_INTERVAL)


def check_hardware():
    while True:
        try:
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent

            # --- Diagnostyka hardware ---
            if cpu > 90 or ram > 85 or disk > 80:
                # Szybkie miganie – krytyczne
                led.blink(on_time=0.2, off_time=0.2)
            elif cpu > 80 or ram > 75 or disk > 70:
                # Wolne miganie – ostrzeżenie
                led.blink(on_time=0.6, off_time=0.6)
            else:
                # Stałe światło – wszystko OK
                led.on()
        except Exception as e:
            print("LED hardware check error:", e)
            led.off()
        time.sleep(5)


def offline_checker():
    while True:
        try:
            with app.app_context():
                db = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME, autocommit=True)
                with db.cursor() as cursor:
                    # Pobierz wszystkie węzły
                    cursor.execute("SELECT id, hostname, last_seen, status FROM nodes")
                    nodes = cursor.fetchall()

                    for node_id, hostname, last_seen, status in nodes:
                        if last_seen is None:
                            continue
                        elapsed = (datetime.now() - last_seen).total_seconds()
                        
                        if elapsed > NODE_OFFLINE_TIMEOUT and status != 'offline':
                            cursor.execute("UPDATE nodes SET status='offline' WHERE id=%s", (node_id,))
                            emit_alert(node_id, f"Host {hostname} jest OFFLINE", level='danger')
                        
                        elif elapsed <= NODE_OFFLINE_TIMEOUT and status != 'online':
                            cursor.execute("UPDATE nodes SET status='online' WHERE id=%s", (node_id,))
                            emit_alert(node_id, f"Host {hostname} wrócił ONLINE", level='success')

                db.close()
        except Exception as e:
            print("Offline checker error:", e)

        socketio.sleep(CHECK_INTERVAL)  # <--- zamiast time.sleep()

# === Dashboard ===
@app.route("/dashboard.html")
def dashboard():
    return send_from_directory(".", "dashboard.html")

if __name__ == "__main__":
    threading.Thread(target=display_loop, daemon=True).start()
    threading.Thread(target=check_hardware, daemon=True).start()
    socketio.start_background_task(offline_checker)
    socketio.start_background_task(live_host_thread)
    socketio.run(app, host="0.0.0.0", port=8080)
