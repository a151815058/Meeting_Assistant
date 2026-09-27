# 威脅建模 (Threat Model)

SSDLC 安全需求分析交付物，採 STRIDE 方法對主要資料流進行分析。

## 資產與資料分類

| 資產 | 分類 | 說明 |
|---|---|---|
| OAuth refresh token（Google/Microsoft） | 機密 | 可用於長期存取使用者行事曆與郵件，外洩等同帳號被接管 |
| 會議逐字稿 / 錄音音訊 | 機密（可能含個資、商業機密） | 會議內容原始資料 |
| 會議記錄（AI 產出） | 內部 | 由逐字稿衍生，寄送對象為與會者 |
| 與會者名單（姓名、Email） | 個資 | 來自 Calendar/Graph API |
| 稽核紀錄 | 內部（供安全事件調查） | 記錄敏感操作 |

## 資料流與信任邊界

```
[瀏覽器/麥克風] --WebSocket(TLS)--> [Flask/SocketIO] --> [Redis/Celery] --> [Faster-Whisper/VAD/pyannote]
                                            |
                                            v
[Google/Microsoft OAuth] <--HTTPS--> [Flask 後端] <--HTTPS--> [Anthropic/LLM Provider]
                                            |
                                            v
                                     [PostgreSQL / Supabase]（Supabase：Session pooler，sslmode=require）
                                            |
                                            v
                              [Gmail API / Graph Mail API] --> 與會者信箱
```

信任邊界：使用者瀏覽器 ↔ 應用伺服器；應用伺服器 ↔ 第三方 OAuth/LLM/郵件供應商；應用伺服器 ↔ 資料庫。

## STRIDE 分析

| 類別 | 威脅情境 | 對應控制 |
|---|---|---|
| Spoofing（偽冒） | 攻擊者偽冒 OAuth callback 竊取授權碼；CSRF 偽冒登入 session；開發模式登入被誤用於正式環境或由區網他人使用（REQ-29：僅開發設定啟用、正式環境拒絕啟動、僅限本機來源）；惡意網站以使用者 cookie 開啟 WebSocket 竊聽/注入逐字稿（Cross-Site WebSocket Hijacking） | OAuth `state` 參數驗證（`app/auth/routes.py`）；REQ-52：僅在 `TRUSTED_PROXY_HOPS` 設定的反向代理（Render）後才採用 `X-Forwarded-*`，預設忽略，避免使用者偽造來源 IP（影響稽核與開發模式登入的本機限制）或偽造 https；`Flask-WTF` CSRF 保護於表單提交；REQ-27：SocketIO 僅允許同源 Origin（`cors_allowed_origins=None`），且 `/transcription` 連線須已登入 |
| Tampering（竄改） | 攻擊者竄改 WebSocket 音訊 metadata 或範本內容注入惡意內容（SSTI）；逐字稿文字夾帶 HTML/JS 或對 LLM 的指令（prompt injection）；上傳偽裝成音訊的惡意檔案攻擊解碼器（FFmpeg） | REQ-20：輸入一律經 schema 驗證（Phase 3：`app/transcription/schemas.py` 驗證 `start_recording` payload 與音訊 chunk 型別/長度）；逐字稿於前端一律以 `textContent` 插入、伺服器端 Jinja2 autoescape；REQ-32：使用者範本於強化沙箱中套用（禁 Python 內部物件、禁 `range`/`*`/`**`、輸出上限）；REQ-33：逐字稿以資料標籤包裝並防標籤跳脫，系統提示禁止執行其中指示，產出須人工複核；REQ-47：上傳檔案限副檔名白名單、以 PyAV 開啟確認含音訊串流，無法解碼即拒絕；解碼器（PyAV 內含 FFmpeg）需隨相依套件更新修補（見高風險控制點 4） |
| Repudiation（否認） | 使用者或系統否認曾執行錄音、寄信、產生會議記錄等操作 | REQ-19：所有敏感操作寫入不可竄改的 `audit_log`（含 actor、action、target、時間戳）；Phase 3 起錄音開始/結束（含時長、段數、結束原因）皆記錄 |
| Information Disclosure（資訊洩漏） | 資料庫外洩導致 refresh token / 逐字稿外流；原始錄音檔外洩；逐字稿送往第三方 LLM（RISK-09） | REQ-18：refresh token 以 Fernet 加密儲存；生產環境要求 `SESSION_COOKIE_SECURE`、HTTPS only；資料庫連線建議啟用 TLS（REQ-50：Supabase 預設 `sslmode=require`，正式環境低於 require 拒絕啟動）；REQ-51：Supabase 自動提供的 REST API（公開 anon key 即可呼叫）對所有資料表封鎖——RLS 全開不設 policy、撤銷 `anon`／`authenticated` 權限與預設權限；REQ-28：原始音訊不落地保存，僅於記憶體分段處理（啟用語者分離時暫存於系統暫存檔，分離完成即刪除）；逐字稿推播僅送至該會議主辦人的 SocketIO room（`watch_meeting` 加入 room 前檢查主辦人）；REQ-47：上傳的錄音檔僅存為私有暫存檔（`mkstemp`，檔名不含使用者輸入），轉錄結束（成功或失敗）即刪除，不保存原始檔名 |
| Denial of Service（阻斷服務） | 大量音訊 chunk 灌爆 WebSocket / Whisper 推論佇列；惡意範本耗盡 CPU/記憶體；重複觸發 LLM 產生 | REQ-26：單一 chunk 上限 32 KB、每連線 token-bucket 限速 64 KB/s（即時速率 2 倍）、單次錄音時長上限 4 小時（皆可由環境變數調整）；同一會議同時僅允許一個錄音連線；VAD/Whisper 在 eventlet `tpool` 執行緒執行，不阻塞其他連線；VAD 緩衝最長 15 秒，記憶體有上限；範本沙箱禁止資源耗盡語法（REQ-32）；同一會議同時只允許一個產生工作，LLM 呼叫於背景執行緒執行；REQ-47 上傳錄音檔：`MAX_CONTENT_LENGTH` 於解析表單前拒絕過大請求、寫檔時再以實際位元組數把關、長度上限與即時錄音相同（檔案資訊與實際解碼長度雙重檢查）、逐段解碼避免整檔載入記憶體、同一會議同時僅一個音訊來源 |
| Elevation of Privilege（權限提升） | 使用者存取非自己主辦的會議、逐字稿或範本；對他人會議錄音 | 所有 `meetings`/`minutes` 路由皆檢查 `organizer_id == current_user.id`（見 `app/meetings/routes.py`）；`start_recording` 同樣檢查主辦人身分（`app/transcription/__init__.py`）；寄送會議記錄、新增／移除與會者同樣限主辦人，移除時另檢查與會者屬於該會議 |
| Spoofing / Tampering（寄信） | 攻擊者竄改寄送請求加入收件人，把系統當垃圾郵件跳板；在會議標題或與會者名稱夾帶換行注入 `Bcc` 等郵件標頭；以系統共用帳號冒名寄信 | REQ-16：收件人只取自 `Participant` 資料表，寄送請求不接受收件人參數；信件以主辦人本人 OAuth 帳號寄出（無共用寄件帳號）；REQ-37：主旨與名稱移除控制字元、MIME 由 `email` 套件組成；收件人上限、確認對話框、重寄冷卻時間（RISK-10） |

