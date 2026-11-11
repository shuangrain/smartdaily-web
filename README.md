# 智生活社區公告與個人通知 Web (`smartdaily-web`)

[![CI Checks](https://github.com/shuangrain/smartdaily-web/actions/workflows/ci.yml/badge.svg)](https://github.com/shuangrain/smartdaily-web/actions/workflows/ci.yml)
[![GitHub Release](https://img.shields.io/github/v/release/shuangrain/smartdaily-web?color=blue)](https://github.com/shuangrain/smartdaily-web/releases)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io%2Fshuangrain%2Fsmartdaily--web-blue?logo=docker)](https://github.com/shuangrain/smartdaily-web/pkgs/container/smartdaily-web)

支援 **iOS 16.4+ 主畫面 PWA Web Push 推播通知**與桌面瀏覽器的輕量級 Web 服務，整合：
1. **📢 社區公告**：自動過濾 `ComId == ""` 的智生活平台團購廣告，並在點開公告時**即時刷新 15 分鐘效期的 PDF / 圖片附件簽名網址**。
2. **🔔 個人通知**：串接 `/api/v3.51/Message/Notification/List`，顯示物管中心發送給住戶的即時推播（含圖片附件）。
3. **📬 我的提醒**：串接 `https://notification.kingnetsmart.com.tw/api/NotificationBox/LoadNotices`，完整呈現郵務包裹、寄放物、個人通知與新公告提醒。
4. **📲 Web Push 自動推播**：背景定期輪詢最新社區公告與個人通知，有新訊息時自動透過 VAPID Web Push 推播至已訂閱的 iPhone / 電腦，並支援隨時一鍵取消訂閱。

> [!WARNING]
> **⚠️ 免責聲明 (Disclaimer)**  
> 本工具僅供個人技術測試、研究與教學交流使用，與「今網智生活（智生活科技股份有限公司）」無任何官方合作或關聯。  
> 使用本工具進行任何 API 呼叫、自動化作業所衍生之任何帳號異常、資料外洩或法律責任，開發者與貢獻者概不負任何形式之責任。請使用者確保在獲得適當授權之環境下使用，並妥善保管您的 `DEVICE_ID`（等同於長期登入憑證）。

---

## 🐳 使用 Docker 快速部署

CI 已自動建置並推送映像檔至 GitHub Container Registry (`ghcr.io/shuangrain/smartdaily-web:latest`)：

### 1. 直接使用 GHCR 映像檔啟動

```bash
docker run -d \
  --name smartdaily-web \
  --restart unless-stopped \
  -p 8000:8000 \
  -e DEVICE_ID="您的DeviceID" \
  -v $(pwd)/data:/app/data \
  ghcr.io/shuangrain/smartdaily-web:latest
```

### 2. 使用 `docker-compose.yml`

建立 `docker-compose.yml` 並直接拉取 GitHub Container Registry 映像檔啟動：

```yaml
services:
  smartdaily-web:
    image: ghcr.io/shuangrain/smartdaily-web:latest
    container_name: smartdaily-web
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - DEVICE_ID=您的DeviceID
      - POLL_INTERVAL_SECONDS=180
      - LOG_LEVEL=INFO
    volumes:
      - ./data:/app/data
```

啟動服務：

```bash
docker compose up -d
```

---

## 🚀 本機開發與啟動（使用 `uv`）

```bash
# 安裝相依套件
uv sync

# 設定環境變數並啟動服務
DEVICE_ID="您的DeviceID" uv run smartdaily-web
```

預設監聽 `http://0.0.0.0:8000`。

---

## 📱 iOS (iPhone / iPad) 開啟 PWA 推播步驟

1. 將本服務透過 HTTPS 反向代理（如 Cloudflare Tunnel、Tailscale Funnel 或 Caddy）公開。
2. 使用 iPhone 的 **Safari** 開啟網址。
3. 點擊底部「**分享**」按鈕 ➔ 選擇「**加入主畫面 (Add to Home Screen)**」。
4. 從 iPhone 桌面開啟剛剛新增的「**社區通知**」App。
5. 點擊右上角「**🔔 推播**」➔ 點擊「**🔔 開啟推播通知**」並允許權限，接著可點擊「**📨 發送測試推播**」立即驗證通知。
