# 系統架構

## 元件圖

```
                     ┌────────────────────┐
                     │   瀏覽器（前端）    │
                     │  getUserMedia 錄音  │
                     └─────────┬──────────┘
                    HTTPS│      │WebSocket(TLS)
                         v      v
                ┌───────────────────────────┐
                │   Flask App (app/)        │
                │  ┌─────────┐ ┌──────────┐ │
                │  │ auth    │ │ meetings │ │
                │  │ (OAuth) │ │ (CRUD/   │ │
                │  │         │ │ 名單同步) │ │
                │  └─────────┘ └──────────┘ │
                │  ┌─────────────┐┌────────┐│
                │  │transcription││minutes ││
                │  │(SocketIO/   ││(範本+  ││
                │  │ VAD/Whisper)││ LLM)   ││
                │  └─────────────┘└────────┘│
                │  ┌────────────────────┐   │
                │  │ notifications (Mail)│   │
                │  └────────────────────┘   │
                └──────────┬────────────────┘
                            │
           ┌────────────────┼─────────────────┐
           v                v                  v
   ┌───────────────┐ ┌─────────────┐  ┌────────────────┐
   │  PostgreSQL    │ │ Redis+Celery│  │ 外部供應商       │
   │ users/meetings/│ │ 背景任務     │  │ Google/Microsoft│
   │ participants/  │ │ (轉錄後處理/ │  │ OAuth+Calendar+ │
   │ transcript/    │ │  LLM/寄信)   │  │ Mail, Anthropic │
   │ minutes/audit  │ └─────────────┘  │ Claude API      │
   └───────────────┘                   └────────────────┘
```

## 模組職責

| 模組 | 職責 |
|---|---|
| `app/auth` | Google/Microsoft OAuth 2.0 登入、token 儲存與刷新（`token_service.py`） |
| `app/meetings` | 會議 CRUD、與會者名單同步（`calendar_sync.py`） |
| `app/transcription` | WebSocket 音訊接收（`__init__.py` 事件處理、`schemas.py` 輸入驗證）、錄音 session 與背景處理（`streaming.py`）、VAD 斷句（`vad.py`）、Faster-Whisper 轉錄（`asr.py`）、語者分離（`diarization.py`，選用） |
| `app/minutes` | 範本 CRUD 與會議記錄頁（`routes.py`）、輸入驗證（`schemas.py`）、prompt 組裝與範本沙箱（`prompting.py`）、背景產生工作與長逐字稿分段（`generator.py`）、`LLMProvider` 介面與 Anthropic 實作（`providers/`） |
| `app/notifications` | 寄送會議記錄：收件人／信件組成、寄件帳號選擇、重試與稽核（`mailer.py`）；Gmail API／Graph／開發用本機信箱傳送（`senders.py`） |
| `app/security` | 欄位加解密（`crypto.py`）、稽核紀錄（`audit.py`） |
| `app/models` | SQLAlchemy ORM models：`User`, `OAuthAccount`, `Meeting`, `Participant`, `TranscriptSegment`, `MinutesTemplate`, `Minutes`, `AuditLog` |

## 資料庫 ER 概念圖

```
User 1───* OAuthAccount
User 1───* Meeting
Meeting 1───* Participant
Meeting 1───* TranscriptSegment
Meeting 1───1 Minutes
MinutesTemplate 1───* Minutes
User/System ───* AuditLog
```

## 關鍵設計決策

1. **音訊來源為瀏覽器端錄音（非 Bot 加入會議）**：MVP 階段以最低整合門檻取得音訊；`TranscriptSegment.platform_speaker_id` 欄位預留給 Phase 6 Bot 整合覆蓋，避免未來重構資料模型（見 RTM RISK-01）。
2. **LLM 供應商介面化**：`app/minutes/providers/base.py` 定義 `LLMProvider` 抽象介面，預設 `AnthropicProvider`，未來可新增 `OpenAIProvider` 等，不影響 `MinutesGeneratorService` 呼叫端。
3. **Email 寄送複用 OAuth 憑證**：不引入獨立 SMTP 帳密管理，寄件人即會議主辦人本人，符合最小權限與可稽核性。
4. **慢速工作在 web process 內以背景工作執行**：即時轉錄與 LLM 產生會議記錄都是秒級到分鐘級的操作，不可阻塞 request-response。本機開發環境無 Redis，因此兩者皆以 `app/background.py`（eventlet 背景 greenlet + `tpool` 真實執行緒）在 web process 內執行，不經 Celery；限制為單一 web process（RISK-06、RISK-08）。寄信（Phase 5）只需一次 API 呼叫（約 1 秒），因此在 request 內同步完成，HTTP 呼叫同樣以 `run_blocking` 放到執行緒；擴充為多 worker 時再改為 Celery。
5. **原始音訊不落地**：只保存逐字稿，音訊僅在記憶體中分段處理（REQ-28）。

