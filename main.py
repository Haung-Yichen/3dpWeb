"""
3D Printer Web Control System
提供瀏覽器與 ESP32 之間的 WebSocket 橋接服務
支援檔案上傳、即時狀態監控、視訊串流
"""

import webbrowser
from flask import Flask, request, send_from_directory, Response, jsonify
from flask_cors import CORS
from flask_sock import Sock
import websocket
import threading
import hashlib
import time
import cv2
import re
import json
import os

# =============================================================================
# 全域設定
# =============================================================================


class Config:
    """應用程式配置"""
    CONFIG_FILE = "config.json"
    ESP32_IP = "ws://192.168.1.147:82"  # 預設值
    CAMERA_SOURCE = "esp32"  # 鏡頭來源: "esp32" 或 "server"
    CAMERA_BACKGROUND = True  # 鏡頭背景長駐: True 或 False
    MAX_RECONNECT_ATTEMPTS = 10
    RECONNECT_DELAY = 1  # 秒
    POLL_INTERVAL = 5  # 秒
    UPLOAD_TIMEOUT = 50  # 秒
    CHUNK_SIZE = 2048
    CAMERA_FPS = 30
    AUTO_SHUTDOWN_DELAY = 30  # 無連線後自動關閉延遲（秒），設為 0 則不自動關閉

    @staticmethod
    def load_config():
        """從檔案載入配置"""
        try:
            if os.path.exists(Config.CONFIG_FILE):
                with open(Config.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    Config.ESP32_IP = data.get('esp32_ip', Config.ESP32_IP)
                    Config.CAMERA_SOURCE = data.get(
                        'camera_source', Config.CAMERA_SOURCE)
                    Config.CAMERA_BACKGROUND = data.get(
                        'camera_background', Config.CAMERA_BACKGROUND)
                    print(f"已從配置檔案載入 ESP32 IP: {Config.ESP32_IP}")
                    print(f"已從配置檔案載入鏡頭來源: {Config.CAMERA_SOURCE}")
                    print(f"已從配置檔案載入鏡頭背景長駐: {Config.CAMERA_BACKGROUND}")
            else:
                print("配置檔案不存在,使用預設配置")
        except Exception as e:
            print(f"載入配置檔案失敗: {e}, 使用預設值")

    @staticmethod
    def save_config():
        """儲存配置到檔案"""
        try:
            data = {
                'esp32_ip': Config.ESP32_IP,
                'camera_source': Config.CAMERA_SOURCE,
                'camera_background': Config.CAMERA_BACKGROUND
            }
            with open(Config.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"配置已儲存到 {Config.CONFIG_FILE}")
            return True
        except Exception as e:
            print(f"儲存配置檔案失敗: {e}")
            return False

    @staticmethod
    def get_camera_url():
        """從 WebSocket URL 解析出 HTTP 串流 URL"""
        try:
            # 假設格式為 ws://IP:PORT
            match = re.search(r'ws://([^:]+)', Config.ESP32_IP)
            if match:
                ip = match.group(1)
                # ESP32 HTTP server 在 port 81 (避免與其他服務衝突)
                return f"http://{ip}:81/video_feed"
        except Exception as e:
            print(f"解析相機 URL 失敗: {e}")
        return None


# =============================================================================
# 攝影機管理 (伺服器本地 USB 攝影機)
# =============================================================================

class CameraThread(threading.Thread):
    """線程安全的本地 USB 攝影機管理類別

    此類別專門處理伺服器端的 USB 攝影機，與 ESP32 攝影機完全獨立。
    根據 Config.CAMERA_BACKGROUND 設定決定是否背景常駐。
    """

    def __init__(self):
        super().__init__()
        self.daemon = True
        self.frame_bytes = None
        self.lock = threading.Lock()
        self.camera = None
        self._running = True
        self._enabled = False  # 用於非背景模式時控制攝影機

    def run(self):
        """開啟本地 USB 攝影機並持續讀取畫面"""
        print("伺服器 USB 攝影機線程啟動中...")

        while self._running:
            # 如果不是背景長駐模式，等待開啟指令
            if not Config.CAMERA_BACKGROUND:
                if not self._enabled:
                    time.sleep(0.1)
                    continue

            # 嘗試開啟攝影機
            if self.camera is None or not self.camera.isOpened():
                print("伺服器攝影機：嘗試開啟本地 USB 攝影機...")
                self.camera = cv2.VideoCapture(0)  # 預設使用第一個攝影機

                if not self.camera.isOpened():
                    print("伺服器攝影機：無法開啟本地 USB 攝影機，稍後重試...")
                    time.sleep(2)
                    continue

                mode = "背景常駐" if Config.CAMERA_BACKGROUND else "按需開啟"
                print(f"伺服器攝影機：已成功開啟本地 USB 攝影機（{mode}）")

            frame_delay = 1.0 / Config.CAMERA_FPS

            while self._running and self.camera.isOpened():
                # 如果不是背景模式且被禁用，則關閉攝影機
                if not Config.CAMERA_BACKGROUND and not self._enabled:
                    print("伺服器攝影機：收到關閉指令")
                    break

                success, frame = self.camera.read()
                if not success:
                    print("伺服器攝影機：讀取失敗，準備重新連接...")
                    break  # 跳出內部迴圈以進行重連

                ret, buffer = cv2.imencode('.jpg', frame)
                if not ret:
                    continue

                with self.lock:
                    self.frame_bytes = buffer.tobytes()

                time.sleep(frame_delay)

            # 釋放攝影機資源
            if self.camera:
                self.camera.release()
                self.camera = None

            if self._running and (Config.CAMERA_BACKGROUND or self._enabled):
                print("伺服器攝影機：連線中斷，準備重連...")
                time.sleep(1)

        print("伺服器攝影機線程：已停止")

    def enable(self):
        """開啟攝影機（用於非背景模式）"""
        self._enabled = True
        print("伺服器攝影機：已啟用")

    def disable(self):
        """關閉攝影機（用於非背景模式）"""
        self._enabled = False
        with self.lock:
            self.frame_bytes = None
        print("伺服器攝影機：已禁用")

    def get_frame(self):
        """線程安全地獲取最新的 JPEG 幀"""
        with self.lock:
            return self.frame_bytes

    def is_ready(self):
        """檢查攝影機是否已就緒"""
        with self.lock:
            return self.frame_bytes is not None

    def stop(self):
        """停止攝影機線程"""
        self._running = False
        if self.camera:
            self.camera.release()
            self.camera = None


# =============================================================================
# ESP32 連線管理
# =============================================================================

class ESP32Connection:
    """ESP32 WebSocket 連線管理器"""

    def __init__(self):
        self.ws = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.lock = threading.Lock()
        self.browser_clients = set()
        self.upload_done_event = threading.Event()
        self.is_uploading = False

    def broadcast_to_browsers(self, message):
        """廣播訊息給所有瀏覽器客戶端"""
        dead_clients = set()
        for client in self.browser_clients:
            try:
                client.send(message)
            except Exception as e:
                print(f"廣播失敗: {e}")
                dead_clients.add(client)

        for client in dead_clients:
            self.browser_clients.discard(client)

    def on_message(self, ws, msg):
        """處理 ESP32 回覆的訊息"""
        print(f"ESP32 回覆: {msg}")
        self.broadcast_to_browsers(msg)

        if msg.strip().lower() == "ok":
            self.upload_done_event.set()
        elif msg == "upload success":
            print("ESP32 顯示檔案上傳成功")

    def on_open(self, ws):
        """ESP32 連線成功"""
        print("成功連接到 ESP32")
        with self.lock:
            self.reconnect_attempts = 0
            self.is_connected = True

        self.broadcast_to_browsers("ESP32_CONNECTED")

    def on_close(self, ws, code, msg):
        """ESP32 連線關閉"""
        print("WebSocket 連線關閉 (ESP32)")

        with self.lock:
            self.is_connected = False

        self.broadcast_to_browsers("ESP32_DISCONNECTED")
        threading.Thread(target=self.attempt_reconnect, daemon=True).start()

    def on_error(self, ws, err):
        """WebSocket 錯誤"""
        print(f"WebSocket 錯誤 (ESP32): {err}")

    def connect(self):
        """連接到 ESP32"""
        self.ws = websocket.WebSocketApp(
            Config.ESP32_IP,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open
        )
        self.ws.run_forever()

    def attempt_reconnect(self):
        """嘗試重新連接 ESP32"""
        with self.lock:
            if self.is_connected:
                return
            if self.reconnect_attempts >= Config.MAX_RECONNECT_ATTEMPTS:
                print(f"重連失敗，已嘗試 {Config.MAX_RECONNECT_ATTEMPTS} 次")
                return

        while self.reconnect_attempts < Config.MAX_RECONNECT_ATTEMPTS:
            with self.lock:
                if self.is_connected:
                    return
                self.reconnect_attempts += 1
                print(
                    f"嘗試重連 ESP32... ({self.reconnect_attempts}/{Config.MAX_RECONNECT_ATTEMPTS})")

            time.sleep(Config.RECONNECT_DELAY)

            try:
                threading.Thread(target=self.connect, daemon=True).start()
                break
            except Exception as e:
                print(f"重連失敗: {e}")

    def send(self, data):
        """發送資料到 ESP32"""
        if self.is_connected and self.ws:
            try:
                self.ws.send(data)
                return True
            except Exception as e:
                print(f"發送失敗: {e}")
                return False
        return False

    def poll_status(self):
        """輪詢 ESP32 狀態"""
        print("啟動 ESP32 狀態輪詢線程...")
        while True:
            if self.is_connected and not self.is_uploading:
                try:
                    self.send("cReqNozzleTemp")
                    time.sleep(0.1)
                    self.send("cReqBedTemp")
                    time.sleep(0.1)
                    self.send("cReqFilamentWeight")
                    time.sleep(0.1)
                    self.send("cReqRemainningTime")
                    time.sleep(0.1)
                    self.send("cReqProgress")
                except Exception as e:
                    print(f"輪詢失敗: {e}")

            time.sleep(Config.POLL_INTERVAL)


# =============================================================================
# Flask 應用程式初始化
# =============================================================================

app = Flask(__name__, static_folder="static")
CORS(app)
sock = Sock(app)

# 全域實例
esp32 = ESP32Connection()
camera = CameraThread()

# 自動關閉計時器
shutdown_timer = None
shutdown_lock = threading.Lock()


# =============================================================================
# WebSocket 路由 (瀏覽器端)
# =============================================================================

def cancel_shutdown_timer():
    """取消自動關閉計時器"""
    global shutdown_timer
    with shutdown_lock:
        if shutdown_timer is not None:
            shutdown_timer.cancel()
            shutdown_timer = None
            print("✓ 已取消自動關閉計時器")


def start_shutdown_timer():
    """啟動自動關閉計時器"""
    global shutdown_timer
    if Config.AUTO_SHUTDOWN_DELAY <= 0:
        return

    with shutdown_lock:
        if shutdown_timer is not None:
            shutdown_timer.cancel()

        def shutdown():
            print(f"\n{'='*60}")
            print("所有瀏覽器已離線，伺服器即將關閉...")
            print(f"{'='*60}")
            os._exit(0)

        shutdown_timer = threading.Timer(Config.AUTO_SHUTDOWN_DELAY, shutdown)
        shutdown_timer.daemon = True
        shutdown_timer.start()
        print(f"⏱ 無連線，{Config.AUTO_SHUTDOWN_DELAY} 秒後自動關閉（重新連線可取消）")


@sock.route('/ws')
def browser_ws_handler(ws_client):
    """處理瀏覽器的 WebSocket 連線"""
    print(f"瀏覽器客戶端已連線: {request.remote_addr}")
    esp32.browser_clients.add(ws_client)

    # 有新連線，取消自動關閉
    cancel_shutdown_timer()

    # 發送初始狀態
    initial_status = "ESP32_CONNECTED" if esp32.is_connected else "ESP32_DISCONNECTED"
    try:
        ws_client.send(initial_status)
    except Exception as e:
        print(f"發送初始狀態失敗: {e}")

    try:
        while True:
            data = ws_client.receive()
            if data:
                print(f"收到瀏覽器指令: {data}")

                # 處理伺服器 USB 攝影機指令
                if data == 'cEnableServerCamera':
                    if Config.CAMERA_BACKGROUND:
                        print("伺服器攝影機已在背景常駐運作，忽略開啟指令")
                    else:
                        camera.enable()
                        print("伺服器攝影機：已開啟")
                    continue

                if data == 'cDisableServerCamera':
                    if Config.CAMERA_BACKGROUND:
                        print("伺服器攝影機已在背景常駐運作，忽略關閉指令")
                    else:
                        camera.disable()
                        print("伺服器攝影機：已關閉")
                    continue

                # 將其他指令轉發給 ESP32
                if not esp32.send(data):
                    print("無法轉發，ESP32 未連線")
    except (ConnectionAbortedError, Exception) as e:
        print(f"瀏覽器連線異常: {e}")
    finally:
        print(f"瀏覽器客戶端已離線: {request.remote_addr}")
        esp32.browser_clients.discard(ws_client)

        # 檢查是否還有其他連線，若無則啟動自動關閉計時器
        if len(esp32.browser_clients) == 0:
            start_shutdown_timer()


# =============================================================================
# HTTP 路由
# =============================================================================

@app.route('/')
def index():
    """首頁"""
    return send_from_directory('.', 'web.html')


@app.route('/video_feed')
def video_feed():
    """提供 USB 鏡頭的 MJPEG 串流"""
    def generate_frames():
        while True:
            frame_bytes = camera.get_frame()
            if frame_bytes is None:
                time.sleep(0.1)
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.033)

    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/upload", methods=["POST"])
