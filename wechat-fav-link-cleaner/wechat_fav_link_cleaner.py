#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信收藏失效链接检测与待删清单生成工具。

工作流程：
    1. 定位并只读打开本地微信收藏数据库（WeChat 3.x / 4.0）。
       若数据库被 SQLCipher 加密，需通过 --key 或环境变量 WECHAT_DB_KEY 提供密钥。
       如果无法读取数据库，可用 --import 从 CSV/JSON/HTML/TXT 导入链接。
    2. 从收藏内容中抽取 URL 并去重。
    3. 并发检测链接可达性，划分为 有效 / 确定失效 / 疑似失效。
    4. 交互确认后，生成「待删清单」（CSV + Markdown）。

重要说明：
    - 本工具**不会**修改微信数据库，也不会自动删除任何收藏。
    - 它只生成一份供你复核、并在微信客户端里手动删除的清单，避免误删。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover - 仅在缺少依赖时触发
    sys.stderr.write("缺少依赖 requests，请先执行: pip install -r requirements.txt\n")
    raise SystemExit(2)


LOG = logging.getLogger("wechat_fav_cleaner")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"'\\，。；：！？（）【】《》]+", re.IGNORECASE)
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 明确判定为失效的 HTTP 状态码
DEAD_STATUS = {404, 410}
# 需要人工复核的 HTTP 状态码（可能是临时故障、反爬或需登录）
SUSPECT_STATUS = {400, 401, 403, 405, 408, 429, 500, 502, 503, 504, 511}

STATUS_CN = {
    "ok": "有效",
    "dead": "确定失效",
    "suspect": "疑似失效",
}

# 常见微信数据目录（Windows / macOS）
WECHAT_ROOTS = [
    Path.home() / "Documents" / "WeChat Files",       # WeChat 3.x (Windows)
    Path.home() / "Documents" / "xwechat_files",      # WeChat 4.0 (Windows)
    Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat",  # macOS
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class LinkItem:
    url: str
    title: str = ""
    source: str = ""          # 数据来源，例如数据库表名或导入文件
    favorite_id: str = ""     # 收藏 id / rowid
    favorite_time: str = ""   # 收藏时间（若能从字段中识别）

    status: str = "pending"   # ok / dead / suspect / pending
    final_url: str = ""
    http_code: int | None = None
    reason: str = ""
    elapsed_ms: int = 0

    def confidence(self) -> str:
        return {"dead": "高", "suspect": "低", "ok": "-"}.get(self.status, "-")


# ---------------------------------------------------------------------------
# 数据库定位与读取
# ---------------------------------------------------------------------------

def discover_db_files(data_root: str | None = None) -> list[Path]:
    """自动发现本机可能的微信收藏数据库文件。"""
    roots: list[Path] = []
    env_root = os.environ.get("WECHAT_DATA_ROOT")
    if env_root:
        roots.append(Path(env_root).expanduser())
    if data_root:
        roots.append(Path(data_root).expanduser())
    roots.extend(WECHAT_ROOTS)

    found: dict[str, Path] = {}
    for root in roots:
        root = root.expanduser()
        if not root.exists():
            LOG.debug("数据目录不存在: %s", root)
            continue
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                low = p.name.lower()
                if ("favorite" in low or "favourite" in low or "collect" in low) and low.endswith(".db"):
                    found[str(p.resolve()).lower()] = p
        except OSError as exc:
            LOG.warning("扫描目录失败 %s: %s", root, exc)

    return sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)


def _sqlite_uri(path: Path) -> str:
    # 以只读模式打开，绝不修改微信数据库
    return "file:{}?mode=ro".format(quote(str(path.resolve()), safe="/\\:"))