## Phase 3：即時轉錄資料流

```
瀏覽器                                        Flask-SocketIO（/transcription）
getUserMedia ─► AudioWorklet（降頻 16kHz PCM16）
  每 250 ms ─── audio_chunk（binary）────────► 驗證型別/大小（schemas.py）─► 限速/時長上限（TokenBucket）
                                                    │ 放入 session.inbox（handler 立即返回）
                                                    ▼
                                             每連線背景 worker（依序處理，保證順序）
                                               ├─ SpeechSegmenter：Silero VAD，停頓 600 ms 斷句，最長 15 秒
                                               ├─ WhisperTranscriber：eventlet tpool 執行緒中推論，不阻塞其他連線
                                               ├─ 寫入 TranscriptSegment（PostgreSQL）
  transcript_segment ◄──────────────────────── └─ emit 至 room「meeting:<id>」
stop_recording ────────────────────────────► flush 剩餘語音 ─► status=transcribed ─► 稽核紀錄
  recording_stopped ◄─────────────────────────┘ （選用）pyannote 語者分離 ─► speaker_labels
```

- 時間軸：同一會議可多次錄音，後續錄音的 `start_ms` 接續既有逐字稿最後的 `end_ms`。
- 模型預熱：使用者開啟會議頁面、SocketIO 連線建立時即在背景載入模型，按下錄音時不需再等待。
- 單一 process 限制：錄音 session 保存在 process 記憶體，需以單一 web worker 運行（Dockerfile 已為 `-w 1`），見 RTM RISK-06。
- `SOCKETIO_MESSAGE_QUEUE`：僅在多 process 需要共享 SocketIO 事件時設定（例如 Celery 要推播）；本機無 Redis 時留空。

## Phase 4：會議記錄產生

```
會議頁／會議記錄頁  POST /meetings/<id>/minutes/generate（CSRF、主辦人檢查、需有逐字稿且非錄音中）
   │ status=processing，啟動背景工作，頁面每 3 秒輪詢 /minutes/status
   ▼
generator：套用範本（強化沙箱）─► 組 prompt（逐字稿包在 <transcript> 資料標籤）─► count_tokens
   ├─ 未超過 MINUTES_MAX_INPUT_TOKENS：單次呼叫
   └─ 超過：依逐字稿段落切成數段 ─► 每段整理成筆記（map）─► 由筆記撰寫會議記錄（reduce）
   ▼
AnthropicProvider：claude-opus-5、adaptive thinking、串流、fallbacks="default"
   ├─ 成功：寫入 Minutes（草稿）、status=minutes_ready、稽核 minutes.generated（模型、token 用量、段數）
   └─ 失敗：還原 status、稽核 minutes.generation_failed、頁面顯示對應錯誤訊息
主辦人於會議記錄頁檢視／修改後儲存（稽核 minutes.edited），Phase 5 再寄出
```

## Phase 5：寄送會議記錄

```
會議頁：與會者名單 ◄─ 行事曆同步（REQ-08）或主辦人手動新增／移除（REQ-35，稽核 participant.added/removed）
   │
會議記錄頁：列出收件人（Participant 資料表，排除主辦人、去重、略過格式錯誤）與寄件方式
   │ POST /meetings/<id>/minutes/send（CSRF、主辦人檢查、確認對話框；不接受任何收件人參數）
   ▼
mailer.send_minutes：檢查 產生中／寄送中／無收件人／超過上限／冷卻時間 ─► 組信（主旨、收件人名稱去除控制字元）
   ▼
選擇寄件方式（REQ-16）：主辦人 OAuthAccount 中具寄信權限者，優先會議所屬平台
   ├─ Google（gmail.send）─► token_service 取得 access token ─► Gmail API messages.send（MIME, base64url）
   ├─ Microsoft（Mail.Send）─► Graph /me/sendMail（JSON，存寄件備份）
   └─ 皆無且為開發環境（MAIL_OUTBOX_ENABLED）─► 寫入 instance/outbox/*.eml（REQ-36）
   ▼ HTTP 呼叫於 run_blocking 執行緒；429/503 自動重試（MAIL_SEND_ATTEMPTS，遞增等待），連線錯誤不重試
   ├─ 成功：Minutes.status=sent、sent_at、Meeting.status=sent、稽核 mail.sent（backend、收件人清單、attempts）
   └─ 失敗：Minutes.status=send_failed（曾寄出成功者維持 sent）、稽核 mail.send_failed、頁面顯示原因與「重新寄送」
```