def upload_file():
    """處理檔案上傳"""
    start_time = time.time()

    # 檢查連接狀態
    if not esp32.is_connected:
        return "ESP32 連接未建立", 503

    if "file" not in request.files:
        return "未收到檔案", 400

    file = request.files["file"]
    if file.filename == "":
        return "檔案名稱無效", 400

    print(f"收到檔案上傳請求: {file.filename}")
    esp32.is_uploading = True

    try:
        sha256 = hashlib.sha256()
        total_bytes = 0

        # 分塊讀取並傳送
        while True:
            chunk = file.stream.read(Config.CHUNK_SIZE)
            total_bytes += len(chunk)
            if not chunk:
                break

            sha256.update(chunk)
            esp32.send(chunk)
            time.sleep(0.001)

        print(f"總字節數: {total_bytes}")
        print(f"SHA256: {sha256.hexdigest()}")

        # 發送結束訊號
        time.sleep(0.1)
        esp32.send("end")
        esp32.send(f"cTransmissionOver<{sha256.hexdigest()}>")
        time.sleep(0.1)

        # 等待 ESP32 確認
        if esp32.upload_done_event.wait(timeout=Config.UPLOAD_TIMEOUT):
            esp32.upload_done_event.clear()
            elapsed = time.time() - start_time
            print(f"檔案上傳成功，耗時: {elapsed:.2f} 秒")
            return "檔案上傳成功", 200
        else:
            print("等待 ESP32 回覆逾時")
            return "等待 ESP32 回覆逾時", 504

    except Exception as e:
        print(f"上傳錯誤: {e}")
        return "檔案上傳失敗", 500
    finally:
        esp32.is_uploading = False


