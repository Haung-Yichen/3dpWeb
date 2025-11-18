# web/main.py
from flask import Flask, request, send_from_directory, Response
from flask_cors import CORS
from flask_sock import Sock
import websocket
import threading
import os
import hashlib
import time
import tempfile
import cv2  # OpenCV 導入

app = Flask(__name__, static_folder="static")
CORS(app)
sock = Sock(app)

# --- ESP32 相關 (不變) ---
esp32_ip = "ws://192.168.1.146:82"
ws_to_esp32 = None
upload_done_event = threading.Event()
reconnect_attempts = 0
max_reconnect_attempts = 5
is_connected_to_esp32 = False
reconnect_lock = threading.Lock()
browser_clients = set()

# -----------------------------------------------------------------
# [ 新增：線程安全的攝影機管理類別 ]
# -----------------------------------------------------------------


class CameraThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True  # 設置為守護線程，主程式退出時自動退出
        self.frame_bytes = None  # 儲存 JPEG 編碼後的幀
        self.lock = threading.Lock()  # 線程鎖，用於安全地更新/讀取幀
        self.camera = None
        self._running = True

    def run(self):
        """
        線程的主體：開啟鏡頭並持續讀取畫面。
        """
        print("正在啟動攝影機線程...")

        # 嘗試使用 DSHOW 後端 (更穩定)，失敗則退回預設
        self.camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.camera.isOpened():
            print("攝影機線程：DSHOW 失敗，嘗試 MSMF (預設)...")
            self.camera = cv2.VideoCapture(0)

        if not self.camera.isOpened():
            print("攝影機線程：致命錯誤 - 無法開啟 USB 鏡頭。")
            self._running = False
            return

        print("攝影機線程：攝影機已成功開啟。")
        while self._running:
            success, frame = self.camera.read()
            if not success:
                print("攝影機線程：讀取鏡頭失敗")
                time.sleep(0.5)  # 稍作等待後重試
                continue

            # 將幀編碼為 JPEG
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                print("攝影機線程：編碼 JPEG 失敗")
                continue

            # 使用鎖來安全地更新共享的幀變數
            with self.lock:
                self.frame_bytes = buffer.tobytes()

            # 控制迴圈速率，不需要快於 30fps
            time.sleep(0.033)  # 約 30 fps

        # 釋放資源
        self.camera.release()
        print("攝影機線程：已釋放鏡頭並停止。")

    def get_frame(self):
        """
        線程安全地獲取最新的 JPEG 幀。
        """
        with self.lock:
            return self.frame_bytes

    def stop(self):
        self._running = False

# -----------------------------------------------------------------
# [ A. 面向 "瀏覽器" 的 WebSocket 伺服器 ] (不變)
# -----------------------------------------------------------------


@sock.route('/ws')
def browser_ws_handler(ws_client):
    # ... (此處程式碼與您原本的 'web/main.py' 完全相同，故省略) ...
    global ws_to_esp32, is_connected_to_esp32
    print(f"瀏覽器客戶端已連線: {request.remote_addr}")
    browser_clients.add(ws_client)
    try:
        while True:
            data = ws_client.receive()
            if data:
                print(f"收到瀏覽器指令: {data}")
                if is_connected_to_esp32 and ws_to_esp32:
                    try:
                        ws_to_esp32.send(data)
                    except Exception as e:
                        print(f"轉發指令到 ESP32 失敗: {e}")
                else:
                    print("無法轉發，ESP32 未連線")
    except ConnectionAbortedError:
        print(f"瀏覽器客戶端連線中斷: {request.remote_addr}")
    except Exception as e:
        print(f"瀏覽器 WS 發生錯誤: {e}")
    finally:
        print(f"瀏覽器客戶端已離線: {request.remote_addr}")
        browser_clients.remove(ws_client)


# -----------------------------------------------------------------
# [ B. 面向 "ESP32" 的 WebSocket 客戶端 ] (不變)
# -----------------------------------------------------------------
def ws_connect_to_esp32():
    # ... (此處程式碼與您原本的 'web/main.py' 完全相同，故省略) ...
    global ws_to_esp32, reconnect_attempts, is_connected_to_esp32

    def on_message(ws, msg):
        global browser_clients
        print("ESP32 回覆:", msg)
        dead_clients = set()
        for client in browser_clients:
            try:
                client.send(msg)
            except Exception as e:
                print(f"廣播給瀏覽器 {client} 失敗: {e}，將其標記為移除")
                dead_clients.add(client)
        for client in dead_clients:
            browser_clients.remove(client)
        if msg.strip().lower() == "ok":
            upload_done_event.set()
        elif msg == "upload success":
            print("ESP32 顯示檔案上傳成功")

    def on_open(ws):
        global reconnect_attempts, is_connected_to_esp32
        print("成功連接到 ESP32")
        with reconnect_lock:
            reconnect_attempts = 0
            is_connected_to_esp32 = True

    def on_close(ws, code, msg):
        global is_connected_to_esp32
        print("WebSocket 連線關閉 (ESP32)")
        with reconnect_lock:
            is_connected_to_esp32 = False
        threading.Thread(target=attempt_reconnect_esp32, daemon=True).start()

    def on_error(ws, err):
        print(f"WebSocket 錯誤 (ESP32): {err}")
    ws_to_esp32 = websocket.WebSocketApp(
        esp32_ip,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open
    )
    ws_to_esp32.run_forever()