## 高風險控制點（需持續監控）

1. **OAuth token 生命週期**：refresh token 僅存密文；存取權杖（access token）不落地，僅存於 process 記憶體快取（`token_service.py`），降低外洩曝險面。
2. **寄信收件人來源**：`REQ-16` 規定收件人必須來自 `Participant` 資料表，寄送請求不接受任意收件人輸入，避免被用作垃圾郵件跳板。
   主辦人可手動新增與會者（REQ-35），但信件以主辦人本人帳號寄出、每次異動與寄送皆稽核，風險等同主辦人自行寄信（RISK-10）。
3. **LLM Prompt Injection**：逐字稿為使用者不可完全控制的內容來源（可能被會議中的第三方注入誘導文字），Phase 4 設計 prompt 模板時，逐字稿內容以資料區塊（非指令）方式提供給 LLM，並要求人工複核後才寄出（REQ-14）。
4. **上傳錄音檔解碼（REQ-47）**：錄音檔由 PyAV（內含 FFmpeg）解析，屬處理不可信二進位輸入的攻擊面；僅限已登入主辦人上傳、副檔名白名單、大小與長度上限，並須定期更新 `av`／`faster-whisper` 相依套件以取得 FFmpeg 安全修補。
5. **Supabase REST API（REQ-51）**：Supabase 對 `public` schema 的新資料表預設開放給公開 API 角色。封鎖由 migration `5f2a9c1e7b40` 執行，並撤銷預設權限涵蓋日後 migration；若日後改在 Supabase 管理介面（Table Editor／SQL Editor，身分可能不是 `postgres`）手動建表，須自行確認 RLS 與權限，可用 Supabase「Security Advisor」檢查。

## 事件應變（Incident Response，摘要）

- 偵測：`audit_log` 異常模式（大量寄信、非常規時間登入）建議串接告警（後續維運項目，不在本次 MVP 範圍）。
- 圍堵：可透過撤銷 `OAuthAccount.refresh_token`（設為 NULL）強制使用者重新授權。
- 復原：所有敏感操作皆有稽核紀錄，可重建事件時間線。