@app.route("/update_esp32_ip", methods=["POST"])
def update_esp32_ip():
    """更新 ESP32 IP 位址"""
    try:
        data = request.get_json()
        new_ip = data.get("ip", "").strip()

        if not new_ip:
            return {"success": False, "error": "IP 位址不能為空"}, 400

        # 驗證格式: ws://X.X.X.X:PORT
        import re
        pattern = r'^ws://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}$'
        if not re.match(pattern, new_ip):
            return {"success": False, "error": "IP 格式錯誤，必須為 ws://X.X.X.X:PORT"}, 400

        # 更新配置
        Config.ESP32_IP = new_ip
        print(f"ESP32 IP 已更新為: {new_ip}")

        # 儲存配置到檔案
        if not Config.save_config():
            return {"success": False, "error": "無法儲存配置檔案"}, 500

        # 斷開現有連線並重新連接
        if esp32.ws:
            try:
                esp32.ws.close()
            except:
                pass

        # 重新連接到新的 IP
        threading.Thread(target=esp32.connect, daemon=True).start()

        return {"success": True, "ip": new_ip}, 200

    except Exception as e:
        print(f"更新 ESP32 IP 錯誤: {e}")
        return {"success": False, "error": str(e)}, 500


@app.route("/update_camera_source", methods=["POST"])
def update_camera_source():
    """更新鏡頭來源設定"""
    try:
        data = request.get_json()
        source = data.get("source", "").strip()

        if source not in ["esp32", "server"]:
            return {"success": False, "error": "無效的鏡頭來源"}, 400

        # 更新配置
        Config.CAMERA_SOURCE = source
        print(f"鏡頭來源已更新為: {source}")

        # 儲存配置到檔案
        if not Config.save_config():
            return {"success": False, "error": "無法儲存配置檔案"}, 500

        return {"success": True, "source": source}, 200

    except Exception as e:
        print(f"更新鏡頭來源錯誤: {e}")
        return {"success": False, "error": str(e)}, 500