- 信件內容為純文字（會議記錄 Markdown 原文 + 說明由 AI 產生並經主辦人確認的頁尾）；寄出的是資料庫中已儲存的版本。
- 同一會議同時只允許一個寄送（process 記憶體鎖，單一 web process 前提同 RISK-06）；寄送中不可編輯或重新產生。
- 設定：`MAIL_MAX_RECIPIENTS`（100）、`MAIL_SEND_ATTEMPTS`（3）、`MAIL_RETRY_BACKOFF_SECONDS`（2）、`MAIL_RESEND_COOLDOWN_SECONDS`（60）、
  `MAIL_OUTBOX_ENABLED`（僅開發環境）、`MAIL_OUTBOX_DIR`。

- 模型設定：`LLM_MODEL`（預設 `claude-opus-5`）、`LLM_EFFORT`（預設 `high`）、`LLM_MAX_OUTPUT_TOKENS`。
- 1M context 下 4 小時會議逐字稿約數萬 token，一般不會觸發分段；分段機制為極長會議的保護。
- 伺服器啟動時（`run.py`）即預先載入語音模型；本機以 Python 用戶端測試時請用 `127.0.0.1` 而非 `localhost`
  （Windows 上 `localhost` 先嘗試 IPv6，每個新連線多約 2 秒；瀏覽器不受影響）。

### Whisper 模型效能量測（2026-09-24，本機 CPU、int8、3 句中文測試語音共 13.9 秒）

| 模型 | beam | 即時倍率（RTF，<1 才跟得上即時） | 辨識結果 |
|---|---|---|---|
| medium | 5 | 3.0–3.5 | 3/3 句完全正確 |
| medium | 1 | 2.04 | 3/3 句完全正確 |
| **small（預設）** | 5 | **0.82** | 3/3 句正確（「下週」寫成「下周」） |
| small | 1 | 0.72 | 同上 |
| base | 5 | 0.29 | 3/3 句正確（測試語音為 TTS 合成，真實會議語音預期 base 誤差較大） |

結論：CPU 環境預設 `small`；`medium` 以上建議搭配 GPU（`WHISPER_DEVICE=cuda`）。

## 測試執行紀錄

Phase 0-2 完成後於本機安裝 PostgreSQL 17（因無 Docker/系統管理員權限，改以使用者
自建的獨立資料庫叢集，監聽 5433 埠，詳見 README「本機開發設置」）執行完整驗證：

```
flask db init && flask db migrate -m "initial schema" && flask db upgrade
pytest tests/ -v
bandit -r app -ll
```

結果：
- `flask db migrate` 正確偵測全部 8 張資料表並產生 migration，`flask db upgrade` 成功套用到真實 PostgreSQL
- `pytest tests/ -v`：15 passed（涵蓋 OAuth 登入/callback、與會者名單同步、模型唯一性約束於真實 PostgreSQL 下的行為、token 加解密）
- `bandit -r app -ll`：0 個 medium/high 發現
- `flask run` 啟動後手動驗證 `/healthz`（200）、`/auth/login`（200）、`/meetings/` 未登入時導向登入頁（302）

Phase 3 完成後（2026-09-24）：
- `pytest tests/ -v`：47 passed（新增 30 案例：VAD 斷句、Whisper 包裝、語者標籤指派、輸入驗證、限速、權限、完整錄音流程、斷線收尾、語者分離暫存檔刪除）
- `bandit -r app -ll`：0 個 medium/high 發現
- 對執行中的開發伺服器端對端實測（python-socketio 用戶端，以即時速度串流 20 秒中文測試語音）：3 句皆正確轉錄並即時推播，
  每句說完約 4–6 秒內出現、寫入 PostgreSQL、狀態轉為 `transcribed`、稽核紀錄完整；跨站 Origin 連線被拒（HTTP 400）
- 瀏覽器實測：會議頁面錄音區塊正常顯示、SocketIO 由 polling 升級為 websocket、AudioWorklet 將 48 kHz / 44.1 kHz 測試音正確降頻為 16 kHz PCM16

Phase 4 完成後（2026-09-24）：
- `pytest tests/ -v`：104 passed（新增 46 案例：範本 CRUD／驗證／權限、沙箱 SSTI 與資源耗盡防護、prompt 組裝與標籤跳脫、
  長逐字稿分段、Anthropic provider 請求參數／錯誤對應／無金鑰處理、產生／失敗還原／編輯／並行保護）
