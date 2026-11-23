# 微信收藏失效链接清理助手

自动识别微信收藏中已失效的链接，经你确认后生成「待删清单」。

> **安全原则**：本工具以只读方式访问数据，**不会修改微信数据库，也不会自动删除任何收藏**。
> 它只生成一份供你复核的清单，实际删除请在微信客户端中手动完成，避免误删。

## 它能做什么

1. **定位数据**：自动搜索本机微信收藏数据库（WeChat 3.x / 4.0 常见路径），也可手动指定。
2. **抽取链接**：遍历数据库各表文本字段，提取并去重 `http/https` 链接。
3. **批量检测**：并发发起请求，把链接分为「有效 / 确定失效 / 疑似失效」。
   - 确定失效：HTTP 404/410、域名无法解析、连接被拒绝、重定向循环。
   - 疑似失效：超时、SSL 异常、403/401/429/5xx 等（可能是反爬或临时故障，需人工判断）。
4. **确认后出清单**：交互式逐项确认，最终输出 CSV + Markdown 待删清单。

## 安装

```bash
pip install -r requirements.txt
```

如果要直接读取**加密**的微信数据库，还需要 SQLCipher 驱动：

```bash
pip install sqlcipher3-binary
```

> 注：部分平台没有预编译包，可能需要本地编译环境。若安装失败，请使用下方的「导出文件导入」方式。

## 使用方式

### 方式 A：读取本地微信数据库

微信 3.x/4.0 的本地数据库是加密的，需要你自己提供密钥（本工具不负责提取密钥，也不会上传）：

```bash
# 先看数据库里有哪些表，排查用
python wechat_fav_link_cleaner.py --list-tables

# 指定密钥（hex 字符串）
python wechat_fav_link_cleaner.py --key 0123456789abcdef0123456789abcdef

# 通过环境变量提供密钥
set WECHAT_DB_KEY=0123456789abcdef0123456789abcdef
python wechat_fav_link_cleaner.py

# 数据库不在默认位置时手动指定
python wechat_fav_link_cleaner.py --db-path "D:\WeChat Files\wxid_xxx\favorite.db" --key <密钥>
```

可选：遇到打开失败时，按数据库实际加密参数补充 PRAGMA，例如：

```bash
python wechat_fav_link_cleaner.py --key <密钥> \
  --cipher-pragma "PRAGMA cipher_page_size=4096;" \
  --cipher-pragma "PRAGMA kdf_iter=4000;"
```

### 方式 B：从导出文件导入（数据库不可读时）

支持 `.csv` / `.json` / `.html` / `.txt` / `.md`。工具会扫描所有字段中的 URL。

```bash
python wechat_fav_link_cleaner.py --import examples/sample_favorites.csv
```

CSV 示例见 `examples/sample_favorites.csv`，包含 `标题,链接` 两列即可。

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `--out DIR` | 报告输出目录，默认 `./wechat_fav_report` |
| `--workers N` | 并发数，默认 8 |
| `--timeout SEC` | 单链接超时秒数，默认 10 |
| `--retries N` | 超时/网络错误重试次数，默认 1 |
| `--only-dead` | 只把「确定失效」纳入候选（默认包含疑似失效） |
| `--yes` | 跳过交互确认，自动确认全部候选（谨慎） |
| `--list-tables` | 只列出数据库表，不进行检测 |
| `-v` | 调试日志 |

## 输出文件

运行后在 `--out` 目录生成：

- `scan_result.json`：全部链接的检测明细。
- `candidates.csv`：所有非有效链接（待确认候选）。
- `delete_list.csv`：你确认后的待删清单（UTF-8 BOM，Excel 可直接打开）。
- `delete_list.md`：同样内容的 Markdown 清单，便于阅读。

确认时的交互：

```
确认加入待删清单？输入 y=全部确认，n=取消，或输入序号（逗号分隔，如 1,3,5）:
```

## 删除阶段（人工）

打开 `delete_list.md` 或 `delete_list.csv`，逐条核对后，在微信中：
`我 → 收藏 → 找到对应条目 → 长按/右键 → 删除`。

建议对「疑似失效」的链接先手动点开验证，再决定是否删除。

## 免责声明

- 请只在你有权访问的设备和账号上使用，遵守微信相关服务条款。
- 数据库密钥、收藏内容均为个人敏感信息，本工具全部在本地处理，不联网上传。
- 链接检测会向目标网站发起请求，属于正常的可用性探测；请合理控制 `--workers` 与频率。
