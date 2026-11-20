"""Chromium 页面交互层：所有外部交互都经 Playwright 驱动真实浏览器完成。

本层只负责“看见的页面事实”：
- 任务列表页 / 的报告状态（任务结束 ≠ 签发，只认页面上的状态文本）；
- 报告详情页 /report 的正式元数据、原件清单与页面公示 SHA256；
- /original 下载触发（不直接 HTTP 抓取，走浏览器下载链路）；
- 档案台 /archive 的文件选择控件上传与移交表单提交；
- /history 历史回执页，用于断线后的收件核实。

不包含任何业务裁决（能否引用、替代关系、联合成套判断在 engine 层），
也绝不访问 /__control 演练控制接口。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .errors import StructuralBrowserError, TransientBrowserError

_NAV_TIMEOUT_MS = 15_000
_UPLOAD_TIMEOUT_MS = 20_000
_TRANSIENT_HINTS = (
    "ERR_CONNECTION", "ERR_TIMED_OUT", "ERR_NETWORK", "ERR_INTERNET_DISCONNECTED",
    "ERR_PROXY", "ERR_SOCKET", "ERR_HTTP2_PROTOCOL", "ERR_EMPTY_RESPONSE",
    "ERR_CONNECTION_RESET", "ERR_TUNNEL",
)


def _is_transient(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, PlaywrightTimeout) or any(h in text for h in _TRANSIENT_HINTS)


def guard(exc: Exception, action: str):
    """把 Playwright 异常翻译成临时/结构性两类采集错误。"""
    if _is_transient(exc):
        raise TransientBrowserError(f"{action}时页面暂时不可用：{exc}") from exc
    raise StructuralBrowserError(f"{action}时页面结构或内容异常：{exc}") from exc


@dataclass(frozen=True)
class ListRow:
    report_id: str
    sample: str
    status: str


class LabBrowser:
    """一个浏览器上下文对应一次命令运行；用 with 管理生命周期。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self) -> "LabBrowser":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--no-sandbox"])
        self._context = self._browser.new_context(accept_downloads=True)
        self.page = self._context.new_page()
        self.page.set_default_timeout(_NAV_TIMEOUT_MS)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()

    # ---------- 只读页面 ----------

    def list_reports(self) -> list[ListRow]:
        """读取任务列表页表格，返回页面实际展示的报告号、样品、状态。"""
        try:
            self.page.goto(self.base_url + "/", wait_until="domcontentloaded")
            rows: list[ListRow] = []
            for tr in self.page.locator("table tr").all()[1:]:
                cells = tr.locator("td").all()
                if len(cells) < 3:
                    continue
                rows.append(ListRow(cells[0].inner_text().strip(),
                                    cells[1].inner_text().strip(),
                                    cells[2].inner_text().strip()))
            return rows
        except Exception as exc:  # noqa: BLE001 - 统一在 guard 内分类
            guard(exc, "读取任务列表页")

    def open_report(self, report_id: str) -> dict:
        """打开正式报告详情页，解析 <pre id=report> 中的签发元数据。"""
        try:
            self.page.goto(
                f"{self.base_url}/report?id={report_id}", wait_until="domcontentloaded")
            text = self.page.locator("#report").inner_text(timeout=_NAV_TIMEOUT_MS)
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            guard(exc, f"读取报告 {report_id} 详情页")
        if not isinstance(data, dict) or "id" not in data or "originals" not in data:
            raise StructuralBrowserError(f"报告 {report_id} 元数据缺少必要字段")
        return data

    def digest_map(self) -> dict[str, str]:
        """从当前报告页采集“原件展示名 -> 页面公示 SHA256”。"""
        out: dict[str, str] = {}
        try:
            anchors = self.page.locator("a.original").all()
            for anchor in anchors:
                href = anchor.get_attribute("href") or ""
                match = re.search(r"[?&]name=([^&]+)", href)
                if not match:
                    raise StructuralBrowserError("原件链接缺少 name 参数")
                paragraph = anchor.evaluate("el => el.parentElement.textContent") or ""
                digest_match = re.search(r"[0-9a-f]{64}", paragraph)
                if not digest_match:
                    raise StructuralBrowserError(f"原件 {match.group(1)} 缺少公示摘要")
                out[match.group(1)] = digest_match.group(0)
            return out
        except StructuralBrowserError:
            raise
        except Exception as exc:  # noqa: BLE001
            guard(exc, "读取原件摘要")

    def download_original(self, report_id: str, name: str, dest_dir: Path) -> Path:
        """通过点击报告详情页链接触发浏览器下载，保存到独立空目录 dest_dir。"""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        try:
            self.page.goto(
                f"{self.base_url}/report?id={report_id}", wait_until="domcontentloaded")
            with self.page.expect_download(timeout=_NAV_TIMEOUT_MS) as got:
                self.page.locator(f"a.original[href*='name={name}']").first.click()
            got.value.save_as(str(target))
        except Exception as exc:  # noqa: BLE001
            guard(exc, f"下载原件 {name}")
        if not target.exists() or target.stat().st_size == 0:
            raise TransientBrowserError(f"原件 {name} 下载结果为空，按临时失败处理")
        return target

    # ---------- 档案台页面 ----------

    def upload_original(self, file_path: Path, list_name: str) -> dict:
        """在档案台用文件选择控件上传一个原件，文件名按签发清单填写。"""
        try:
            self.page.goto(self.base_url + "/archive", wait_until="domcontentloaded")
            self.page.fill("#filename", list_name)
            self.page.set_input_files("#file", str(file_path))
            self.page.click("#upload")
            self.page.wait_for_function(
                "el => el.textContent.trim().length > 0",
                arg=self.page.locator("#upload-result").element_handle(),
                timeout=_UPLOAD_TIMEOUT_MS)
            return self._read_json_pre("#upload-result", f"上传原件 {list_name}")
        except Exception as exc:  # noqa: BLE001
            guard(exc, f"上传原件 {list_name}")

    def submit_archive(self, key: str, report_id: str, items: list[str]) -> dict:
        """填写移交引用 key、报告号、项目范围并提交；返回页面文本解析结果。

        lose_reply 场景下页面返回 503，返回体仍是 JSON（含 error 字段），
        由调用方据 /history 核实，不在此重试提交，避免产生第二份记录。
        """
        try:
            self.page.goto(self.base_url + "/archive", wait_until="domcontentloaded")
            self.page.fill("#key", key)
            self.page.fill("#report", report_id)
            self.page.fill("#items", ",".join(items))
            self.page.click("#submit")
            self.page.wait_for_function(
                "el => el.textContent.trim().length > 0",
                arg=self.page.locator("#result").element_handle(),
                timeout=_UPLOAD_TIMEOUT_MS)
            return self._read_json_pre("#result", f"提交移交 {key}")
        except Exception as exc:  # noqa: BLE001
            guard(exc, f"提交移交 {key}")

    def read_history(self) -> dict:
        """读取归档历史页展示的全部收件记录（同 key 只可能出现一份）。"""
        try:
            self.page.goto(self.base_url + "/history", wait_until="domcontentloaded")
            text = self.page.locator("#history").inner_text(timeout=_NAV_TIMEOUT_MS).strip()
            if not text:
                return {}
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            guard(exc, "读取归档历史页")
        if not isinstance(data, dict):
            raise StructuralBrowserError("归档历史页内容不是记录对象")
        return data

    # ---------- 内部 ----------

    def _read_json_pre(self, selector: str, action: str) -> dict:
        text = self.page.locator(selector).inner_text().strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StructuralBrowserError(f"{action}回执不是 JSON：{text[:120]}") from exc
        if not isinstance(data, dict):
            raise StructuralBrowserError(f"{action}回执结构异常")
        return data
