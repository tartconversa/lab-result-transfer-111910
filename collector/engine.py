"""采集业务引擎：把页面事实翻译成可引用依据、待办与移交动作。

规则与 docs/source-contract.md 一一对应：
1. 只认页面状态：preview 不可引用，revoked 暂停对外引用；任务结束不等于签发。
2. 授权样品之外的报告一律不领取。
3. 配置 = 样品 + 卡片/驱动/固件；同一样品不同配置独立，原件以报告清单的
   原始 name 为准，不用“最新同名文件”补齐。
4. joint 联合结论只能成套移交，绝不拆成看似完整的单项。
5. 可分批签发：独立项目缺件不阻塞已签发项目；缺一个项目只在待办里挂缺。
6. 复测 supersedes 建立替代关系，旧档案保留不删；未被提及的项目继续有效。
7. 撤签只暂停该报告支撑的引用；仍有独立有效证明的项目继续可用。
8. 临时下载失败与撤签分开记账，失败可重试，不改写任何签发状态。
9. 移交按 key 幂等：提交后先查 /history 核实，绝不重复上传形成两份记录；
   补齐只针对缺项，使用独立的补齐 key。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser import LabBrowser
from .errors import TransientBrowserError
from .ledger import ClaimRecord, HandoffRecord, Ledger, OriginalRecord

# 档案台 /archive 与 /history 中出现的拒绝码语义。
_REJECT_CODES = {
    "not_signed": "报告未签发，暂不能移交",
    "joint_scope": "联合项目必须成套移交，不能拆分",
    "missing_original": "档案台缺少本次报告清单内原件",
    "digest_mismatch": "上传摘要与档案台核算不一致",
}
_GATEWAY_AFTER_WRITE = "gateway_after_write"


@dataclass
class TransferPlan:
    key: str
    report_id: str
    revision: int
    items: list[str]
    joint: bool
    reason: str


class CollectorEngine:
    def __init__(self, ledger: Ledger, browser: LabBrowser, download_root: Path):
        self.ledger = ledger
        self.browser = browser
        self.download_root = Path(download_root)

    # ================= 领取 =================

    def claim(self) -> dict[str, Any]:
        """领取授权样品的全部正式依据并下载报告清单内原件。

        每个报告使用独立下载目录；返回本次领取摘要。预览与撤签均不下载原件，
        撤签只做状态对账（保留已下载的旧文件，不删除）。
        """
        rows = self.browser.list_reports()
        authorized = set(self.ledger.request["authorized_samples"])
        summary = {"claimed": [], "skipped_preview": [], "outside_authorization": [],
                   "reconciled_revoked": [], "transient_failures": []}
        for row in rows:
            if row.sample not in authorized:
                summary["outside_authorization"].append(row.report_id)
                self.ledger.log_event("claim.skip-auth",
                                      f"报告 {row.report_id} 样品 {row.sample} 不在授权范围")
                continue
            meta = self.browser.open_report(row.report_id)
            # 列表页与详情页状态以详情页正式元数据为准，并做一致性校验。
            if meta.get("status") != row.status:
                self.ledger.log_event(
                    "claim.status-mismatch",
                    f"报告 {row.report_id} 列表={row.status} 详情={meta.get('status')}，以详情页为准")
            if meta["status"] == "preview":
                self.ledger.upsert_claim(meta, [], [])
                summary["skipped_preview"].append(row.report_id)
                self.ledger.log_event("claim.preview",
                                      f"报告 {row.report_id} 仅为预览，不可对外引用，不下载原件")
                continue
            if meta["status"] == "revoked":
                self.ledger.upsert_claim(meta, self._preserve_originals(meta), [])
                self._apply_revocation(meta)
                summary["reconciled_revoked"].append(row.report_id)
                continue
            if meta["status"] != "signed":
                self.ledger.log_event("claim.unknown-status",
                                      f"报告 {row.report_id} 状态 {meta['status']} 暂不处理")
                continue
            originals, failures = self._collect_originals(meta)
            self.ledger.upsert_claim(meta, originals, [str(f) for f in failures])
            self._apply_supersession(meta)
            summary["claimed"].append(row.report_id)
            for failure in failures:
                summary["transient_failures"].append(f"{row.report_id}: {failure}")
        return summary

    def _preserve_originals(self, meta: dict) -> list[OriginalRecord]:
        """撤签对账时保留已下载原件记录，不重新下载、不删除旧档案。"""
        existing = self.ledger.find_claim(meta["id"])
        return existing.originals if existing else []

    def _collect_originals(self, meta: dict) -> tuple[list[OriginalRecord], list[Exception]]:
        """严格按报告 originals 清单下载；临时失败逐项记录，不拿同名文件顶替。"""
        self.browser.open_report(meta["id"])
        digests = self.browser.digest_map()
        records: list[OriginalRecord] = []
        failures: list[Exception] = []
        # 每份报告一个独立空目录，防止不同报告同名原件互相污染。
        dest_dir = self.download_root / f"{meta['id']}-rev{meta['revision']}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in meta["originals"]:
            page_digest = digests.get(name, "")
            target = dest_dir / name
            try:
                if target.exists() and self._sha256(target) == page_digest:
                    path = target  # 重跑时复用已核实文件，不重复下载
                else:
                    path = self.browser.download_original(
                        meta["id"], name, dest_dir)
                actual = self._sha256(path)
                if not page_digest:
                    raise TransientBrowserError(f"报告页未公示 {name} 的摘要")
                if actual != page_digest:
                    raise TransientBrowserError(
                        f"{name} 本地摘要 {actual} 与页面公示 {page_digest} 不一致")
                records.append(OriginalRecord(name=name, path=str(path),
                                              sha256=actual, page_digest=page_digest))
            except TransientBrowserError as exc:
                failures.append(exc)
                self.ledger.log_event("download.failed", f"{meta['id']}/{name}: {exc}")
        return records, failures

    # ================= 替代与撤签 =================

    def _apply_supersession(self, meta: dict) -> None:
        """复测报告按 supersedes 建立替代关系；未提及项目保持有效，不删旧档案。"""
        for marker in meta.get("supersedes", []):
            old_report_id, item = marker.split(":", 1)
            old_claim = self.ledger.find_claim(old_report_id)
            if old_claim is None:
                self.ledger.log_event("supersede.unknown-base",
                                      f"{meta['id']} 声明替代 {marker}，但本地无 {old_report_id} 领取记录")
                continue
            for handoff in self.ledger.handoffs:
                if handoff.report_id != old_report_id or item not in handoff.items:
                    continue
                if item in handoff.accepted:
                    handoff.state = ("superseded-partial"
                                     if any(i in handoff.accepted and i != item
                                            for i in handoff.items) else "superseded")
                    handoff.note = (f"项目 {item} 已被 {meta['id']} 替代，"
                                    f"原归档号 {handoff.accepted[item]} 保留备查；"
                                    + ("其余项目继续有效" if handoff.state == "superseded-partial"
                                       else "本移交整体被替代"))
                    handoff.updated_at = self.ledger.clock()
            self.ledger.log_event(
                "supersede.linked",
                f"{meta['id']}（配置 {meta['configuration']}）替代 {old_report_id}:{item}，"
                "旧档案保留，未提及项目不作废")
        self.ledger.save()

    def _apply_revocation(self, meta: dict) -> None:
        """撤签暂停该报告支撑的对外引用；有其他独立有效证明的项目仍可用。"""
        for handoff in self.ledger.handoffs:
            if handoff.report_id != meta["id"] or handoff.state in ("superseded",):
                continue
            handoff.state = "suspended"
            handoff.note = f"报告 {meta['id']} 已撤签，其支撑的对外引用暂停，等待新的有效证明"
            handoff.updated_at = self.ledger.clock()
            self.ledger.log_event("revoke.suspended", f"移交 {handoff.key} 引用随 {meta['id']} 暂停")
        self.ledger.save()

    # ================= 移交规划 =================

    def plan_transfers(self, only_report: str | None = None) -> list[TransferPlan]:
        """依据当前台账生成可执行移交计划；联合结论成套、独立项目可分批。"""
        plans: list[TransferPlan] = []
        requested = set(self.ledger.request["requested_items"])
        for claim in self.ledger.claims:
            if only_report and claim.report_id != only_report:
                continue
            if claim.status != "signed":
                continue
            scope = [i for i in claim.items if i in requested] if not only_report else list(claim.items)
            if not scope:
                continue
            if claim.joint and set(scope) != set(claim.items):
                # 联合报告只覆盖请求中的部分项目时，仍必须按报告全量成套处理；
                # 若成套原件不齐则整体挂待办，而不是拆单。
                scope = list(claim.items)
            if self._missing_expected(claim) or claim.failures:
                continue
            key = self._unit_key(claim, scope, supplement=False)
            plans.append(TransferPlan(key, claim.report_id, claim.revision,
                                      scope, claim.joint,
                                      "联合成套移交" if claim.joint else "独立项目移交"))
        return plans

    def plan_supplements(self) -> list[TransferPlan]:
        """为已有移交中的 pending 缺项生成补齐计划（独立 key，只补缺项）。"""
        plans: list[TransferPlan] = []
        for handoff in self.ledger.handoffs:
            if handoff.state == "suspended":
                continue
            # 断线未确认的单元只能等历史核实，不能另起新 key 绕开幂等保护；
            # 已确认收件但仍有 pending 的单元才允许生成补齐计划。
            if handoff.submit_attempted and not handoff.receipt_confirmed:
                continue
            missing = [i for i in handoff.items if i not in handoff.accepted]
            if not missing:
                continue
            claim = self.ledger.find_claim(handoff.report_id)
            if claim is None or claim.status != "signed":
                continue
            scope = list(claim.items) if claim.joint else missing
            key = self._unit_key(claim, scope, supplement=True)
            plans.append(TransferPlan(key, claim.report_id, claim.revision,
                                      scope, claim.joint,
                                      "联合缺件成套补齐" if claim.joint else "缺项补齐"))
        return plans

    def _unit_key(self, claim: ClaimRecord, scope: list[str], supplement: bool) -> str:
        base = self.ledger.request["archive_reference"]
        if not supplement:
            return f"{base}:{claim.report_id}"
        return f"{base}:{claim.report_id}:sup:" + "+".join(scope)

    # ================= 移交执行 =================

    def transfer(self, only_report: str | None = None) -> list[HandoffRecord]:
        """执行全部可移交计划与缺项补齐；同 key 全程幂等。"""
        plans = self.plan_transfers(only_report) + self.plan_supplements()
        # 去重：同一 key 只执行一次（计划与补齐可能覆盖同一独立缺项场景）。
        seen: set[str] = set()
        results: list[HandoffRecord] = []
        for plan in plans:
            if plan.key in seen:
                continue
            seen.add(plan.key)
            results.append(self._execute_plan(plan))
        return results

    def _safe_history(self) -> dict:
        """历史页临时不可达时返回空集，调用方按“暂不可见、稍后核实”处理。"""
        try:
            return self.browser.read_history()
        except TransientBrowserError as exc:
            self.ledger.log_event("history.unavailable", f"归档历史暂时不可达：{exc}")
            return {}

    def _execute_plan(self, plan: TransferPlan) -> HandoffRecord:
        claim = self.ledger.find_claim(plan.report_id)
        handoff = self.ledger.upsert_handoff(
            plan.key, plan.report_id, plan.revision, plan.items, plan.joint)

        # 0) 已提交但回执未确认（断连/503 后）：只查历史，绝不重复提交或另起补齐。
        if handoff.submit_attempted and not handoff.receipt_confirmed:
            history = self._safe_history()
            if plan.key in history:
                self._reconcile_receipt(handoff, history[plan.key],
                                        source="history-resume")
                return handoff
            handoff.last_error = "上次提交结果未确认，历史暂不可见，保持待办不重复提交"
            handoff.updated_at = self.ledger.clock()
            self.ledger.save()
            self.ledger.log_event("transfer.unconfirmed",
                                  f"{plan.key} 已提交但历史暂无记录，等待下次核实")
            return handoff

        # 1) 任何提交动作之前先查历史：同 key 已收件则直接对账，绝不重复提交。
        history = self._safe_history()
        if plan.key in history:
            self._reconcile_receipt(handoff, history[plan.key], source="history-before-submit")
            return handoff

        # 2) 按报告清单上传尚缺原件；上传成功过的不重复上传。
        for original in claim.originals:
            if original.uploaded:
                continue
            if not original.path or not Path(original.path).exists():
                handoff.last_error = f"本地缺少原件 {original.name}，请重新领取"
                handoff.updated_at = self.ledger.clock()
                self.ledger.save()
                self.ledger.log_event("transfer.missing-local",
                                      f"{plan.report_id}/{original.name} 本地文件缺失")
                return handoff
            reply = self.browser.upload_original(Path(original.path), original.name)
            if "error" in reply:
                handoff.last_error = f"{original.name}: {self._explain(reply)}"
                handoff.updated_at = self.ledger.clock()
                self.ledger.save()
                self.ledger.log_event("upload.rejected",
                                      f"{plan.key}/{original.name}: {reply}")
                return handoff
            if reply.get("sha256") != original.sha256:
                handoff.last_error = f"{original.name} 回执摘要与本地台账不一致"
                handoff.updated_at = self.ledger.clock()
                self.ledger.save()
                return handoff
            self.ledger.mark_uploaded(plan.report_id, original.name)

        # 3) 提交移交。标记落盘后，任何断连/503 都先查历史再决定，禁止盲重交。
        handoff.submit_attempted = True
        self.ledger.save()
        try:
            reply = self.browser.submit_archive(plan.key, plan.report_id, plan.items)
        except TransientBrowserError as exc:
            self.ledger.log_event(
                "transfer.connection-lost",
                f"{plan.key} 提交后连接断开（{exc}），改走归档历史核实")
            history = self._safe_history()
            if plan.key in history:
                self._reconcile_receipt(handoff, history[plan.key],
                                        source="history-after-disconnect")
                return handoff
            handoff.last_error = "提交后连接断开且历史暂不可见，等待下次运行核实"
            handoff.updated_at = self.ledger.clock()
            self.ledger.save()
            return handoff
        if "error" in reply:
            if reply["error"] == _GATEWAY_AFTER_WRITE:
                self.ledger.log_event(
                    "transfer.gateway-after-write",
                    f"{plan.key} 页面返回写入后 503，改走归档历史核实，不重复提交")
                history = self._safe_history()
                if plan.key in history:
                    self._reconcile_receipt(handoff, history[plan.key],
                                            source="history-after-503")
                    return handoff
                handoff.last_error = "写入后断连且历史暂不可见，等待下次运行核实"
            else:
                handoff.last_error = self._explain(reply)
                self.ledger.log_event("transfer.rejected", f"{plan.key}: {reply}")
            handoff.updated_at = self.ledger.clock()
            self.ledger.save()
            return handoff

        self._reconcile_receipt(handoff, reply, source="submit-reply")
        return handoff

    def reconcile(self) -> list[HandoffRecord]:
        """断线恢复：只用 /history 页面对全部本地移交做收件核实，不发起新提交。"""
        history = self._safe_history()
        touched: list[HandoffRecord] = []
        for handoff in self.ledger.handoffs:
            if handoff.key in history:
                self._reconcile_receipt(handoff, history[handoff.key],
                                        source="history-reconcile")
                touched.append(handoff)
            elif handoff.state == "open" and handoff.last_error:
                # 历史仍无记录：保持待办，绝不用重复提交制造第二份记录。
                self.ledger.log_event("reconcile.still-missing",
                                      f"{handoff.key} 历史中暂无收件记录，保持待办")
        return touched

    def _reconcile_receipt(self, handoff: HandoffRecord, receipt: dict, source: str) -> None:
        handoff.accepted = dict(receipt.get("accepted", {}))
        handoff.pending = list(receipt.get("pending", []))
        handoff.revision = receipt.get("revision", handoff.revision)
        handoff.last_error = ""
        # 回执已经页面确认；之后的重放走历史短路，缺项则走独立补齐单元。
        handoff.receipt_confirmed = True
        if handoff.state not in ("suspended", "superseded", "superseded-partial"):
            handoff.state = "closed" if not handoff.pending else "open"
        handoff.updated_at = self.ledger.clock()
        self.ledger.save()
        self.ledger.log_event(
            "receipt.reconciled",
            f"{handoff.key} 经 {source} 核实：accepted={sorted(handoff.accepted)} "
            f"pending={handoff.pending}")

    # ================= 查询视图 =================

    def todo_view(self) -> dict[str, Any]:
        """待办核对：每个请求项目当前依据、缺件、预览/撤签/替代状态。"""
        requested = list(self.ledger.request["requested_items"])
        lines: dict[str, Any] = {}
        for item in requested:
            providers = self._providers_for(item)
            entry = {"item": item, "citable": [], "blocked": [], "notes": []}
            for claim in providers:
                missing = list(self._missing_expected(claim))
                if claim.status == "preview":
                    entry["blocked"].append(self._claim_ref(
                        claim, "仅为临时预览，非正式签发，不可作为对外交付证明"))
                elif claim.status == "revoked":
                    entry["blocked"].append(self._claim_ref(claim, "报告已撤签，引用暂停"))
                elif claim.failures:
                    entry["blocked"].append(self._claim_ref(
                        claim, "原件临时下载失败（可重试，非撤签）：" + "; ".join(claim.failures)))
                elif missing:
                    entry["blocked"].append(self._claim_ref(
                        claim, "缺原件：" + ", ".join(missing)))
                else:
                    entry["citable"].append(self._claim_ref(claim, ""))
            if not providers:
                entry["notes"].append("本地暂无覆盖该项目的正式签发报告")
            lines[item] = entry
        # 联合报告覆盖但请求中未逐项列出的项目，也展示成套约束。
        for claim in self.ledger.claims:
            if claim.joint:
                extra = [i for i in claim.items if i not in requested]
                if extra:
                    lines.setdefault("joint:" + claim.report_id, {
                        "item": "、".join(claim.items),
                        "citable": [], "blocked": [],
                        "notes": [f"联合报告 {claim.report_id} 须成套引用，覆盖项目 "
                                  f"{'、'.join(claim.items)}（含请求外 {'、'.join(extra)}）"]})
        return {"project": self.ledger.request["project"],
                "requested_items": requested, "items": lines}

    def handoffs_view(self) -> dict[str, Any]:
        """移交查询：逐项归档号、pending、暂停与被替代的原归档。"""
        out = []
        for handoff in self.ledger.handoffs:
            out.append({
                "key": handoff.key, "report": handoff.report_id,
                "revision": handoff.revision, "items": handoff.items,
                "joint": handoff.joint, "state": handoff.state,
                "accepted": handoff.accepted, "pending": handoff.pending,
                "note": handoff.note, "last_error": handoff.last_error,
                "updated_at": handoff.updated_at,
            })
        return {"project": self.ledger.request["project"],
                "archive_reference": self.ledger.request["archive_reference"],
                "handoffs": out}

    def claims_view(self) -> dict[str, Any]:
        """领取查询：样品/配置归属、报告状态、原件清单与摘要。"""
        return {"project": self.ledger.request["project"],
                "authorized_samples": self.ledger.request["authorized_samples"],
                "claims": [{
                    "report": c.report_id, "revision": c.revision, "sample": c.sample,
                    "configuration": c.configuration, "items": c.items,
                    "joint": c.joint, "status": c.status,
                    "supersedes": c.supersedes, "manifest": list(c.original_manifest),
                    "originals": [{"name": o.name, "sha256": o.sha256,
                                   "uploaded": o.uploaded, "path": o.path}
                                  for o in c.originals],
                    "failures": c.failures,
                } for c in self.ledger.claims]}

    # ---------- 视图辅助 ----------

    def _providers_for(self, item: str) -> list[ClaimRecord]:
        return [c for c in self.ledger.claims if item in c.items]

    def _missing_expected(self, claim: ClaimRecord) -> list[str]:
        """报告清单中尚未成功下载（无本地有效摘要）的原件。"""
        have = {o.name for o in claim.originals}
        return [n for n in claim.original_manifest if n not in have]

    @staticmethod
    def _claim_ref(claim: ClaimRecord, note: str) -> dict[str, Any]:
        return {"report": claim.report_id, "revision": claim.revision,
                "sample": claim.sample, "configuration": claim.configuration,
                "joint": claim.joint, "status": claim.status,
                "supersedes": claim.supersedes, "note": note}

    @staticmethod
    def _explain(reply: dict) -> str:
        code = reply.get("error", "unknown")
        return _REJECT_CODES.get(code, f"档案台拒绝：{code}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