def attempt_reconnect_esp32():
    # ... (此處程式碼與您原本的 'web/main.py' 完全相同，故省略) ...
    global reconnect_attempts, ws_to_esp32, is_connected_to_esp32
    with reconnect_lock:
        if is_connected_to_esp32:
            return
        if reconnect_attempts >= max_reconnect_attempts:
            print(f"重連 ESP32 失敗，已嘗試 {max_reconnect_attempts} 次")
            return
    while reconnect_attempts < max_reconnect_attempts:
        with reconnect_lock:
            if is_connected_to_esp32:
                return
            reconnect_attempts += 1
            print(
                f"嘗試重連 ESP32... ({reconnect_attempts}/{max_reconnect_attempts})")
        time.sleep(5)
        try:
            threading.Thread(target=ws_connect_to_esp32, daemon=True).start()
            break
        except Exception as e:
            print(f"重連 ESP32 嘗試 {reconnect_attempts} 失敗: {e}")
            if reconnect_attempts >= max_reconnect_attempts:
                print("重連 ESP32 失敗，已達到最大嘗試次數")

# -----------------------------------------------------------------
# [ 修改：視訊串流函式 ]
# -----------------------------------------------------------------
def generate_frames():
    """
    生成 USB 鏡頭的視訊幀 (從共享的攝影機線程)。
    """
    global cam_thread  # 存取全域的攝影機線程實例
    while True:
        # 從攝影機線程獲取最新的幀
        frame_bytes = cam_thread.get_frame()

        if frame_bytes is None:
            # 攝影機可能尚未啟動或讀取失敗，稍後重試
            # print("generate_frames: 等待第一幀...")
            time.sleep(0.1)
            continue

        # 傳送 MJPEG 格式的幀
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

        # 稍微延遲，避免佔用過多 CPU
        time.sleep(0.033)


@app.route('/video_feed')
def video_feed():
    """
    提供 USB 鏡頭的 MJPEG 串流。
    (此函式不變，但它呼叫的 'generate_frames' 已被修改)
    """
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# -----------------------------------------------------------------
# [ HTTP 路由 ] (不變)
# -----------------------------------------------------------------
@app.route('/')
def index():
    return send_from_directory('.', 'web.html')


@app.route("/upload", methods=["POST"])
def upload_file():
    # 使用 main.py 的全域變數
    global upload_done_event, is_connected_to_esp32, ws_to_esp32
    spend_time = time.time()

    # 檢查連接狀態
    if not is_connected_to_esp32 or ws_to_esp32 is None:
        return "ESP32 連接未建立", 503

    if "file" not in request.files:
        return "未收到檔案", 400

    file = request.files["file"]
    if file.filename == "":
        return "檔案名稱無效", 400

    print(f"收到檔案上傳請求: {file.filename}")
    time.sleep(0.01)

    try:
        sha256 = hashlib.sha256()
        chunk_size = 1024
        total_bytes = 0

        # 直接從請求的串流中讀取，而不是存成暫存檔
        while True:
            chunk = file.stream.read(chunk_size)
            total_bytes += len(chunk)
            if not chunk:
                break
            sha256.update(chunk)
            # 直接透過 ws_to_esp32 傳送
            ws_to_esp32.send(chunk.decode('utf-8'))
            time.sleep(0.001)  # 避免傳輸過快

        print("total bytes:", total_bytes)
        print("SHA256:", sha256.hexdigest())

        time.sleep(0.1)
        ws_to_esp32.send("end")
        ws_to_esp32.send(f"cTransmissionOver<{total_bytes}>")
        time.sleep(0.1)

        # [ 關鍵 ] 在 HTTP 路由中直接等待 ESP32 回覆 'ok'
        if upload_done_event.wait(timeout=50):
            upload_done_event.clear()  # 重置
            print("檔案上傳成功，回傳 200")
            print(f"上傳花費時間: {time.time() - spend_time} 秒")
            return "檔案上傳成功", 200
        else:
            print("等待 ESP32 回覆逾時，回傳 504")
            return "等待 ESP32 回覆逾時", 504

    except Exception as e:
        print(f"錯誤: {e}")
        return "檔案上傳失敗", 500


# -----------------------------------------------------------------
# [ 修改：伺服器啟動 ]
# -----------------------------------------------------------------

# 建立一個全域的攝影機線程實例
cam_thread = CameraThread()

if __name__ == "__main__":

    # [ 1. 啟動攝影機線程 ]
    cam_thread.start()

    # [ 2. 啟動 ESP32 連線線程 ] (您原本的程式)
    threading.Thread(target=ws_connect_to_esp32, daemon=True).start()

    # [ 3. 啟動 Flask 網頁伺服器 ] (您原本的程式)
    print("伺服器啟動於 http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