@app.route("/get_esp32_camera_url", methods=["GET"])
def get_esp32_camera_url():
    """獲取 ESP32 相機串流 URL"""
    try:
        url = Config.get_camera_url()
        if url:
            return jsonify({"success": True, "url": url})
        else:
            return jsonify({"success": False, "error": "無法解析 ESP32 IP"}), 500
    except Exception as e:
        print(f"獲取 ESP32 相機 URL 錯誤: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/get_server_ip", methods=["GET"])
def get_server_ip():
    """獲取伺服器 IP 位址"""
    try:
        import socket

        # 獲取本地 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 這個不會實際發送封包，只是用來獲取本地 IP
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()

        # 返回完整的 URL
        server_url = f"http://{ip}:5000"

        return jsonify({"success": True, "ip": ip, "url": server_url})

    except Exception as e:
        print(f"獲取伺服器 IP 錯誤: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/get_config", methods=["GET"])
def get_config():
    """獲取當前配置"""
    try:
        return jsonify({
            "success": True,
            "esp32_ip": Config.ESP32_IP,
            "camera_source": Config.CAMERA_SOURCE,
            "camera_background": Config.CAMERA_BACKGROUND
        })
    except Exception as e:
        print(f"獲取配置錯誤: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/update_camera_background", methods=["POST"])
