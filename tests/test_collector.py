"""采集客户端集成测试：全部外部交互都驱动真实 Chromium 访问合成来源。

测试侧使用 /__control 切换来源修订（retest/revoke/partial/lose_reply），
这是演练人员的职责；被测的 collector 包自身绝不访问该接口（见
test_no_control_endpoint_usage）。
"""
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from collector.browser import LabBrowser
from collector.engine import CollectorEngine
from collector.ledger import Ledger
from tests.support import SourceTest

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST = {
    "authorized_samples": ["s1", "s2"],
    "project": "project-alpha",
    "requested_items": ["latency", "power", "thermal"],
    "archive_reference": "handoff-1",
}


class CollectorCase(SourceTest):
    def make_engine(self, workdir: Path):
        ledger = Ledger(workdir / "ledger.json", request=REQUEST)
        browser = LabBrowser(self.base)
        browser.__enter__()
        # 显式注册：测试体结束立即关闭浏览器，先于 SourceTest.tearDown 关服务器，
        # 避免在已关闭的事件循环上继续驱动页面。
        self.addCleanup(browser.__exit__, None, None, None)
        engine = CollectorEngine(ledger, browser, workdir / "downloads")
        return ledger, engine, browser

    def history_keys(self, browser: LabBrowser) -> set[str]:
        return set(browser.read_history().keys())

    def write_request(self, workdir: Path, payload=REQUEST) -> Path:
        path = workdir / "request.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path


