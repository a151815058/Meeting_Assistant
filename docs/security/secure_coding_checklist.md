# 安全編碼規範與 OWASP Top 10 對應

## 開發規範

- [x] 所有 DB 存取透過 SQLAlchemy ORM，不手寫字串拼接 SQL（避免 SQL Injection，OWASP A03）
- [x] 所有表單提交啟用 Flask-WTF CSRF 保護，且每個 POST 表單皆含 `csrf_token` 隱藏欄位（OWASP A01 / CSRF；2026-09-24 補上原缺漏的「新增會議」「同步與會者」表單，並以 CSRF 開啟狀態的測試防止回歸）
- [x] 開發模式登入（REQ-29）僅 `DevelopmentConfig` 啟用、僅限本機來源、正式環境誤開則拒絕啟動（OWASP A05 / A07）
- [x] 敏感欄位（OAuth refresh token）以 Fernet 加密儲存，金鑰來自環境變數，不入版控（OWASP A02 加密失效）
- [x] OAuth callback 驗證 `state` 參數，防止 CSRF 授權碼注入；Google 另使用 PKCE（`code_verifier` 存於 session）
- [x] OAuth callback 錯誤（取消授權、供應商錯誤、換 token 失敗）不回 500、不原樣顯示網址中的錯誤文字，並寫入稽核紀錄（REQ-38）
- [x] `OAUTHLIB_INSECURE_TRANSPORT`（允許 http callback）僅開發環境設定，正式環境設定則拒絕啟動（REQ-39，OWASP A02）
- [x] 所有 `meetings`/`minutes` 路由檢查資源所有權（`organizer_id == current_user.id`），避免 IDOR（OWASP A01）
- [x] Session cookie 設定 `HttpOnly`、`SameSite=Lax`；生產環境另加 `Secure`
- [x] `.env.example` 僅放假值；README 說明金鑰取得方式；真實金鑰不進版控
- [x] （Phase 3）WebSocket 外部輸入以 marshmallow schema / 型別長度檢查驗證（`app/transcription/schemas.py`；OWASP A03 注入 / A04 不安全設計）
- [x] （Phase 4）範本與會議記錄編輯內容以 marshmallow schema 驗證（`app/minutes/schemas.py`）
- [x] （Phase 3）SocketIO 僅允許同源連線（`cors_allowed_origins=None`；注意 `[]` 在 engineio 代表「不檢查 Origin」），`/transcription` 連線需登入，錄音需為會議主辦人（OWASP A01）
- [x] （Phase 3）WebSocket 音訊上傳限速、chunk 大小與錄音時長上限（OWASP A04 / DoS）
- [x] （Phase 3）原始音訊不落地保存；逐字稿於前端以 `textContent` 插入避免 XSS
- [x] （Phase 3）前端第三方 JS（Socket.IO client 4.8.1）改為自行託管於 `app/static/vendor/`，執行期不從外部 CDN 載入
- [x] （Phase 4）範本渲染使用強化的 Jinja2 sandbox（SSTI 防護 + 禁止 `range`/`*`/`**` 資源耗盡 + StrictUndefined + 輸出上限）
- [x] （Phase 4）逐字稿送 LLM 前以資料標籤包裝並防止標籤跳脫，系統提示禁止執行逐字稿中的指示（prompt injection 緩解）
- [x] （Phase 4）不使用行內 JS 事件處理器帶入使用者資料（`data-confirm` + `common.js`），避免屬性解碼後的 XSS
- [x] （Phase 5）寄信收件人一律從 `Participant` 資料表讀取，寄送請求不接受任何收件人參數；手動新增與會者為獨立、經 schema 驗證並寫入稽核紀錄的操作（REQ-16、REQ-35）
- [x] （Phase 5）信件以主辦人本人 OAuth 帳號寄出，需已授權 `gmail.send`／`Mail.Send`；無系統共用寄件帳號（REQ-16）
- [x] （Phase 5）郵件主旨與收件人名稱移除 CR/LF 等控制字元，MIME 以 Python `email` 套件組成，防止郵件標頭注入（REQ-37）
- [x] （Phase 5）寄送防濫用：收件人上限、確認對話框、重寄冷卻時間、同一會議同時僅一個寄送；連線錯誤不自動重試避免重複寄送（REQ-17、REQ-37）
- [x] （Phase 5）開發用本機信箱僅開發環境可啟用，正式環境誤開則拒絕啟動（REQ-36）

## OWASP Top 10 (2021) 對應

| OWASP 分類 | 本專案對應控制 |
|---|---|
| A01 權限控制失效 | 所有會議/範本/逐字稿路由檢查擁有者；`@login_required` 保護所有需登入頁面 |
| A02 加密機制失效 | refresh token 落地加密（Fernet）；生產環境強制 HTTPS + Secure cookie |
| A03 注入 | ORM 參數化查詢；外部輸入 schema 驗證；Jinja2 autoescape + 使用者範本沙箱（SSTI）；前端逐字稿以 `textContent` 渲染；LLM prompt injection 緩解（REQ-33） |
| A04 不安全設計 | SSDLC 流程、威脅建模（見 `threat_model.md`）、RTM 追蹤安全需求 |
| A05 安全設定缺陷 | `.env.example` 分離設定與程式碼；`ProductionConfig` 強制 `SESSION_COOKIE_SECURE` |
| A07 識別與驗證失效 | OAuth 2.0（Google/Microsoft）取代自建密碼系統，降低憑證外洩風險 |
| A08 軟體與資料完整性失效 | `requirements.txt` 鎖定版本；bandit SAST 掃描納入開發流程 |
| A09 安全記錄與監控失效 | `audit_log` 記錄所有敏感操作（OAuth 授權、錄音開始/結束、範本增修刪、會議記錄產生/失敗/編輯、寄信） |
| A10 SSRF | 外部 HTTP 呼叫僅限白名單供應商（Google/Microsoft/Anthropic 官方網域），不接受使用者提供任意 URL |

## 掃描工具與流程

- **SAST**：`bandit -r app -ll`（於每個 Phase 完成後執行；目前結果：0 個 medium/high 發現，2 個已審閱之 low false-positive 已加 `# nosec` 註記並保留原因）
- **依賴掃描**：建議 CI 加入 `pip-audit` 或 GitHub Dependabot（尚未設定，記錄於待辦）
- **單元/整合測試**：`pytest tests/ -v`，每個 Phase 完成後執行並確保全綠才可進入下一 Phase

## 待辦（Backlog，非 MVP 阻塞項）

- [x] 導入速率限制保護 WebSocket 音訊上傳（Phase 3，REQ-26）
- [ ] 登入端點速率限制
- [ ] CI pipeline 串接 bandit + pytest + pip-audit 自動化執行
- [ ] 生產環境 secrets 改用雲端 Secrets Manager（目前為環境變數）
