"""本地项目档案台账：记录领取依据、原件、移交回执、替代与撤签关系。

台账是客户端自己的工作记录，不是实验室签发结论；所有正式状态仍以浏览器
看到的门户页面为准。写入采用临时文件原子替换，避免运行中断产生半截记录。
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import LedgerError


def _now() -> str:
    # 单一可信时间源，测试可注入固定时钟。
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OriginalRecord:
    name: str                       # 报告清单中的原始 name（区分同名文件的依据）
    path: str                       # 本地保存路径（独立目录）
    sha256: str                     # 下载后本地实算摘要
    page_digest: str                # 报告页公示摘要
    uploaded: bool = False          # 是否已在档案台上传成功


@dataclass
class ClaimRecord:
    report_id: str
    revision: int
    sample: str
    configuration: str
    items: list[str]
    joint: bool
    status: str                     # signed / revoked / preview（页面原值）
    original_manifest: list[str] = field(default_factory=list)  # 报告清单原始 name
    originals: list[OriginalRecord] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)  # 临时下载失败原因
    claimed_at: str = ""
    updated_at: str = ""


@dataclass
class HandoffRecord:
    key: str
    report_id: str
    revision: int
    items: list[str]
    joint: bool
    accepted: dict[str, str] = field(default_factory=dict)   # item -> 归档号
    pending: list[str] = field(default_factory=list)
    state: str = "open"             # open / closed / suspended / superseded / superseded-partial
    submit_attempted: bool = False  # 是否已向档案台发起过提交
    receipt_confirmed: bool = False # 回执是否已经历史/响应确认（断线未确认时为 False）
    note: str = ""
    last_error: str = ""
    updated_at: str = ""


class Ledger:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path, request: dict[str, Any] | None = None,
                 clock=_now):
        self.path = Path(path)
        self.clock = clock
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != self.SCHEMA_VERSION:
                raise LedgerError(f"台账版本不受支持：{data.get('schema_version')}")
            self.request = data["request"]
            self.claims = [self._claim_from_dict(c) for c in data.get("claims", [])]
            self.handoffs = [HandoffRecord(**h) for h in data.get("handoffs", [])]
            self.events = data.get("events", [])
        else:
            if request is None:
                raise LedgerError("台账不存在且未提供 inputs/request.json 初始化数据")
            self._validate_request(request)
            self.request = request
            self.claims: list[ClaimRecord] = []
            self.handoffs: list[HandoffRecord] = []
            self.events: list[dict[str, str]] = []
            self.save()

    @staticmethod
    def _validate_request(request: dict[str, Any]) -> None:
        for key in ("authorized_samples", "project", "requested_items", "archive_reference"):
            if not request.get(key):
                raise LedgerError(f"输入缺少必要字段：{key}")
        if not isinstance(request["authorized_samples"], list):
            raise LedgerError("authorized_samples 必须是样品编号列表")

    # ---------- 持久化 ----------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "request": self.request,
            "claims": [self._claim_to_dict(c) for c in self.claims],
            "handoffs": [asdict(h) for h in self.handoffs],
            "events": self.events,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp",
                                        dir=str(self.path.parent))
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            Path(tmp_name).replace(self.path)
        finally:
            if Path(tmp_name).exists():
                Path(tmp_name).unlink()

    def log_event(self, kind: str, detail: str) -> None:
        self.events.append({"at": self.clock(), "kind": kind, "detail": detail})

    # ---------- 领取 ----------

    def find_claim(self, report_id: str) -> ClaimRecord | None:
        return next((c for c in self.claims if c.report_id == report_id), None)

    def upsert_claim(self, meta: dict, originals: list[OriginalRecord],
                     failures: list[str]) -> ClaimRecord:
        existing = self.find_claim(meta["id"])
        if existing is None:
            claim = ClaimRecord(
                report_id=meta["id"], revision=meta["revision"], sample=meta["sample"],
                configuration=meta["configuration"], items=list(meta["items"]),
                joint=bool(meta.get("joint")), status=meta["status"],
                original_manifest=list(meta["originals"]),
                originals=originals, supersedes=list(meta.get("supersedes", [])),
                failures=list(failures), claimed_at=self.clock(), updated_at=self.clock())
            self.claims.append(claim)
        else:
            existing.revision = meta["revision"]
            existing.sample = meta["sample"]
            existing.configuration = meta["configuration"]
            existing.items = list(meta["items"])
            existing.joint = bool(meta.get("joint"))
            existing.status = meta["status"]
            existing.original_manifest = list(meta["originals"])
            existing.supersedes = list(meta.get("supersedes", []))
            existing.failures = list(failures)
            # 保留已上传标记：按清单 name 合并，新原件覆盖文件路径与摘要。
            uploaded = {o.name: o.uploaded for o in existing.originals}
            for rec in originals:
                rec.uploaded = uploaded.get(rec.name, False)
            existing.originals = originals
            existing.updated_at = self.clock()
            claim = existing
        self.save()
        return claim

    # ---------- 移交 ----------

    def find_handoff(self, key: str) -> HandoffRecord | None:
        return next((h for h in self.handoffs if h.key == key), None)

    def upsert_handoff(self, key: str, report_id: str, revision: int,
                       items: list[str], joint: bool) -> HandoffRecord:
        handoff = self.find_handoff(key)
        if handoff is None:
            handoff = HandoffRecord(key=key, report_id=report_id, revision=revision,
                                    items=list(items), joint=joint,
                                    updated_at=self.clock())
            self.handoffs.append(handoff)
        handoff.updated_at = self.clock()
        self.save()
        return handoff

    def mark_uploaded(self, report_id: str, name: str) -> None:
        claim = self.find_claim(report_id)
        if claim is None:
            raise LedgerError(f"台账中没有报告 {report_id} 的领取记录")
        for original in claim.originals:
            if original.name == name:
                original.uploaded = True
        self.save()

    def original(self, report_id: str, name: str) -> OriginalRecord | None:
        claim = self.find_claim(report_id)
        if claim is None:
            return None
        return next((o for o in claim.originals if o.name == name), None)

    # ---------- 序列化 ----------

    @staticmethod
    def _claim_to_dict(claim: ClaimRecord) -> dict:
        data = asdict(claim)
        return data

    @staticmethod
    def _claim_from_dict(data: dict) -> ClaimRecord:
        originals = [OriginalRecord(**o) for o in data.pop("originals", [])]
        return ClaimRecord(originals=originals, **data)
