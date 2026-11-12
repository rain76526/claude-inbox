# Windows 安裝說明（給同事）

## 需要先有

- Claude Code（終端機版）
- Python 3.9 以上，且 `python` 或 `py -3` 在 PATH 裡。在 PowerShell 確認：

  ```powershell
  python --version
  py -3 --version
  ```

  兩個都找不到就先裝 Python（安裝時勾選「Add python.exe to PATH」）。

## 安裝

在 Claude Code 裡輸入：

```
/plugin marketplace add rain76526/claude-inbox
/plugin install inbox@claude-inbox
```

裝好後**整個重開** Claude Code，對 agent 說「跑 inbox url」，用瀏覽器開印出來的網址。

### 想寫進 settings.json 讓它自動裝

打開 `%USERPROFILE%\.claude\settings.json`，加上（已有 `extraKnownMarketplaces` / `enabledPlugins` 就合併進去）：

```json
{
  "extraKnownMarketplaces": {
    "claude-inbox": {
      "source": { "source": "github", "repo": "rain76526/claude-inbox" }
    }
  },
  "enabledPlugins": {
    "inbox@claude-inbox": true
  }
}
```

注意：

- `repo` 用簡寫 `rain76526/claude-inbox`，**不要**寫成 `https://github.com/...` 網址。
- **不要**照抄別人的 settings.json。別人的檔案裡可能是 `"source": "directory", "path": "D:\\..."`（指向他自己電腦的資料夾），在你的電腦上不存在。

## 常見錯誤

### `Cannot add marketplace "claude-inbox": its source doesn't match its extraKnownMarketplaces entry`

意思是：settings 裡已經登記了一個叫 `claude-inbox` 的 marketplace，但來源和你現在要加的不一樣。

1. 依序打開下面幾個檔案，搜尋 `claude-inbox`：
   - `%USERPROFILE%\.claude\settings.json`
   - 專案資料夾裡的 `.claude\settings.json`、`.claude\settings.local.json`
2. 找到的 `extraKnownMarketplaces.claude-inbox.source` 改成上面的 `{ "source": "github", "repo": "rain76526/claude-inbox" }`。
3. 重開 Claude Code，再跑一次 `/plugin marketplace add rain76526/claude-inbox`。
4. 上面的檔案都沒有、還是一樣錯：設定可能來自公司統一派送的 managed settings（`C:\Program Files\ClaudeCode\managed-settings.json`），你改不了，請找 IT。

暫時繞過（只對這一次啟動有效）：

```powershell
git clone https://github.com/rain76526/claude-inbox $env:USERPROFILE\claude-inbox
claude --plugin-dir $env:USERPROFILE\claude-inbox
```

### `inbox: Python 3.9+ not found`

`python` 和 `py -3` 都找不到。裝 Python 或把它加進 PATH，重開 Claude Code。

注意 Windows 內建的 `python` 可能是 Microsoft Store 的捷徑（執行會跳出商店），這種不算數。到「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」把 `python.exe`、`python3.exe` 關掉，再裝正式版 Python。

### 網頁打不開

先在 PowerShell 跑（路徑換成 plugin 實際位置，通常在 `%USERPROFILE%\.claude\plugins\cache\claude-inbox\inbox\<版本>\`）：

```powershell
& "$env:USERPROFILE\.claude\plugins\cache\claude-inbox\inbox\0.2.0\bin\inbox.cmd" start
& "$env:USERPROFILE\.claude\plugins\cache\claude-inbox\inbox\0.2.0\bin\inbox.cmd" url
```

- 網址一定要帶 `?t=...`（token）。只開 `http://localhost:7799/` 會看到「需要 token」。
- 7799 被別的程式占用：在 `%USERPROFILE%\.claude-inbox\config.json` 把 `"port"` 改成別的數字，跑 `inbox stop` 再 `inbox start`。
- 服務的錯誤記錄在 `%USERPROFILE%\.claude-inbox\server.log`。

## 還是不行，請提供這些資訊

1. 實際錯誤訊息全文（截圖或複製文字），以及是在哪一步出現的（安裝、重開、開網頁、agent 跑 `inbox ask`）。
2. `claude --version`
3. `python --version`、`py -3 --version`、`where.exe python` 的輸出
4. `/plugin` 畫面裡 inbox 的狀態（已安裝？已啟用？有沒有錯誤）
5. `%USERPROFILE%\.claude\settings.json` 裡 `extraKnownMarketplaces` 與 `enabledPlugins` 兩段（其他內容不用給）
6. 上面「網頁打不開」那兩行指令的輸出，以及瀏覽器開網址看到什麼
7. `%USERPROFILE%\.claude-inbox\server.log` 最後 30 行
8. `netstat -ano | findstr 7799` 的輸出