def open_database(path: Path, key: str | None, cipher_pragma: list[str]) -> sqlite3.Connection:
    """只读打开数据库；先尝试明文 SQLite，失败后尝试 SQLCipher。"""
    uri = _sqlite_uri(path)

    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        LOG.info("以明文 SQLite 打开成功: %s", path)
        return conn
    except sqlite3.DatabaseError as exc:
        LOG.debug("明文打开失败 (%s)，尝试 SQLCipher", exc)
        try:
            conn.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass

    if not key:
        raise RuntimeError(
            f"数据库 {path} 已加密，未提供密钥。请使用 --key 或设置环境变量 WECHAT_DB_KEY；"
            "或先用 --import 从导出的文件导入链接。"
        )

    try:
        import sqlcipher3  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "数据库已加密但缺少 sqlcipher3。请安装: pip install sqlcipher3-binary "
            "（或使用 --import 从导出文件导入）。"
        ) from exc

    conn = sqlcipher3.connect(uri, uri=True, timeout=5)
    # 支持直接传入 x'<hex>' 形式或裸 hex
    key_value = key if key.lower().startswith("x'") else "x'" + key.strip('"\'') + "'"
    conn.execute(f'PRAGMA key = "{key_value}"')
    for pragma in cipher_pragma:
        conn.execute(pragma)
    try:
        conn.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"SQLCipher 打开失败，请检查 --key 与 --cipher-pragma: {exc}") from exc
    LOG.info("以 SQLCipher 打开成功: %s", path)
    return conn


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        for enc in ("utf-8", "utf-16-le", "gbk"):
            try:
                return value.decode(enc)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="ignore")
    return str(value)


def extract_urls(text: str) -> list[str]:
    """从任意文本中抽取并规范化 URL。"""
    if not text:
        return []
    decoded = html.unescape(text)
    urls: list[str] = []
    for match in URL_RE.findall(decoded):
        url = match.rstrip(".,;:!?)]}>\"'")
        try:
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                urls.append(url)
        except ValueError:
            continue
    return urls


def _pick_title(row: dict[str, Any]) -> str:
    preferred = ("title", "name", "subject", "desc", "description", "content", "摘要", "标题", "描述")
    for key, value in row.items():
        low = key.lower()
        text = _to_text(value).strip()
        if not text or text.startswith("http"):
            continue
        if any(word in low for word in preferred):
            return text[:120]
    for value in row.values():
        text = _to_text(value).strip()
        if text and not text.startswith("http") and 2 <= len(text) <= 120:
            return text
    return ""


def _pick_field(row: dict[str, Any], keywords: Iterable[str]) -> str:
    for key, value in row.items():
        low = key.lower()
        if any(word in low for word in keywords):
            text = _to_text(value).strip()
            if text:
                return text[:64]
    return ""


def extract_from_db(conn: sqlite3.Connection) -> list[LinkItem]:
    """遍历数据库所有表，抽取 URL（只读）。"""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        )
        if row[0] and not row[0].startswith("sqlite_")
    ]
    LOG.info("发现 %d 张表: %s", len(tables), ", ".join(tables[:20]))

    items: dict[str, LinkItem] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})")]
            has_rowid = True
            try:
                cursor = conn.execute(f"SELECT rowid, * FROM {quoted} LIMIT 1")
            except sqlite3.DatabaseError:
                has_rowid = False
                cursor = conn.execute(f"SELECT * FROM {quoted} LIMIT 1")
            select = f"SELECT rowid, * FROM {quoted}" if has_rowid else f"SELECT * FROM {quoted}"
            names = (["rowid"] if has_rowid else []) + cols
            count = 0
            for values in conn.execute(select):
                count += 1
                row = dict(zip(names, values))
                urls = []
                for value in values:
                    urls.extend(extract_urls(_to_text(value)))
                if not urls:
                    continue
                title = _pick_title(row)
                fid = _pick_field(row, ("id", "rowid", "favid", "收藏id"))
                ftime = _pick_field(row, ("time", "date", "createtime", "创建时间", "时间"))
                for url in urls:
                    if url not in items:
                        items[url] = LinkItem(
                            url=url,
                            title=title,
                            source=table,
                            favorite_id=fid,
                            favorite_time=ftime,
                        )
            LOG.debug("表 %s 扫描 %d 行", table, count)
        except sqlite3.DatabaseError as exc:
            LOG.warning("跳过表 %s: %s", table, exc)

    LOG.info("从数据库抽取到 %d 个唯一链接", len(items))
    return list(items.values())


# ---------------------------------------------------------------------------
# 从导出文件导入（数据库不可读时的备用方案）
# ---------------------------------------------------------------------------

