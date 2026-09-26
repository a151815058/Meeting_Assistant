# Meeting Assistant 會議小助手

Flask + PostgreSQL 打造的會議小助手：即時錄音轉文字、會議平台與會者同步、
AI 會議記錄產製、自動寄送。依 SSDLC（Secure Software Development Lifecycle）
流程開發，完整需求追溯矩陣與安全設計文件見 `docs/`。

## 功能

1. 即時串流錄音 + 即時轉文字（Faster-Whisper + Silero VAD）
2. 串接 Google Meet / Microsoft Teams 取得與會者名單，並以語者分離標記發言片段
3. 會議結束後依可自訂範本，由 LLM（預設 Anthropic Claude，介面可抽換）自動產製會議記錄
4. 會議記錄透過 Gmail API / Microsoft Graph Mail API 自動寄送給所有與會者

## 專案文件（SSDLC 交付物）

- 需求追溯矩陣：`docs/requirements/requirements_matrix.md`
- 威脅建模：`docs/security/threat_model.md`
- 安全編碼規範 / OWASP Top 10 對應：`docs/security/secure_coding_checklist.md`
- 系統架構：`docs/architecture/architecture.md`

## 開發進度

目前已完成 **Phase 0（專案骨架與 SSDLC 文件）**、**Phase 1（資料模型與 OAuth）**、
**Phase 2（會議與與會者名單同步）**、**Phase 3（即時錄音轉文字）**、**Phase 4（AI 產製會議記錄）**、
**Phase 5（Email 寄送會議記錄）**。需求追溯詳見 `docs/requirements/requirements_matrix.md`。

### 從行事曆建立會議

- 以 Google／Microsoft 帳號登入後，「新增會議」頁會列出行事曆上過去 24 小時到未來 14 天的會議（`CALENDAR_LOOKBACK_HOURS`、`CALENDAR_LOOKAHEAD_DAYS` 可調）。
- 選擇後會自動帶入標題（可自行修改）；建立時自動帶入會議時間並同步與會者名單。已建立過的會議會標示「已建立」。
- 時間以 `DISPLAY_TIMEZONE`（預設 `Asia/Taipei`）顯示。範圍外的會議可在「進階」手動輸入事件 ID。

### 寄送會議記錄（Phase 5）使用說明

- 收件人一律取自會議的「與會者」名單：可從行事曆同步，或在會議頁手動新增／移除；主辦人本人不會收到。
- 在會議記錄頁確認內容並「儲存修改」後，按「寄出會議記錄」。寄出的是已儲存的版本。
- 會議記錄頁可「下載 Word」「下載 PDF」；寄信時也可勾選附加 Word／PDF 檔。文件最上方會加上會議時間、主辦人與與會者。
  PDF 需要中文字型：Windows 自動使用微軟正黑體，Docker 映像檔已安裝 Noto Sans CJK，其他環境請以 `PDF_FONT_PATH` 指定 .ttf／.ttc 字型。
- 信件以主辦人自己的帳號寄出：Google 登入走 Gmail API、Microsoft 登入走 Graph（需在登入時同意寄信權限）。
- 寄送失敗會顯示原因並可「重新寄送」；郵件服務回應 429／503 時會自動重試（`MAIL_SEND_ATTEMPTS`）。
  連線中斷時不會自動重試（信件可能已寄出），請先確認寄件備份。
- 開發模式登入的帳號沒有寄信權限，此時信件會寫入本機信箱 `instance/outbox/*.eml`（可用 Outlook／記事本開啟），
  不會真的寄出。正式環境一律關閉；若在正式設定中開啟 `MAIL_OUTBOX_ENABLED`，應用程式會拒絕啟動。

### AI 會議記錄（Phase 4）使用說明

- 需先在 `.env` 設定 `ANTHROPIC_API_KEY`（至 https://console.anthropic.com 建立），重新啟動服務後生效。
- 錄音結束後，在會議頁面按「以預設範本產生會議記錄」，或到會議記錄頁選擇範本；產生通常需數十秒到數分鐘，頁面會自動更新。
- 產生的會議記錄為草稿，請確認決議、負責人與期限後再儲存；重新產生會覆蓋修改。
- 上方選單「範本」可新增／編輯範本並設定預設範本。範本為 Markdown，可用 `{{ meeting.title }}`、`{{ participant_names }}` 等變數。
- 預設模型為 `claude-opus-5`（`LLM_MODEL` 可調）。逐字稿會傳送至 Anthropic API 處理，部署前請確認組織的資料政策。

### 即時轉錄（Phase 3）使用說明

- 進入會議頁面按「開始錄音」，瀏覽器會詢問麥克風權限；每段話說完（停頓約 0.6 秒）後約數秒逐字稿即出現。
- 第一次使用時會自動從 Hugging Face 下載 Whisper 模型（`small` 約 500 MB），之後使用本機快取。
- `WHISPER_MODEL_SIZE` 預設 `small`：CPU 上可跟上即時速度。`medium` 較準確但在 CPU 上約為即時速度的 3 倍慢，
  建議搭配 GPU（`WHISPER_DEVICE=cuda`）。效能量測見 `docs/architecture/architecture.md`。
- 原始錄音不會保存，只保存逐字稿。
- 語者分離（Speaker A/B）為選用功能：`pip install -r requirements-diarization.txt`，至 Hugging Face 同意
  `pyannote/speaker-diarization-community-1` 使用條款並建立 token，於 `.env` 設定 `DIARIZATION_ENABLED=true`、`HF_TOKEN=...`。