class ClaimTest(CollectorCase):
    def test_claim_skips_preview_and_enforces_authorization(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, engine, _ = self.make_engine(tmp)
            summary = engine.claim()
            self.assertEqual(sorted(summary["claimed"]), ["R-A", "R-C", "R-J"])
            self.assertEqual(summary["skipped_preview"], ["PRE"])
            self.assertEqual(summary["transient_failures"], [])

            ledger = Ledger(tmp / "ledger.json")
            by_id = {c.report_id: c for c in ledger.claims}
            # 预览登记但不下载任何原件，绝不能作为可交付证明。
            self.assertEqual(by_id["PRE"].status, "preview")
            self.assertEqual(by_id["PRE"].originals, [])
            # 样品/配置归属清晰；每份报告原件在独立目录，同名文件不串用。
            ra, rj = by_id["R-A"], by_id["R-J"]
            self.assertEqual((ra.sample, ra.configuration),
                             ("s1", "card-c1/driver-d10/fw-f2"))
            self.assertTrue(rj.joint)
            paths = {o.name: o.path for o in ra.originals + rj.originals}
            self.assertTrue(all(Path(p).is_file() for p in paths.values()))
            self.assertIn("R-A-rev1", paths["latency-1.bin"])
            self.assertIn("R-J-rev1", paths["power-1.bin"])
            # 本地实算摘要与页面公示摘要一致。
            for claim in (ra, rj):
                for original in claim.originals:
                    self.assertEqual(original.sha256, original.page_digest)

    def test_outside_authorization_is_not_claimed(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            request = dict(REQUEST, authorized_samples=["s2"])
            ledger = Ledger(tmp / "ledger.json", request=request)
            with LabBrowser(self.base) as browser:
                engine = CollectorEngine(ledger, browser, tmp / "downloads")
                summary = engine.claim()
            self.assertEqual(summary["outside_authorization"],
                             ["R-A", "R-J"])  # s1 的报告一律不领取
            self.assertEqual(summary["claimed"], ["R-C"])


class TransferTest(CollectorCase):
    def test_joint_report_moves_as_a_set_and_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, engine, browser = self.make_engine(tmp)
            engine.claim()
            handoffs = engine.transfer()
            keys = {h.key for h in handoffs}
            self.assertEqual(keys, {"handoff-1:R-A", "handoff-1:R-J"})
            joint = next(h for h in handoffs if h.report_id == "R-J")
            # 联合结论成套：power/thermal 同时在同一份收件记录里。
            self.assertEqual(set(joint.accepted), {"power", "thermal"})
            self.assertEqual(joint.pending, [])
            # 服务端同 key 只有一份记录。
            server_before = self.history_keys(browser)
            engine.transfer()  # 重复运行
            self.assertEqual(server_before, self.history_keys(browser))

    def test_lose_reply_reconciles_via_history_without_duplicate(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, engine, browser = self.make_engine(tmp)
            engine.claim()
            self.control("lose_reply")  # 首个移交写入后页面 503
            handoffs = {h.report_id: h for h in engine.transfer()}
            ra = handoffs["R-A"]
            self.assertEqual(ra.state, "closed")
            self.assertIn("latency", ra.accepted)
            self.assertEqual(ra.last_error, "")
            # 再次运行只做历史对账，绝不产生第二份记录。
            server_before = self.history_keys(browser)
            engine.transfer()
            self.assertEqual(server_before, self.history_keys(browser))
            kinds = {e["kind"] for e in Ledger(tmp / "ledger.json").events}
            self.assertIn("transfer.gateway-after-write", kinds)

    def test_partial_issuance_then_supplement_only_fills_gap(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            request = dict(REQUEST, authorized_samples=["s2"],
                           requested_items=["bandwidth", "stability"],
                           archive_reference="partial-1")
            ledger = Ledger(tmp / "ledger.json", request=request)
            with LabBrowser(self.base) as browser:
                engine = CollectorEngine(ledger, browser, tmp / "downloads")
                engine.claim()
                self.control("partial")
                first = engine.transfer()
                self.assertEqual(len(first), 1)
                self.assertEqual(list(first[0].accepted), ["bandwidth"])
                self.assertEqual(first[0].pending, ["stability"])

                self.control("complete")
                again = engine.transfer()
                # 补齐使用独立 key，只包含缺项，原记录保留。
                sup = next(h for h in again if ":sup:" in h.key)
                self.assertEqual(sup.items, ["stability"])
                self.assertEqual(list(sup.accepted), ["stability"])
                base = next(h for h in again if ":sup:" not in h.key)
                self.assertEqual(list(base.accepted), ["bandwidth"])
                history = self.history_keys(browser)
            self.assertEqual(len(history), 2)
            self.assertTrue(any(k.endswith(":sup:stability") for k in history))


class LifecycleTest(CollectorCase):
    def test_retest_supersedes_only_named_item_and_keeps_archive(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger, engine, _ = self.make_engine(tmp)
            engine.claim()
            engine.transfer()
            old_arc = ledger.find_handoff("handoff-1:R-A").accepted["latency"]

            self.control("retest")
            summary = engine.claim()
            self.assertIn("R-B", summary["claimed"])
            engine.transfer()

            ra = ledger.find_handoff("handoff-1:R-A")
            rb = ledger.find_handoff("handoff-1:R-B")
            self.assertEqual(ra.state, "superseded")
            self.assertIn(old_arc, ra.note)  # 原归档号保留备查，不删除
            self.assertEqual(rb.state, "closed")  # 复测结果独立有效
            # R-J 未被复测提及，继续有效，不作废。
            self.assertEqual(ledger.find_handoff("handoff-1:R-J").state, "closed")
            # R-B 属于新配置（fw-f3），引用自己清单里的 latency-2.bin。
            rb_claim = ledger.find_claim("R-B")
            self.assertEqual(rb_claim.configuration, "card-c1/driver-d10/fw-f3")
            self.assertEqual([o.name for o in rb_claim.originals], ["latency-2.bin"])
            self.assertIn("R-B-rev2", rb_claim.originals[0].path)

    def test_revocation_suspends_citation_but_independent_proofs_survive(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger, engine, _ = self.make_engine(tmp)
            engine.claim()
            engine.transfer()
            self.control("retest")
            engine.claim()
            engine.transfer()
            self.control("revoke")
            engine.claim()  # 撤签对账

            ra = ledger.find_handoff("handoff-1:R-A")
            self.assertEqual(ra.state, "superseded")  # 已被替代的历史保持替代结论
            rb = ledger.find_handoff("handoff-1:R-B")
            rj = ledger.find_handoff("handoff-1:R-J")
            self.assertEqual(rb.state, "closed")  # 独立有效证明继续可用
            self.assertEqual(rj.state, "closed")
            self.assertEqual(ledger.find_claim("R-A").status, "revoked")
            # 旧下载文件不删除。
            self.assertTrue(any(o.name == "latency-1.bin"
                                for o in ledger.find_claim("R-A").originals))

            view = engine.todo_view()
            latency = view["items"]["latency"]
            self.assertEqual([x["report"] for x in latency["citable"]], ["R-B"])
            blocked = {x["report"]: x["note"] for x in latency["blocked"]}
            self.assertIn("撤签", blocked["R-A"])
            self.assertIn("预览", blocked["PRE"])


class FailureSeparationTest(CollectorCase):
    def test_transient_download_failure_is_retryable_and_distinct_from_revoke(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger, engine, browser = self.make_engine(tmp)
            calls = {"n": 0}
            real_download = browser.download_original

            def flaky(report_id, name, dest_dir):
                calls["n"] += 1
                if calls["n"] == 1:  # 首次领取时 R-A 唯一原件临时失败
                    from collector.errors import TransientBrowserError
                    raise TransientBrowserError("模拟连接中断")
                return real_download(report_id, name, dest_dir)

            browser.download_original = flaky
            summary = engine.claim()
            self.assertEqual(len(summary["transient_failures"]), 1)
            ra = ledger.find_claim("R-A")
            self.assertEqual(ra.status, "signed")  # 失败不改写签发状态
            self.assertTrue(ra.failures)
            self.assertEqual(ra.originals, [])

            # 待办归为可重试故障，而不是撤签/预览；R-A 缺原件不产生移交，
            # 但不阻塞原件齐全的 R-J。
            todo = engine.todo_view()
            note = todo["items"]["latency"]["blocked"][0]["note"]
            self.assertIn("临时下载失败", note)
            self.assertEqual({h.report_id for h in engine.transfer()}, {"R-J"})

            # 恢复后重跑领取，原件补齐、故障清除，移交成功。
            browser.download_original = real_download
            engine.claim()
            ra = ledger.find_claim("R-A")
            self.assertEqual(ra.failures, [])
            self.assertEqual(len(ra.originals), 1)
            handoffs = engine.transfer()
            self.assertIn("handoff-1:R-A", {h.key for h in handoffs})

    def test_connection_lost_after_submit_never_resubmits_blindly(self):
        from collector.errors import TransientBrowserError
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ledger_path = tmp / "ledger.json"
            ledger = Ledger(ledger_path, request=REQUEST)

            # 第一次运行：提交真实写入后模拟断连，历史页也暂时不可达。
            browser = LabBrowser(self.base).__enter__()
            try:
                engine = CollectorEngine(ledger, browser, tmp / "downloads")
                engine.claim()
                real_submit = browser.submit_archive
                submit_calls = {"n": 0}

                def submit_then_disconnect(key, report_id, items):
                    reply = real_submit(key, report_id, items)
                    if report_id == "R-A":
                        submit_calls["n"] += 1
                        raise TransientBrowserError("模拟提交后断连")
                    return reply

                def unavailable_history():
                    raise TransientBrowserError("模拟历史页暂时不可达")

                browser.submit_archive = submit_then_disconnect
                browser.read_history = unavailable_history
                engine.transfer()
            finally:
                browser.__exit__(None, None, None)

            handoff = ledger.find_handoff("handoff-1:R-A")
            self.assertTrue(handoff.submit_attempted)
            self.assertIn("连接断开", handoff.last_error)
            self.assertEqual(submit_calls["n"], 1)

            # 第二次运行：模拟 CLI 重启，从磁盘重载台账；禁止任何重新提交。
            reloaded = Ledger(ledger_path)
            browser2 = LabBrowser(self.base).__enter__()
            try:
                browser2.submit_archive = lambda *a: self.fail("不得重复提交")
                engine2 = CollectorEngine(reloaded, browser2, tmp / "downloads")
                engine2.transfer()
                server_keys = set(browser2.read_history().keys())
            finally:
                browser2.__exit__(None, None, None)

            handoff = reloaded.find_handoff("handoff-1:R-A")
            self.assertEqual(handoff.state, "closed")
            self.assertIn("latency", handoff.accepted)
            self.assertEqual(
                len([k for k in server_keys if k.startswith("handoff-1:R-A")]), 1)


class CliTest(CollectorCase):
    def test_cli_runs_under_isolated_mode_and_uses_browser(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            request_path = self.write_request(tmp)
            ledger_path = tmp / "ledger.json"
            dl = tmp / "downloads"
            common = ["python3", "-I", str(REPO_ROOT / "run_collector.py"),
                      "--base-url", self.base, "--ledger", str(ledger_path),
                      "--downloads", str(dl)]
            claim = subprocess.run(common + ["claim", "--request", str(request_path)],
                                   cwd=tmp, capture_output=True, text=True, timeout=120)
            self.assertEqual(claim.returncode, 0, claim.stderr)
            self.assertIn("claimed", claim.stdout)
            transfer = subprocess.run(common + ["transfer"], cwd=tmp,
                                      capture_output=True, text=True, timeout=120)
            self.assertEqual(transfer.returncode, 0, transfer.stderr)
            handoffs = subprocess.run(common + ["handoffs"], cwd=tmp,
                                      capture_output=True, text=True, timeout=120)
            payload = json.loads(handoffs.stdout)
            self.assertTrue(any(h["state"] == "closed"
                                for h in payload["handoffs"]))
            # 下载目录隔离：每个报告一个子目录。
            subdirs = sorted(p.name for p in dl.iterdir() if p.is_dir())
            self.assertTrue(subdirs)
            self.assertTrue(all(d.startswith("R-") for d in subdirs))

    def test_no_control_endpoint_usage(self):
        # 客户端代码只能通过页面工作，演练控制接口仅测试侧可用。
        # 用 AST 检查真实字符串常量，忽略文档字符串里的说明性文字。
        import ast
        forbidden = []
        for py in list(REPO_ROOT.joinpath("collector").glob("*.py")) + [
                REPO_ROOT / "run_collector.py"]:
            tree = ast.parse(py.read_text(encoding="utf-8"))
            docstring_nodes = set()
            scopes = [tree] + [n for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                 ast.ClassDef))]
            for scope in scopes:
                first = scope.body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstring_nodes.add(id(first.value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and "__control" in node.value
                        and id(node) not in docstring_nodes):
                    forbidden.append((py.name, node.lineno))
        self.assertEqual(forbidden, [])