def extract_from_file(path: Path) -> list[LinkItem]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    items: dict[str, LinkItem] = {}
    suffix = path.suffix.lower()

    def add(url: str, title: str = "") -> None:
        if url not in items:
            items[url] = LinkItem(url=url, title=title[:120], source=path.name)

    if suffix == ".csv":
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            title = ""
            for k, v in row.items():
                if k and any(w in k.lower() for w in ("title", "name", "标题", "desc")):
                    title = v or ""
                    break
            for k, v in row.items():
                if v:
                    for url in extract_urls(v):
                        add(url, title)
    elif suffix == ".json":
        def walk(node: Any, title: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (str, int, float)) and any(
                        w in str(k).lower() for w in ("title", "name", "标题", "desc")
                    ):
                        title = _to_text(v)
                    walk(v, title)
            elif isinstance(node, list):
                for child in node:
                    walk(child, title)
            elif isinstance(node, str):
                for url in extract_urls(node):
                    add(url, title)
        try:
            walk(json.loads(text))
        except json.JSONDecodeError as exc:
            LOG.warning("JSON 解析失败，按纯文本处理: %s", exc)
            for url in extract_urls(text):
                add(url)
    elif suffix in (".html", ".htm"):
        for href, inner in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", text, re.I | re.S):
            for url in extract_urls(html.unescape(href)):
                label = re.sub(r"<[^>]+>", "", inner).strip()
                add(url, label)
        for url in extract_urls(text):
            add(url)
    else:  # txt / md / 其他
        for url in extract_urls(text):
            add(url)

    LOG.info("从文件 %s 抽取到 %d 个唯一链接", path, len(items))
    return list(items.values())


# ---------------------------------------------------------------------------
# 链接检测
# ---------------------------------------------------------------------------

def classify(item: LinkItem, http_code: int | None, error_kind: str) -> None:
    if error_kind == "dns":
        item.status, item.reason = "dead", "域名无法解析"
    elif error_kind == "refused":
        item.status, item.reason = "dead", "连接被拒绝"
    elif error_kind == "redirect":
        item.status, item.reason = "dead", "重定向次数过多"
    elif error_kind == "timeout":
        item.status, item.reason = "suspect", "请求超时（可能已失效或屏蔽机器人）"
    elif error_kind == "ssl":
        item.status, item.reason = "suspect", "SSL 证书/握手异常"
    elif error_kind == "network":
        item.status, item.reason = "suspect", "网络错误"
    elif http_code in DEAD_STATUS:
        item.status, item.reason = "dead", f"HTTP {http_code}"
    elif http_code in SUSPECT_STATUS:
        item.status, item.reason = "suspect", f"HTTP {http_code}（可能需登录或反爬）"
    elif http_code is not None and 200 <= http_code < 400:
        item.status, item.reason = "ok", f"HTTP {http_code}"
    else:
        item.status, item.reason = "suspect", f"HTTP {http_code}"


def check_one(
    item: LinkItem,
    session: requests.Session,
    timeout: float,
    retries: int,
) -> LinkItem:
    started = datetime.now()
    headers = {"User-Agent": DEFAULT_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    last_error = ""
    last_code: int | None = None

    for attempt in range(retries + 1):
        try:
            resp = session.head(item.url, allow_redirects=True, timeout=timeout, headers=headers)
            last_code = resp.status_code
            item.final_url = resp.url or item.url
            # 有些站点 HEAD 返回 403/405，用 GET 再确认一次
            if last_code in (403, 405, 501):
                get_resp = session.get(
                    item.url, allow_redirects=True, timeout=timeout, headers=headers, stream=True
                )
                last_code = get_resp.status_code
                item.final_url = get_resp.url or item.url
                get_resp.close()
            break
        except requests.exceptions.SSLError as exc:
            last_error, last_code = "ssl", None
            LOG.debug("SSL 异常 %s: %s", item.url, exc)
            break
        except requests.exceptions.Timeout as exc:
            last_error = "timeout"
            LOG.debug("超时 %s: %s", item.url, exc)
            if attempt == retries:
                break
        except requests.exceptions.ConnectionError as exc:
            text = str(exc).lower()
            if any(k in text for k in ("name or service not known", "nodename nor servname", "getaddrinfo")):
                last_error = "dns"
            elif "refused" in text:
                last_error = "refused"
            else:
                last_error = "network"
            LOG.debug("连接错误 %s: %s", item.url, exc)
            if last_error in ("dns", "refused") or attempt == retries:
                break
        except requests.exceptions.TooManyRedirects:
            last_error, last_code = "redirect", None
            break
        except requests.exceptions.RequestException as exc:
            last_error = "network"
            LOG.debug("请求异常 %s: %s", item.url, exc)
            if attempt == retries:
                break

    classify(item, last_code, last_error)
    item.http_code = last_code
    item.elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)
    if not item.final_url:
        item.final_url = item.url
    return item


