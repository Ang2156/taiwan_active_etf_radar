#!/usr/bin/env python3
"""Download official ETF holdings, keep snapshots, and calculate daily changes.

Output:
  data/holdings.json
  data/history/YYYY-MM-DD.json

Optional environment variable:
  HOLDINGS_SOURCES_JSON='[
    {
      "code": "00980A",
      "name": "主動野村臺灣優選",
      "issuer": "野村投信",
      "url": "https://example.com/holdings.csv",
      "format": "csv"
    }
  ]'

Supported formats are csv, json and html.  The downloader deliberately does not
turn a failed/empty response into holdings data, because doing so would make
every security look as if it had exited the fund.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
OUTPUT_FILE = DATA_DIR / "holdings.json"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/126 Safari/537.36 TaiwanActiveEtfRadar/1.0"
)


# These are official issuer product pages. Some issuers render holdings through
# JavaScript; when that happens, set HOLDINGS_SOURCES_JSON to the issuer's
# official CSV/JSON download endpoint without changing this program.
DEFAULT_SOURCES = [
    {
        "code": "00980A",
        "name": "主動野村臺灣優選",
        "issuer": "野村投信",
        "url": "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets",
        "format": "nomura_json",
        "method": "POST",
        "payload": {"FundID": "00980A", "SearchDate": None},
    },
    {
        "code": "00981A",
        "name": "主動統一台股增長",
        "issuer": "統一投信",
        "url": "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW",
        "format": "html",
        "browser": True,
        "click_texts": ["持股狀況", "投資組合", "持股"],
    },
    {
        "code": "00982A",
        "name": "主動群益台灣強棒",
        "issuer": "群益投信",
        "url": "https://www.capitalfund.com.tw/etf/product/detail/399/portfolio",
        "format": "capital_html",
    },
]


COLUMN_ALIASES = {
    "symbol": (
        "股票代號", "證券代號", "代號", "標的代號", "ticker", "symbol",
        "stock code", "security code",
    ),
    "name": (
        "股票名稱", "證券名稱", "名稱", "標的名稱", "name", "security name",
    ),
    "shares": (
        "股數", "持有股數", "持股數", "投資股數", "數量", "shares", "quantity",
    ),
    "weight": (
        "權重", "持股比重", "投資比例", "占基金淨資產價值比率", "比例",
        "weight", "ratio", "%",
    ),
    "industry": ("產業", "產業別", "industry", "sector"),
    "date": ("資料日期", "日期", "date", "as of"),
}


@dataclass
class Holding:
    symbol: str
    name: str
    shares: float
    weight: float | None = None
    industry: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "shares": number(self.shares),
            "weight": number(self.weight) if self.weight is not None else None,
            "industry": self.industry,
        }


class TableParser(HTMLParser):
    """Small dependency-free HTML table parser."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(clean_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