- 必須以單一 web process 執行（`python run.py` 或 gunicorn `-w 1`），錄音狀態存於 process 記憶體。
- 也可在「即時錄音轉文字」頁籤「上傳錄音檔」（MP3、WAV、M4A、AAC、FLAC、OGG、OPUS、WEBM、WMA、MP4），轉出的逐字稿接在現有逐字稿之後。
  錄音檔轉完後立即刪除、不會保存；大小上限 `TRANSCRIPTION_UPLOAD_MAX_BYTES`（預設 500 MB），長度上限同即時錄音（`TRANSCRIPTION_MAX_RECORDING_SECONDS`）。
  同一會議同時只能有一個音訊來源（錄音或上傳）。轉錄速度約同即時錄音（`small` 模型在 CPU 上約即時速度或更快）。

## 本機開發設置

### 1. 取得金鑰與設定

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 將輸出值填入 .env 的 TOKEN_ENCRYPTION_KEY
```

需自行至 Google Cloud Console / Azure AD 建立 OAuth 用戶端，填入
`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`、`MS_CLIENT_ID`/`MS_CLIENT_SECRET`。

- **Google**：建立「網頁應用程式」類型的 OAuth 用戶端，授權重新導向 URI 填 `http://localhost:5000/auth/google/callback`；
  啟用 Gmail API 與 Google Calendar API；OAuth 同意畫面在「測試」狀態時，需把要登入的帳號加入測試使用者。
- **Microsoft**：於 Azure「應用程式註冊」新增應用程式（平台選 Web），重新導向 URI 填 `http://localhost:5000/auth/microsoft/callback`；
  建立用戶端密碼；API 權限加入 Microsoft Graph 委派權限 `User.Read`、`Calendars.Read`、`Mail.Send`。
- 本機開發以 http 執行 callback，開發環境會自動允許（`OAUTHLIB_INSECURE_TRANSPORT`）；正式環境必須使用 https，設定此變數會拒絕啟動。
- 使用者可在同意畫面取消部分權限，仍可登入，但未授權的功能（例如寄信）會提示需重新授權。

### 2. 啟動依賴服務（PostgreSQL + Redis）

**有 Docker 的環境（建議，與正式部署一致）：**

```bash
docker compose up -d postgres redis
```

**Windows 本機沒有 Docker、也沒有系統管理員權限時**：可用已安裝的 PostgreSQL
執行檔，在使用者自己的資料夾建立一個獨立的資料庫叢集，不需要 Windows
服務控制權限：

```powershell
$PG = "C:\Program Files\PostgreSQL\17\bin"
& "$PG\initdb.exe" -D ".devdb\pgdata" -U postgres --auth=trust -E UTF8
& "$PG\pg_ctl.exe" -D ".devdb\pgdata" -o "-p 5433" -l ".devdb\log\postgres.log" start
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -p 5433 -c "CREATE ROLE meeting_assistant WITH LOGIN PASSWORD 'meeting_assistant';"
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -p 5433 -c "CREATE DATABASE meeting_assistant OWNER meeting_assistant;"
& "$PG\psql.exe" -U postgres -h 127.0.0.1 -p 5433 -c "CREATE DATABASE meeting_assistant_test OWNER meeting_assistant;"
```

`.env` 的 `DATABASE_URL` 對應改成 `postgresql+psycopg2://meeting_assistant:meeting_assistant@localhost:5433/meeting_assistant`。
停止此實例：`& "$PG\pg_ctl.exe" -D ".devdb\pgdata" stop`；下次要用時重新
`pg_ctl start` 即可（`.devdb/` 已加入 `.gitignore`，不會進版控）。

### 3. 安裝套件與初始化資料庫

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements-dev.txt

flask db init
flask db migrate -m "initial schema"
flask db upgrade
```

### 4. 啟動應用程式

```bash
flask run
# 或使用 SocketIO 開發伺服器
python run.py
```

瀏覽 http://localhost:5000/auth/login

**尚未申請 OAuth 金鑰時**：開發環境（`FLASK_ENV=development`）的登入頁下方會出現「開發模式登入」，
輸入任意 Email 與名稱即可登入測試（僅接受本機 127.0.0.1 連線）。此帳號沒有行事曆/郵件權限，
無法同步與會者，寄信會改寫入本機信箱（見上方 Phase 5 說明），但可建立會議、手動新增與會者與測試即時錄音。正式環境一律關閉；若在正式設定中開啟，應用程式會拒絕啟動。
不需要時可在 `.env` 設定 `DEV_LOGIN_ENABLED=false` 關閉。

## 測試

```bash
# 對照上方「本機開發設置」使用的 PostgreSQL 位置調整連線字串
TEST_DATABASE_URL="postgresql+psycopg2://meeting_assistant:meeting_assistant@localhost:5433/meeting_assistant_test" pytest tests/ -v
bandit -r app -ll         # SAST 安全掃描，Must 為 0 個 medium/high 發現
```

Phase 0-5 已於本機無 Docker、以獨立 PostgreSQL 17 實例驗證：215/215 測試（含 UI 版面、即時轉錄、上傳錄音檔、開發模式登入、OAuth 登入與取消授權、行事曆下拉選單、會議記錄、匯出 Word／PDF、寄送會議記錄測試）
通過、`flask db migrate`/`upgrade` 產生並套用正確的 8 張資料表、`flask run`
起服務後 `/healthz`、`/auth/login`、`/meetings/`（未登入導向登入頁）皆回應正常、
bandit 掃描 0 個 medium/high 發現。

## 已知限制

- MVP 階段的「發言人辨識」為語者分離聚類（Speaker A/B），非平台原生說話事件；
  精準身分綁定需 Bot 加入會議（Phase 6，未實作）。詳見 RTM 風險登記 RISK-01。
- 瀏覽器端錄音僅能擷取使用者本機聽到的聲音，非會議平台端乾淨多軌音訊。

## 授權

內部專案，未指定開源授權。