def check_all(
    items: list[LinkItem],
    workers: int,
    timeout: float,
    retries: int,
) -> list[LinkItem]:
    session = requests.Session()
    session.mount("http://", HTTPAdapter(pool_connections=workers, pool_maxsize=workers))
    session.mount("https://", HTTPAdapter(pool_connections=workers, pool_maxsize=workers))
    results: list[LinkItem] = []
    total = len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_one, item, session, timeout, retries): item for item in items}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                results.append(future.result())
            except Exception as exc:  # 兜底，避免单个链接拖垮整体
                item = futures[future]
                item.status, item.reason = "suspect", f"检测异常: {exc}"
                results.append(item)
            if done % 20 == 0 or done == total:
                LOG.info("检测进度: %d/%d", done, total)
    session.close()
    return results


# ---------------------------------------------------------------------------
# 报告与确认
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "序号", "标题", "链接", "最终链接", "判定", "置信度", "状态码",
    "失效原因", "耗时(ms)", "来源", "收藏ID", "收藏时间",
]


def _csv_row(index: int, item: LinkItem) -> dict[str, Any]:
    return {
        "序号": index,
        "标题": item.title,
        "链接": item.url,
        "最终链接": item.final_url,
        "判定": STATUS_CN.get(item.status, item.status),
        "置信度": item.confidence(),
        "状态码": item.http_code if item.http_code is not None else "",
        "失效原因": item.reason,
        "耗时(ms)": item.elapsed_ms,
        "来源": item.source,
        "收藏ID": item.favorite_id,
        "收藏时间": item.favorite_time,
    }