class CapitalPortfolioParser(HTMLParser):
    """Parse the server-rendered desktop holdings rows on Capital's ETF page."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.container_depth: int | None = None
        self.row_depth: int | None = None
        self.cell_depth: int | None = None
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "div":
            return
        self.depth += 1
        classes = set(dict(attrs).get("class", "").split())
        if {"pct-stock-table-tbody", "tbody"} <= classes:
            self.container_depth = self.depth
            return
        if self.container_depth is None:
            return
        if {"tr", "show-for-medium"} <= classes and self.row_depth is None:
            self.row_depth = self.depth
            self.row = []
            return
        if self.row_depth is not None and ({"th"} <= classes or {"td"} <= classes):
            self.cell_depth = self.depth
            self.cell_parts = []

    def handle_data(self, data: str) -> None:
        if self.cell_depth is not None:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "div":
            return
        if self.cell_depth == self.depth:
            self.row.append(clean_text("".join(self.cell_parts)))
            self.cell_depth = None
            self.cell_parts = []
        if self.row_depth == self.depth:
            if len(self.row) >= 4:
                self.rows.append(self.row[:4])
            self.row_depth = None
            self.row = []
        if self.container_depth == self.depth:
            self.container_depth = None
        self.depth -= 1


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def number(value: Any) -> int | float:
    value = float(value)
    return int(value) if value.is_integer() else round(value, 6)


def parse_number(value: Any, percent: bool = False) -> float | None:
    text = clean_text(value)
    if not text or text in {"-", "—", "N/A", "NA", "null", "None"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "").replace("%", "").replace("％", "")
    text = re.sub(r"[^\d.+\-Ee]", "", text)
    if not text:
        return None
    try:
        result = float(text)
        return -result if negative else result
    except ValueError:
        return None


def normalized(value: str) -> str:
    return re.sub(r"[\s()（）_\-/]", "", clean_text(value)).lower()


def find_column(headers: list[str], field: str) -> int | None:
    candidates = [normalized(alias) for alias in COLUMN_ALIASES[field]]
    for index, header in enumerate(headers):
        current = normalized(header)
        if any(alias == current or alias in current for alias in candidates):
            return index
    return None


def valid_symbol(value: str) -> bool:
    value = clean_text(value).upper()
    return bool(re.fullmatch(r"[A-Z0-9.\-]{2,20}", value)) and bool(
        re.search(r"\d", value)
    )


def rows_to_holdings(rows: list[list[Any]]) -> list[Holding]:
    if len(rows) < 2:
        return []
    best: list[Holding] = []
    for header_index in range(min(4, len(rows) - 1)):
        headers = [clean_text(value) for value in rows[header_index]]
        symbol_i = find_column(headers, "symbol")
        name_i = find_column(headers, "name")
        shares_i = find_column(headers, "shares")
        weight_i = find_column(headers, "weight")
        industry_i = find_column(headers, "industry")
        if symbol_i is None or shares_i is None:
            continue
        parsed: list[Holding] = []
        for row in rows[header_index + 1 :]:
            if max(symbol_i, shares_i) >= len(row):
                continue
            symbol = clean_text(row[symbol_i]).upper()
            shares = parse_number(row[shares_i])
            if not valid_symbol(symbol) or shares is None:
                continue
            name = clean_text(row[name_i]) if name_i is not None and name_i < len(row) else ""
            weight = (
                parse_number(row[weight_i], percent=True)
                if weight_i is not None and weight_i < len(row)
                else None
            )
            industry = (
                clean_text(row[industry_i])
                if industry_i is not None and industry_i < len(row)
                else ""
            )
            parsed.append(Holding(symbol, name, shares, weight, industry))
        if len(parsed) > len(best):
            best = parsed
    return best


def dict_rows_to_holdings(items: Iterable[dict[str, Any]]) -> list[Holding]:
    items = list(items)
    if not items:
        return []
    keys: list[str] = []
    for item in items[:10]:
        for key in item:
            if key not in keys:
                keys.append(key)
    symbol_key = key_for(keys, "symbol")
    shares_key = key_for(keys, "shares")
    name_key = key_for(keys, "name")
    weight_key = key_for(keys, "weight")
    industry_key = key_for(keys, "industry")
    if symbol_key is None or shares_key is None:
        return []
    result: list[Holding] = []
    for item in items:
        symbol = clean_text(item.get(symbol_key)).upper()
        shares = parse_number(item.get(shares_key))
        if not valid_symbol(symbol) or shares is None:
            continue
        result.append(
            Holding(
                symbol=symbol,
                name=clean_text(item.get(name_key)) if name_key else "",
                shares=shares,
                weight=parse_number(item.get(weight_key), True) if weight_key else None,
                industry=clean_text(item.get(industry_key)) if industry_key else "",
            )
        )
    return result


def key_for(keys: list[str], field: str) -> str | None:
    index = find_column(keys, field)
    return keys[index] if index is not None else None


def find_json_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            holdings = dict_rows_to_holdings(value)
            if holdings:
                return value
        for item in value:
            found = find_json_records(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = find_json_records(item)
            if found:
                return found
    return []


def decode_bytes(raw: bytes, charset: str | None = None) -> str:
    for encoding in (charset, "utf-8-sig", "utf-8", "big5", "cp950"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", errors="replace")


def download(source: dict[str, Any]) -> tuple[bytes, str]:
    payload = None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/csv,text/html,*/*",
        "Referer": source.get("referer", source["url"]),
    }
    if source.get("payload") is not None:
        payload = json.dumps(source["payload"], ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        source["url"],
        data=payload,
        headers=headers,
        method=clean_text(source.get("method", "GET")).upper(),
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read()
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset()
    if len(raw) < 20:
        raise ValueError("官方來源回傳內容為空")
    return raw, charset or content_type


def parse_source(raw: bytes, source: dict[str, Any], encoding_hint: str) -> list[Holding]:
    fmt = clean_text(source.get("format", "auto")).lower()
    text = decode_bytes(raw, source.get("encoding") or encoding_hint)
    if fmt == "auto":
        stripped = text.lstrip()
        fmt = "json" if stripped.startswith(("{", "[")) else (
            "html" if "<html" in stripped[:500].lower() else "csv"
        )
    if fmt == "nomura_json":
        data = json.loads(text)
        tables = (
            data.get("Entries", {})
            .get("Data", {})
            .get("Table", [])
        )
        candidates: list[list[Holding]] = []
        for table in tables:
            columns = [column.get("Name", "") for column in table.get("Columns", [])]
            rows = [columns, *table.get("Rows", [])]
            candidates.append(rows_to_holdings(rows))
            if table.get("NavDate") and not source.get("_detected_date"):
                source["_detected_date"] = str(table["NavDate"]).replace("/", "-")
        return max(candidates, key=len, default=[])
    if fmt == "json":
        data = json.loads(text)
        return dict_rows_to_holdings(find_json_records(data))
    if fmt == "csv":
        delimiter = "\t" if text.count("\t") > text.count(",") else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        return rows_to_holdings(rows)
    if fmt == "html":
        parser = TableParser()
        parser.feed(text)
        candidates = [rows_to_holdings(table) for table in parser.tables]
        return max(candidates, key=len, default=[])
    if fmt == "capital_html":
        parser = CapitalPortfolioParser()
        parser.feed(text)
        rows = [["股票代號", "股票名稱", "權重(%)", "股數"], *parser.rows]
        holdings = rows_to_holdings(rows)
        date_match = re.search(r"資料日期[：:\s]*(\d{4}[/-]\d{1,2}[/-]\d{1,2})", text)
        if date_match:
            source["_detected_date"] = date_match.group(1).replace("/", "-")
        return holdings
    raise ValueError(f"不支援的來源格式：{fmt}")


def download_with_browser(source: dict[str, Any]) -> list[Holding]:
    """Render a protected/dynamic official page and inspect its API responses."""

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "此來源需要 Playwright；請先執行 pip install playwright "
            "以及 playwright install chromium"
        ) from exc

    candidates: list[list[Holding]] = []
    response_errors: list[str] = []

    def inspect_response(response: Any) -> None:
        try:
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" in content_type or response.url.lower().endswith(".json"):
                data = response.json()
                records = find_json_records(data)
                holdings = dict_rows_to_holdings(records)
                if holdings:
                    candidates.append(holdings)
            elif "text/csv" in content_type or response.url.lower().endswith(".csv"):
                text = response.text()
                rows = list(csv.reader(io.StringIO(text)))
                holdings = rows_to_holdings(rows)
                if holdings:
                    candidates.append(holdings)
        except Exception as exc:  # A page may load many unrelated/streaming responses.
            response_errors.append(str(exc))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()
        page.on("response", inspect_response)
        try:
            page.goto(
                source["url"],
                wait_until="domcontentloaded",
                timeout=int(os.getenv("HOLDINGS_BROWSER_TIMEOUT", "90000")),
            )
            page.wait_for_timeout(4000)
            for text in source.get("click_texts", []):
                locator = page.get_by_text(text, exact=False)
                if locator.count():
                    try:
                        locator.first.click(timeout=5000)
                        page.wait_for_timeout(2500)
                    except PlaywrightTimeoutError:
                        pass
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            html = page.content()
            rendered_source = dict(source)
            rendered_source["format"] = "html"
            rendered = parse_source(html.encode("utf-8"), rendered_source, "utf-8")
            if rendered:
                candidates.append(rendered)
        finally:
            context.close()
            browser.close()

    if not candidates:
        detail = f"；已檢查 {len(response_errors)} 個無法解析的回應" if response_errors else ""
        raise ValueError(f"瀏覽器已載入頁面，但找不到完整持股資料{detail}")
    return max(candidates, key=len)


def fetch_holdings(source: dict[str, Any]) -> list[Holding]:
    """Use the official API/HTML first, then a real browser when configured."""

    direct_error: Exception | None = None
    try:
        raw, hint = download(source)
        holdings = parse_source(raw, source, hint)
        if holdings:
            return holdings
        direct_error = ValueError("官方來源沒有可辨識的持股資料")
    except Exception as exc:
        direct_error = exc

    if source.get("browser"):
        try:
            return download_with_browser(source)
        except Exception as browser_error:
            raise ValueError(
                f"直接下載失敗：{direct_error}；瀏覽器備援失敗：{browser_error}"
            ) from browser_error
    raise ValueError(str(direct_error))


def load_sources() -> list[dict[str, Any]]:
    value = os.getenv("HOLDINGS_SOURCES_JSON", "").strip()
    if not value:
        return DEFAULT_SOURCES
    sources = json.loads(value)
    if not isinstance(sources, list):
        raise ValueError("HOLDINGS_SOURCES_JSON 必須是 JSON 陣列")
    required = {"code", "name", "issuer", "url"}
    for source in sources:
        missing = required - set(source)
        if missing:
            raise ValueError(f"來源缺少欄位：{', '.join(sorted(missing))}")
    return sources


def snapshot_date(source: dict[str, Any], holdings: list[Holding]) -> str:
    configured = clean_text(source.get("_detected_date") or source.get("date"))
    if configured:
        return configured.replace("/", "-")
    return date.today().isoformat()


def read_previous(before: str) -> dict[str, Any] | None:
    for path in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        if path.stem < before:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    if OUTPUT_FILE.exists():
        try:
            value = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
            if clean_text(value.get("date")) < before:
                return value
        except (OSError, json.JSONDecodeError):
            pass
    return None


def holding_index(funds: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for fund in funds:
        for holding in fund.get("holdings", []):
            result[(fund["code"], holding["symbol"])] = holding
    return result


def calculate_changes(
    current_funds: list[dict[str, Any]], previous: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if previous is None:
        return []
    current = holding_index(current_funds)
    old = holding_index(previous.get("funds", []))
    changes: list[dict[str, Any]] = []
    for key in sorted(set(current) | set(old)):
        fund_code, symbol = key
        now = current.get(key)
        before = old.get(key)
        now_shares = float(now["shares"]) if now else 0.0
        old_shares = float(before["shares"]) if before else 0.0
        share_change = now_shares - old_shares
        now_weight = now.get("weight") if now else None
        old_weight = before.get("weight") if before else None
        weight_change = (
            float(now_weight) - float(old_weight)
            if now_weight is not None and old_weight is not None
            else None
        )
        if before is None:
            action = "新增"
        elif now is None:
            action = "退出"
        elif share_change > 0:
            action = "增持"
        elif share_change < 0:
            action = "減持"
        elif weight_change not in (None, 0):
            action = "權重調整"
        else:
            continue
        base = now or before or {}
        changes.append(
            {
                "fundCode": fund_code,
                "symbol": symbol,
                "name": base.get("name", ""),
                "industry": base.get("industry", ""),
                "action": action,
                "sharesBefore": number(old_shares),
                "sharesAfter": number(now_shares),
                "shareChange": number(share_change),
                "weightBefore": old_weight,
                "weightAfter": now_weight,
                "weightChange": number(weight_change) if weight_change is not None else None,
            }
        )
    priority = {"新增": 0, "增持": 1, "減持": 2, "退出": 3, "權重調整": 4}
    changes.sort(key=lambda row: (priority[row["action"]], -abs(row["shareChange"])))
    return changes


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    sources = load_sources()
    funds: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for index, source in enumerate(sources):
        code = clean_text(source["code"]).upper()
        try:
            holdings = fetch_holdings(source)
            funds.append(
                {
                    "code": code,
                    "name": clean_text(source["name"]),
                    "issuer": clean_text(source["issuer"]),
                    "source": source["url"],
                    "date": snapshot_date(source, holdings),
                    "holdingCount": len(holdings),
                    "holdings": [holding.as_dict() for holding in holdings],
                }
            )
            print(f"OK {code}: {len(holdings)} holdings")
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"code": code, "source": source["url"], "message": str(exc)})
            print(f"ERROR {code}: {exc}", file=sys.stderr)
        if index + 1 < len(sources):
            time.sleep(float(os.getenv("HOLDINGS_REQUEST_DELAY", "1")))

    if not funds:
        print("No valid official holdings were downloaded; output was not replaced.", file=sys.stderr)
        return 1

    data_date = max(
        (clean_text(fund.get("date")) for fund in funds if clean_text(fund.get("date"))),
        default=date.today().isoformat(),
    )
    previous = read_previous(data_date)
    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "date": data_date,
        "previousDate": previous.get("date") if previous else None,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fundCount": len(funds),
        "holdingCount": sum(fund["holdingCount"] for fund in funds),
        "funds": funds,
        "changes": calculate_changes(funds, previous),
        "errors": errors,
    }
    write_json(HISTORY_DIR / f"{data_date}.json", payload)
    write_json(OUTPUT_FILE, payload)
    print(
        f"Wrote {OUTPUT_FILE.relative_to(ROOT)}: "
        f"{len(funds)} funds, {len(payload['changes'])} changes, {len(errors)} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
