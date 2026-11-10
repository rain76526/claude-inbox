# 進度看板 設計規格（草案，待使用者確認）

目標：多條工作線（多個 Claude Code session、多個專案）同時跑時，在收件匣同一個網頁一眼看到每條線的狀態、卡點和待回的題目。
看板只是「各線自己回報」加上「hook 自動記下最後活動時間」，不做排程、不替 agent 決定事情。

## 1. 資料：`board.json`

位置：`$CLAUDE_INBOX_HOME/board.json`（跟 `config.json`、`events.jsonl` 同一層）。

```json
{
  "version": 1,
  "lines": [
    {
      "id": "inbox-board",
      "session": "pm-inbox",
      "session_id": "3f2c…",
      "project": "D:\\AI-Company\\vendor\\claude-inbox",
      "path": "D:\\AI-Company\\vendor\\claude-inbox",
      "branch": "feature/board",
      "task": "進度看板",
      "status": "active",
      "progress": "規格寫好，等確認",
      "blocker": "",
      "next": "TDD 實作 board.json 讀寫",
      "updated_at": "2026-11-10T14:03:00+08:00",
      "last_active": "2026-11-10T14:05:12+08:00"
    }
  ]
}
```

| 欄位 | 說明 |
|---|---|
| `id` | 工作線代號，唯一鍵。英數、`-`、`_`、`.`，1–64 字。 |
| `session` | 顯示用的 session 名稱（例如 `pm-inbox`），自由文字。 |
| `session_id` | Claude Code 的 session UUID，`set` 時自動取 `CLAUDE_CODE_SESSION_ID`；hook 靠它對應 `last_active`。 |
| `project` | 專案根（`git rev-parse --show-toplevel`，跟題目的 `project` 同一套正規化）。用來對應未答題數。 |
| `path` | 實際工作路徑（worktree 可能跟專案根不同），預設目前資料夾。 |
| `branch` | 預設取目前 git 分支。 |
| `task` | 需求一句話。 |
| `status` | `active` 進行中／`blocked` 卡住／`review` 待驗收／`done` 完成／`idle` 閒置。CLI 也接受中文。 |
| `progress`／`blocker`／`next` | 進度一句話／卡點／下一步。 |
| `updated_at` | 最後一次 `inbox status set` 的時間（人為回報）。 |
| `last_active` | 最後一次 hook 觸發或 `set` 的時間（自動訊號）。 |

限制：每個文字欄位最多 500 字；最多 200 條線。超過就拒絕寫入並說明。

## 2. 寫入：`inbox status` 子指令

```
inbox status set <id> [--status 進行中] [--task …] [--progress …] [--blocker …] [--next …]
                      [--session 名稱] [--project 路徑] [--path 路徑] [--branch 分支]
inbox status list [--json] [--all]
inbox status remove <id>
```

- `set`：沒有這條線就新增（`project`／`path`／`branch`／`session_id` 由目前環境自動帶入）；已存在就只改有給的欄位。每次 `set` 更新 `updated_at` 與 `last_active`。`--blocker ''` 可清空卡點。
- `list`：依狀態分組印一行一條；`--json` 印原始資料給 agent／總 PM 讀；預設不列 `done`，`--all` 才列。
- `remove`：刪掉一條線。找不到就以非 0 結束。
- 寫入一律：`FileLock(board.lock)` → 重讀 → 修改 → `write_json`（暫存檔 + `replace_file` 原子取代、Windows 重試）。跟現有 `config.lock`／`events.lock` 同一套機制。
- `board.json` 壞掉（不是合法 JSON）時：`set`／`remove` 拒絕寫入並提示路徑，不覆蓋使用者的檔案；網頁顯示「看板資料讀不到」。

## 3. 自動訊號：hook 更新 `last_active`

- 現有 `inbox hook`（SessionStart、UserPromptSubmit）取完回覆後，對 `session_id` 相同的線更新 `last_active`。
- 只更新、不新增：沒用 `inbox status set` 登記過的 session 不會出現在看板上。
- 不能拖慢 hook：
  - 沒有 `board.json` 或沒有相符的線 → 直接結束，不拿鎖。
  - 距離上次 `last_active` 不到 60 秒 → 不寫（避免每次送訊息都重寫檔案）。
  - 拿 `board.lock` 最多等 2 秒，等不到就放棄這次更新；任何錯誤都吞掉，不影響回覆的遞送與 hook 的輸出。
  - 整體仍在 hook 的 10 秒 timeout 內。

## 4. 網頁：「看板」分頁

- 同一頁頂端加兩個分頁：「收件匣」（現有內容）、「看板」。選擇記在 `localStorage`（讀寫包 try/catch，讀不到就預設收件匣）。
- `/api/state` 多回一個 `board` 欄位（同一次輪詢，不另開端點）；每條線由服務端算好：
  - `open_questions`：未收起、尚未回答、`path_key(project)` 相同的題數。
  - `stale`：`last_active` 距今超過門檻（預設 120 分鐘，`config.json` 的 `board_stale_minutes` 可改）。`done`／`idle` 不標。
- 依狀態分五欄：進行中、卡住、待驗收、閒置、完成（完成欄預設收合）。窄螢幕改成上下堆疊。
- 卡片顯示：id、session、需求、分支、進度、卡點（有才顯示、醒目色）、下一步、最後活動「x 分鐘前」、未答題數徽章。
- 未答題數 > 0 時徽章可點：切到「收件匣」分頁，並只顯示該專案的題目（頂端出現可移除的「專案：xxx」篩選標籤）。
- `stale` 的卡片加「xx 小時沒動靜」標示並降低亮度。
- 網頁對看板只讀，沒有寫入 API。

## 5. 安全

- 沿用現有 `check()`：token、Host、Origin 檢查一樣套用在 `/api/state`。
- 看板所有文字都經過頁面現有的 `esc()` 後才放進 HTML，跟題目一樣，防 XSS；不提供任何 HTML／Markdown 渲染。
- `board.json` 與 `board.lock` 跟其他資料檔同樣放在權限 700 的資料夾內，`board.json` 由 `write_json` 建立（600）。

## 6. 跟 `pm-state.md` 的分工

- **看板以 `board.json` 為準**：各線狀態、進度、卡點、下一步只寫在這裡（工作線自己用 `inbox status set` 回報）。
- `pm-state.md` 只留決策紀錄（誰在何時決定了什麼、為什麼），不再抄寫各線狀態。
- 總 PM 要彙整時跑 `inbox status list --json`。
- 建議：既有回報格式「專案｜路徑｜分支｜需求｜狀態｜進度｜卡點｜下一步」的每一欄都對到 `board.json` 欄位，工作線回報時同時 `set` 一次。

## 7. 不做（這一版）

- 網頁上編輯／拖拉卡片。
- 歷史紀錄／時間軸。
- 自動判斷狀態（只有 `last_active` 是自動的）。

## 8. 實作順序（TDD，每塊一個 commit）

1. `board.json` 讀寫與驗證（FileLock、原子寫入、壞檔不覆蓋、欄位限制）。
2. `inbox status set/list/remove` CLI。
3. hook 更新 `last_active`（節流、限時拿鎖、錯誤吞掉）。
4. `/api/state` 加 `board`（`open_questions`、`stale`）。
5. 網頁看板分頁、專案篩選、stale 標示（含 XSS 測試：含 `<script>` 的欄位原樣顯示）。
6. README／SKILL.md 補用法。