def update_camera_background():
    """更新鏡頭背景長駐設定"""
    try:
        data = request.get_json()
        enabled = data.get("enabled", True)

        # 更新配置
        Config.CAMERA_BACKGROUND = bool(enabled)
        print(f"鏡頭背景長駐已更新為: {Config.CAMERA_BACKGROUND}")

        # 儲存配置到檔案
        if not Config.save_config():
            return {"success": False, "error": "無法儲存配置檔案"}, 500

        return {"success": True, "enabled": Config.CAMERA_BACKGROUND}, 200

    except Exception as e:
        print(f"更新鏡頭背景長駐錯誤: {e}")
        return {"success": False, "error": str(e)}, 500


# =============================================================================
# 主程式入口
# =============================================================================


def main():
    """啟動所有服務"""
    print("=" * 60)
    print("3D Printer Web Control System")
    print("=" * 60)

    # 載入配置
    Config.load_config()
    print("✓ 配置已載入")

    # 啟動攝影機
    camera.start()
    print("✓ 攝影機線程已啟動")

    # 啟動 ESP32 連線
    threading.Thread(target=esp32.connect, daemon=True).start()
    print("✓ ESP32 連線線程已啟動")

    # 啟動狀態輪詢
    threading.Thread(target=esp32.poll_status, daemon=True).start()
    print("✓ 狀態輪詢線程已啟動")

    # 啟動 Flask 伺服器
    print("=" * 60)
    print("伺服器啟動於 http://0.0.0.0:5000")
    print("=" * 60)

    # 自動開啟瀏覽器
    def open_browser():
        time.sleep(1)  # 等待伺服器啟動
        webbrowser.open('http://127.0.0.1:5000')

    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