- `bandit -r app -ll`：0 個 medium/high 發現
- 對執行中伺服器端對端實測：開發模式登入 → 建立範本（SSTI 範本被拒）→ 建立會議 → 即時錄音（3 句正確轉錄）→ 產生會議記錄；
  因本機未設定 `ANTHROPIC_API_KEY`，背景工作正確回報「尚未設定 LLM 金鑰」、會議狀態還原為 `transcribed`、稽核紀錄完整。
  實際呼叫 Claude 產生會議記錄待設定金鑰後驗證。
- 修正：語音模型預熱原本在事件迴圈上執行 import，首次連線時整個伺服器停頓約 2 秒；改為伺服器啟動時於背景執行緒預載
- 修正（2026-09-24）：設定 `ANTHROPIC_API_KEY` 實測時，串流回應的訊息物件沒有 `_request_id`，記錄 log 時拋錯導致產生失敗；
  改由 `stream.request_id` 取得，修正後真實 Claude 呼叫產生會議記錄成功

Phase 5 完成後（2026-09-24）：
- `pytest tests/ -v`：142 passed（新增 38 案例：Gmail／Graph 請求格式、HTTP 錯誤對應與可重試判斷、本機信箱、收件人去重與排除主辦人、
  郵件標頭注入防護、寄件帳號權限選擇、寄送／失敗／重試／冷卻／並行保護、收件人不可由請求指定、與會者新增移除與驗證、權限）
- 修正測試夾具：每個測試建立新的 engine 但未關閉連線池，測試數增加後耗盡 PostgreSQL 連線上限；teardown 改為 `db.engine.dispose()`
- `bandit -r app -ll`：0 個發現
- 對執行中伺服器端對端實測（開發模式登入、CSRF 開啟）：新增會議 → 新增與會者（格式錯誤與大小寫重複被拒）→ 無會議記錄時寄送回 404 →
  缺 CSRF token 回 400 → 寄出（附帶偽造的 `to` 參數被忽略）→ 本機信箱 .eml 的寄件人、2 位收件人（不含主辦人）、主旨、內容正確 →
  立即重寄被冷卻時間擋下 → 狀態 `sent`、稽核紀錄完整。真實 Gmail／Graph 寄送待設定 OAuth 用戶端後驗證。

OAuth 修正（2026-09-24，REQ-06／07／38／39）：
- 原測試整段模擬 OAuth 函式庫，未發現以下問題；改以真實 `google-auth-oauthlib`／oauthlib／msal 邏輯測試（僅模擬對外 HTTP）：
  Microsoft 權限清單含 msal 保留權限 `offline_access`（登入即 `ValueError`）、Google PKCE `code_verifier` 未帶到 callback、
  本機 http callback 被 oauthlib 拒絕、使用者取消部分權限時 oauthlib 拋 `Warning`；並補上取消授權／供應商錯誤／換 token 失敗的處理
- `pytest tests/ -v`：157 passed（新增 15 案例）；逐一還原各 bug 確認對應測試會失敗；`bandit -r app -ll`：0 個發現
- 設定 Google OAuth 用戶端後，使用者實際以 Google 帳號登入成功（取得全部 5 個權限）

行事曆下拉選單（2026-09-24，REQ-40）：
- 新增會議頁以 Calendar API `events.list`（`singleEvents`）／Graph `calendarView` 列出近期會議；建立時以 `events.get`／`/me/events/{id}` 重新讀取
  事件取得標題與時間，並立即同步與會者。API 呼叫以 `run_blocking` 在執行緒執行；時間以 UTC 儲存、以 `DISPLAY_TIMEZONE` 顯示
- 同時補上新增會議的輸入驗證（`MeetingSchema`）與 Graph 事件 ID 的 URL 編碼
- `pytest tests/ -v`：179 passed（新增 22 案例）；`bandit -r app -ll`：0 個發現；對真實 Google Calendar API 實測讀取成功

匯出 Word／PDF（2026-09-24，REQ-41）：
- `app/minutes/export.py`：markdown-it（停用原始 HTML）將已儲存的會議記錄解析為區塊模型，python-docx 與 fpdf2 共用同一模型輸出，
  兩種格式結構一致；PDF 內嵌中文字型子集（範例約 40–80 KB），CJK 文字以逐字換行避免超出頁面
- 寄信附件由 `mailer.build_attachments` 於寄送前產生（失敗或超過 `MAIL_MAX_ATTACHMENT_BYTES` 即不寄出）；Gmail／本機信箱走 MIME
  （中文檔名依 RFC 2231 編碼），Graph 使用 `fileAttachment`
- `pytest tests/ -v`：193 passed（新增 14 案例）；`bandit -r app -ll`：0 個發現；目視檢查 PDF 版面；對執行中伺服器實測下載與寄送附件
