"""The helmdrift command: arguments and wiring only."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from helmdrift import __version__
from helmdrift.diff import check
from helmdrift.live import ClusterReader, KubeReader, LiveError
from helmdrift.render import RenderError, parse_manifest, render_chart, render_release
from helmdrift.report import exit_code, render_json, render_text
from helmdrift.rules import RulesError, builtin_rules, load_ignore_file

#: Picked up automatically from the working directory, so a repo can keep its rules next to it.
DEFAULT_IGNORE_FILE = ".helmdrift-ignore.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helmdrift",
        description="What is running in the cluster versus what the Helm chart says, and who "
        "changed it. Read-only. Exits 0 when there is no drift, 1 when there is, 2 on errors.",
    )
    parser.add_argument("--release", "-r", required=True, help="the Helm release to check")
    parser.add_argument("--namespace", "-n", required=True, help="the release's namespace")

    source = parser.add_argument_group(
        "what should be running (default: what Helm applied at the last install or upgrade)"
    )
    choice = source.add_mutually_exclusive_group()
    choice.add_argument("--chart", help="compare with this chart, rendered with helm template")
    choice.add_argument(
        "--manifest", help="compare with an already rendered manifest file, or - for stdin"
    )
    source.add_argument(
        "-f",
        "--values",
        action="append",
        default=[],
        metavar="FILE",
        help="values file for --chart; repeatable",
    )
    source.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="value override for --chart; repeatable",
    )

    cluster = parser.add_argument_group("cluster")
    cluster.add_argument("--context", help="kubeconfig context to read (default: the current one)")
    cluster.add_argument(
        "--kubeconfig", help="kubeconfig file (default: $KUBECONFIG or ~/.kube/config)"
    )
    cluster.add_argument("--helm", default="helm", help="the helm binary (default: helm on PATH)")

    rules = parser.add_argument_group("rules")
    rules.add_argument(
        "--ignore-file",
        action="append",
        default=[],
        metavar="FILE",
        help=f"rules for differences to ignore; repeatable (default: {DEFAULT_IGNORE_FILE})",
    )
    rules.add_argument(
        "--manager",
        action="append",
        default=[],
        metavar="NAME",
        help="another field manager that counts as the chart, besides helm; repeatable",
    )
    rules.add_argument(
        "--no-hpa-rule",
        action="store_true",
        help="report replica counts set by a HorizontalPodAutoscaler as drift",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--format", choices=["text", "json"], default="text")
    output.add_argument("--show-ignored", action="store_true", help="list ignored differences too")
    output.add_argument("--no-color", action="store_true", help="plain text, even in a terminal")
    parser.add_argument("--version", action="version", version=f"helmdrift {__version__}")
    return parser


def run(argv: Sequence[str] | None = None, reader: ClusterReader | None = None) -> int:
    """Run the command and return its exit code. Tests pass a reader instead of a real cluster."""
    args = build_parser().parse_args(argv)
    if (args.values or args.set) and not args.chart:
        print("helmdrift: error: -f/--values and --set need --chart", file=sys.stderr)
        return 2
    try:
        rules = builtin_rules()
        ignore_files = args.ignore_file or (
            [DEFAULT_IGNORE_FILE] if Path(DEFAULT_IGNORE_FILE).is_file() else []
        )
        for path in ignore_files:
            rules = rules.with_ignore(load_ignore_file(path))

        if args.chart:
            desired = render_chart(
                args.chart, args.release, args.namespace, args.values, args.set, helm=args.helm
            )
            source = f"the chart {args.chart}"
        elif args.manifest:
            text = sys.stdin.read() if args.manifest == "-" else _read(args.manifest)
            desired = parse_manifest(text, args.namespace)
            source = "standard input" if args.manifest == "-" else f"the manifest {args.manifest}"
        else:
            desired = render_release(
                args.release, args.namespace, args.context, args.kubeconfig, helm=args.helm
            )
            source = "what Helm applied at the last install or upgrade"

        reader = reader or KubeReader(args.context, args.kubeconfig)
        report = check(
            desired,
            reader,
            args.release,
            args.namespace,
            rules,
            chart_managers=["helm", *args.manager],
            hpa_rule=not args.no_hpa_rule,
        )
    except (RenderError, LiveError, RulesError) as error:
        print(f"helmdrift: error: {error}", file=sys.stderr)
        return 2

    if args.format == "json":
        context = {
            "release": args.release,
            "namespace": args.namespace,
            "source": source,
            "ignore_files": ignore_files,
        }
        render_json(report, context, sys.stdout)
    else:
        heading = (
            f"helmdrift: release {args.release} in namespace {args.namespace}, "
            f"{report.target}, compared with {source}"
        )
        if ignore_files:
            heading += f"; ignore rules from {', '.join(ignore_files)}"
        render_text(
            report,
            heading,
            sys.stdout,
            color=False if args.no_color else None,
            show_ignored=args.show_ignored,
        )
    return exit_code(report)


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise RenderError(f"cannot read {path}: {error.strerror}") from None


def main() -> None:
    raise SystemExit(run())