def write_csv(path: Path, items: list[LinkItem]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, item in enumerate(items, 1):
            writer.writerow(_csv_row(i, item))


def write_markdown(path: Path, items: list[LinkItem], generated_at: str) -> None:
    lines = [
        "# 微信收藏待删链接清单",
        "",
        f"- 生成时间：{generated_at}",
        f"- 待删条目：**{len(items)}** 条",
        "- 说明：本清单由工具自动生成，工具**不会**删除任何微信收藏。",
        "  请逐条复核后，在微信客户端「我 → 收藏」中手动删除。",
        "",
        "| 序号 | 判定 | 置信度 | 标题 | 链接 | 原因 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, item in enumerate(items, 1):
        title = item.title.replace("|", "\\|")[:60] or "-"
        reason = item.reason.replace("|", "\\|")
        lines.append(
            f"| {i} | {STATUS_CN.get(item.status, item.status)} | {item.confidence()} | "
            f"{title} | {item.url} | {reason} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def ask_confirmation(
    candidates: list[LinkItem], auto_yes: bool, ok_count: int = 0
) -> list[LinkItem]:
    """交互式确认，返回最终进入待删清单的条目。"""
    dead = [x for x in candidates if x.status == "dead"]
    suspect = [x for x in candidates if x.status == "suspect"]
    print("\n" + "=" * 64)
    print(f"检测完成：有效 {ok_count} 条，"
          f"确定失效 {len(dead)} 条，疑似失效 {len(suspect)} 条")
    print("=" * 64)
    if not candidates:
        return []
    print("\n候选待删链接：")
    for i, item in enumerate(candidates, 1):
        print(f"  [{i:>3}] [{STATUS_CN[item.status]}/{item.confidence()}] "
              f"{item.title[:34] or '(无标题)'}  {item.url}")
        print(f"       原因: {item.reason}")

    if auto_yes:
        print("\n已通过 --yes 自动确认全部候选项。")
        return candidates

    while True:
        answer = input(
            "\n确认加入待删清单？输入 y=全部确认，n=取消，或输入序号（逗号分隔，如 1,3,5）: "
        ).strip().lower()
        if answer in ("y", "yes", "是"):
            return candidates
        if answer in ("n", "no", "否", ""):
            return []
        try:
            indices = [int(x.strip()) for x in answer.split(",") if x.strip()]
            picked = [candidates[i - 1] for i in indices if 1 <= i <= len(candidates)]
            if picked:
                return picked
            print("序号无效，请重新输入。")
        except ValueError:
            print("输入格式不正确，请重新输入。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检测微信收藏中的失效链接，确认后生成待删清单（不会自动删除收藏）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python wechat_fav_link_cleaner.py --list-tables\n"
            "  python wechat_fav_link_cleaner.py --key <32位hex密钥>\n"
            "  python wechat_fav_link_cleaner.py --import favorites.csv\n"
        ),
    )
    parser.add_argument("--db-path", help="微信收藏数据库文件或所在目录（不指定则自动搜索）")
    parser.add_argument("--data-root", help="微信数据根目录，用于缩小自动搜索范围")
    parser.add_argument("--key", default=os.environ.get("WECHAT_DB_KEY"),
                        help="SQLCipher 密钥（也可用环境变量 WECHAT_DB_KEY）")
    parser.add_argument("--cipher-pragma", action="append", default=[],
                        help="额外的 SQLCipher PRAGMA，可重复传入，例如 'PRAGMA cipher_page_size=4096;'")
    parser.add_argument("--import", dest="import_file", help="从 CSV/JSON/HTML/TXT 导入链接（数据库不可读时备用）")
    parser.add_argument("--out", default="./wechat_fav_report", help="报告输出目录（默认 ./wechat_fav_report）")
    parser.add_argument("--workers", type=int, default=8, help="并发检测数（默认 8）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单链接超时秒数（默认 10）")
    parser.add_argument("--retries", type=int, default=1, help="超时/网络错误重试次数（默认 1）")
    parser.add_argument("--include-suspect", action="store_true", default=True,
                        help="将疑似失效链接也纳入候选（默认开启）")
    parser.add_argument("--only-dead", action="store_true", help="只把确定失效的链接纳入候选")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认，自动确认全部候选（谨慎使用）")
    parser.add_argument("--list-tables", action="store_true", help="仅列出数据库中的表，用于排查")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    conn: sqlite3.Connection | None = None
    try:
        if args.import_file:
            path = Path(args.import_file).expanduser()
            if not path.is_file():
                LOG.error("导入文件不存在: %s", path)
                return 2
            items = extract_from_file(path)
            source_desc = f"导入文件: {path}"
        else:
            if args.db_path:
                given = Path(args.db_path).expanduser()
                if given.is_dir():
                    candidates = discover_db_files(str(given))
                elif given.is_file():
                    candidates = [given]
                else:
                    LOG.error("路径不存在: %s", given)
                    return 2
            else:
                candidates = discover_db_files(args.data_root)

            if not candidates:
                LOG.error(
                    "未找到微信收藏数据库。请用 --db-path 指定路径，或用 --import 从导出文件导入。"
                )
                return 2

            LOG.info("找到 %d 个候选数据库，使用: %s", len(candidates), candidates[0])
            conn = open_database(candidates[0], args.key, args.cipher_pragma)
            if args.list_tables:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )]
                print("数据库中的表:")
                for t in tables:
                    print("  -", t)
                return 0
            items = extract_from_db(conn)
            source_desc = f"数据库: {candidates[0]}"

        if not items:
            LOG.warning("没有抽取到任何链接，退出。")
            return 0

        results = check_all(items, args.workers, args.timeout, args.retries)
        results.sort(key=lambda x: (x.status != "dead", x.status != "suspect", x.url))

        out_dir = Path(args.out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 全量结果与候选清单
        (out_dir / "scan_result.json").write_text(
            json.dumps(
                {"generated_at": generated_at, "source": source_desc,
                 "items": [asdict(x) for x in results]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        candidates = [x for x in results if x.status in ("dead", "suspect")]
        if args.only_dead:
            candidates = [x for x in candidates if x.status == "dead"]
        write_csv(out_dir / "candidates.csv", candidates)

        ok_count = sum(1 for x in results if x.status == "ok")
        picked = ask_confirmation(candidates, args.yes, ok_count=ok_count)
        if picked:
            write_csv(out_dir / "delete_list.csv", picked)
            write_markdown(out_dir / "delete_list.md", picked, generated_at)
            print(f"\n已生成待删清单（{len(picked)} 条）:")
            print(f"  {out_dir / 'delete_list.csv'}")
            print(f"  {out_dir / 'delete_list.md'}")
        else:
            print("\n未生成待删清单（已取消）。候选清单仍保存在:")
            print(f"  {out_dir / 'candidates.csv'}")

        print("\n提醒：工具不会自动删除微信收藏，请按清单在微信中手动删除并再次核对。")
        return 0

    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.warning("用户中断")
        return 130
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
