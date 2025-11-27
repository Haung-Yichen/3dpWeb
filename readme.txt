# 3D 列印機網頁控制系統

一個基於 Flask 的網頁應用程式，提供瀏覽器與 ESP32 之間的 WebSocket 橋接服務，
支援 3D 列印機的遠端控制、檔案上傳、即時狀態監控及視訊串流功能。

---

## 系統架構

```
瀏覽器 <--WebSocket--> Flask 伺服器 <--WebSocket--> ESP32 <--> STM32 <--> 3D 列印機
```

---

## 功能特色

- **即時通訊**：瀏覽器與 ESP32 間的雙向 WebSocket 通訊
- **檔案上傳**：支援 .gcode / .gco 檔案上傳至 ESP32 SD 卡，含 SHA256 校驗
- **列印控制**：從 SD 卡選擇檔案並開始列印
- **狀態監控**：即時顯示溫度、列印進度、連線狀態
- **視訊串流**：支援 ESP32-CAM 或伺服器端 USB 攝影機
- **配置持久化**：設定自動儲存至 config.json

---

## 環境需求

- Python 3.8+
- 相依套件（見 requirements.txt）

---

## 安裝與執行

1. **建立虛擬環境（建議）**
   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   source .venv/bin/activate # Linux/Mac
   ```

2. **安裝相依套件**
   ```bash
   pip install -r requirements.txt
   ```

3. **啟動伺服器**
   ```bash
   python main.py
   ```

4. **開啟瀏覽器**
   - 本機：http://127.0.0.1:5000
   - 區網其他裝置：http://<伺服器IP>:5000

---

## 設定說明

首次執行後，可透過網頁右側選單「設定」進行配置：

| 設定項目 | 說明 |
|---------|------|
| ESP32 IP | ESP32 的 WebSocket 位址（格式：ws://X.X.X.X:82）|
| 鏡頭來源 | 選擇 ESP32-CAM 或伺服器端 USB 攝影機 |
| 鏡頭背景長駐 | 開啟後伺服器 USB 攝影機會在背景持續運作（需重啟生效）|

設定會自動儲存至 `config.json`。

---

## 檔案結構

```
3dpWeb/
├── main.py           # 後端伺服器主程式
├── web.html          # 前端網頁介面
├── config.json       # 配置檔案（自動產生）
├── requirements.txt  # Python 相依套件
└── readme.txt        # 本說明文件
```

---

## 常用指令

| 指令 | 說明 |
|-----|------|
| cGetAllFiles | 取得 SD 卡檔案列表 |
| cStartToPrint<檔名> | 開始列印指定檔案 |
| cEnableCamera | 開啟 ESP32 攝影機 |
| cDisableCamera | 關閉 ESP32 攝影機 |

---

## 注意事項

- ESP32 需先連上同一區域網路
- 首次使用請在「設定」中輸入正確的 ESP32 IP
- 上傳大型 .gcode 檔案時請耐心等待校驗完成
