"""
3D Printer Web Control System
提供瀏覽器與 ESP32 之間的 WebSocket 橋接服務
支援檔案上傳、即時狀態監控、視訊串流
"""

from flask import Flask, request, send_from_directory, Response
from flask_cors import CORS
from flask_sock import Sock
import websocket
import threading
import hashlib
import time
import cv2

# =============================================================================
# 全域設定
# =============================================================================

class Config:
    """應用程式配置"""
    ESP32_IP = "ws://192.168.1.147:82"
    MAX_RECONNECT_ATTEMPTS = 5
    RECONNECT_DELAY = 1  # 秒
    POLL_INTERVAL = 5  # 秒
    UPLOAD_TIMEOUT = 50  # 秒
    CHUNK_SIZE = 2048
    CAMERA_FPS = 30


# =============================================================================
# 攝影機管理
# =============================================================================

class CameraThread(threading.Thread):
    """線程安全的攝影機管理類別"""
    
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.frame_bytes = None
        self.lock = threading.Lock()
        self.camera = None
        self._running = True

    def run(self):
        """開啟鏡頭並持續讀取畫面"""
        print("正在啟動攝影機線程...")
        
        # 嘗試使用 DSHOW 後端
        self.camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.camera.isOpened():
            print("攝影機：DSHOW 失敗，嘗試預設後端...")
            self.camera = cv2.VideoCapture(0)

        if not self.camera.isOpened():
            print("攝影機：無法開啟 USB 鏡頭")
            self._running = False
            return

        print("攝影機：已成功開啟")
        frame_delay = 1.0 / Config.CAMERA_FPS
        
        while self._running:
            success, frame = self.camera.read()
            if not success:
                print("攝影機：讀取失敗")
                time.sleep(0.5)
                continue

            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                print("攝影機：編碼 JPEG 失敗")
                continue

            with self.lock:
                self.frame_bytes = buffer.tobytes()

            time.sleep(frame_delay)

        self.camera.release()
        print("攝影機：已停止")

    def get_frame(self):
        """線程安全地獲取最新的 JPEG 幀"""
        with self.lock:
            return self.frame_bytes

    def stop(self):
        """停止攝影機"""
        self._running = False


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
                print(f"嘗試重連 ESP32... ({self.reconnect_attempts}/{Config.MAX_RECONNECT_ATTEMPTS})")
            
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


# =============================================================================
# WebSocket 路由 (瀏覽器端)
# =============================================================================

@sock.route('/ws')
def browser_ws_handler(ws_client):
    """處理瀏覽器的 WebSocket 連線"""
    print(f"瀏覽器客戶端已連線: {request.remote_addr}")
    esp32.browser_clients.add(ws_client)
    
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
                if not esp32.send(data):
                    print("無法轉發，ESP32 未連線")
    except (ConnectionAbortedError, Exception) as e:
        print(f"瀏覽器連線異常: {e}")
    finally:
        print(f"瀏覽器客戶端已離線: {request.remote_addr}")
        esp32.browser_clients.discard(ws_client)


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


# =============================================================================
# 主程式入口
# =============================================================================

def main():
    """啟動所有服務"""
    print("=" * 60)
    print("3D Printer Web Control System")
    print("=" * 60)
    
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
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
