"""命令行入口：领取、待办核对、移交、移交查询、断线核实。

所有命令都通过 LabBrowser 驱动 Chromium 完成页面交互；输入与本地路径全部
显式传参，默认工作目录位于仓库 var/ 下（已 gitignore）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .browser import LabBrowser
from .engine import CollectorEngine
from .errors import CollectorError
from .ledger import Ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collector",
        description="联合实验室报告/仪器原件领取与项目档案台移交客户端（仅合成来源）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765",
                        help="合成实验室门户地址")
    parser.add_argument("--ledger", default="var/collector-ledger.json",
                        help="本地档案台账路径（原子写入）")
    parser.add_argument("--downloads", default="var/downloads",
                        help="下载原件根目录，每份报告一个独立子目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_claim = sub.add_parser("claim", help="按授权领取正式报告与仪器原件")
    p_claim.add_argument("--request", default="inputs/request.json",
                         help="项目可读输入（仅首次初始化台账时使用）")

    p_todo = sub.add_parser("todo", help="待办核对：当前依据、缺件、预览/撤签/替代状态")
    p_todo.add_argument("--request", default="inputs/request.json", help=argparse.SUPPRESS)

    p_transfer = sub.add_parser("transfer", help="向档案台页面移交并读取回执（幂等）")
    p_transfer.add_argument("--report", help="只处理指定报告（默认覆盖输入请求项目）")
    p_transfer.add_argument("--request", default="inputs/request.json", help=argparse.SUPPRESS)

    p_reconcile = sub.add_parser("reconcile", help="断线后只用归档历史核实收件，不重复提交")
    p_reconcile.add_argument("--request", default="inputs/request.json", help=argparse.SUPPRESS)
    p_handoffs = sub.add_parser("handoffs", help="移交查询：逐项归档号、pending、替代与暂停")
    p_handoffs.add_argument("--request", default="inputs/request.json", help=argparse.SUPPRESS)
    p_claims = sub.add_parser("claims", help="领取查询：样品/配置归属、原件清单与摘要")
    p_claims.add_argument("--request", default="inputs/request.json", help=argparse.SUPPRESS)
    return parser


def _load_request(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger_path = Path(args.ledger)
    request = _load_request(args.request) if (
        args.command == "claim" and not ledger_path.exists()) else None
    try:
        ledger = Ledger(ledger_path, request=request)
        with LabBrowser(args.base_url) as browser:
            engine = CollectorEngine(ledger, browser, Path(args.downloads))
            if args.command == "claim":
                _print_json(engine.claim())
            elif args.command == "todo":
                _print_json(engine.todo_view())
            elif args.command == "transfer":
                records = engine.transfer(only_report=args.report)
                _print_json(engine.handoffs_view() | {
                    "this_run": [h.key for h in records]})
            elif args.command == "reconcile":
                touched = engine.reconcile()
                _print_json(engine.handoffs_view() | {
                    "reconciled": [h.key for h in touched]})
            elif args.command == "handoffs":
                _print_json(engine.handoffs_view())
            elif args.command == "claims":
                _print_json(engine.claims_view())
    except CollectorError as exc:
        # 临时故障与结构性问题都以非零码退出，消息中保留分类，便于脚本区分。
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
