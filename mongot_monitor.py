#!/usr/bin/env python3
"""
MongoDB Search Node Monitor (mongot) - Ultimate SRE Advisor Edition
===================================================================
Entry point: App Factory + CLI startup.
"""

import argparse
import json
import logging
import os
from datetime import datetime

try:
    from bson import Binary, ObjectId, Timestamp
except ImportError:
    Binary = bytes
    ObjectId = type(None)
    Timestamp = type(None)

from flask import Flask
from flask_cors import CORS

import state
from collectors.kubernetes import K8S_AVAILABLE, init_k8s
from collectors.mongodb import init_mongo
from background import BackgroundCollector
from security import BasicAuth, register_security_headers

log = logging.getLogger("mongot-monitor")


# ── JSON encoder ──────────────────────────────────────────────────────────────

class MongoJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (Binary, bytes)): return str(o)
        if isinstance(o, ObjectId): return str(o)
        if isinstance(o, datetime): return o.isoformat()
        if isinstance(o, Timestamp): return o.as_datetime().isoformat()
        try: return super().default(o)
        except TypeError: return str(o)


# ── App Factory ───────────────────────────────────────────────────────────────

def create_app(allowed_origins=None, basic_auth=None) -> Flask:
    _basedir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(_basedir, "frontend", "templates"),
        static_folder=os.path.join(_basedir, "frontend", "static"),
    )

    # CORS — default to same-origin only; pass explicit origins in production
    origins = allowed_origins or ["http://127.0.0.1:5050", "http://localhost:5050"]
    CORS(app, origins=origins)

    app.json_encoder = MongoJSONEncoder
    try:
        app.json.compact = True
    except Exception:
        pass

    register_security_headers(app)

    if basic_auth:
        basic_auth.register(app)

    from routes.api import api_bp
    from routes.frontend import frontend_bp
    app.register_blueprint(api_bp)
    app.register_blueprint(frontend_bp)

    return app


app = create_app()


# ── CLI entry point ───────────────────────────────────────────────────────────

def _print_diagnosis(diag: dict) -> None:
    """Print a human-readable diagnosis report to stdout."""
    R = "\033[0m"
    health = diag["health"]
    color  = {"healthy": "\033[32m", "degraded": "\033[33m", "critical": "\033[31m"}.get(health, "")
    s      = diag["summary"]

    print("\n" + "━" * 54)
    print("  MongoDB Search Diagnostics — Automatic Diagnosis")
    print("━" * 54)
    print(f"\nHEALTH STATUS: {color}{health.upper()}{R}  "
          f"({s['crit']} critical, {s['warn']} warnings, {s['pass']} passed)\n")

    for item in diag.get("healthy", []):
        print(f"  \033[32m✔{R} {item['title']}")

    if diag.get("warnings"):
        print()
        for item in diag["warnings"]:
            print(f"  \033[33m⚠{R}  {item['title']}")
            print(f"     {item['detail']}")

    if diag.get("critical"):
        print()
        for item in diag["critical"]:
            print(f"  \033[31m✖{R}  {item['title']}")
            print(f"     {item['detail']}")

    if diag.get("recommendations"):
        print("\nRECOMMENDATIONS")
        for rec in diag["recommendations"]:
            print(f"  → {rec}")

    print("\n" + "━" * 54 + "\n")


def _print_index_report(reports: list) -> None:
    """Print a human-readable Search Index Inspector report to stdout."""
    R  = "\033[0m"
    GR = "\033[32m"
    YL = "\033[33m"
    RD = "\033[31m"
    CY = "\033[36m"

    print("\n" + "━" * 58)
    print("  MongoDB Search — Index Inspector")
    print("━" * 58)

    if not reports:
        print("\n  No Search indexes found.\n")
        print("━" * 58 + "\n")
        return

    for r in reports:
        status_color = RD if r["status"] == "FAILED" else YL if r["status"] == "BUILDING" else GR
        obs = r["observations"]
        has_crit = any(o["level"] == "crit" for o in obs)
        has_warn = any(o["level"] == "warn" for o in obs)
        ns_color = RD if has_crit else YL if has_warn else GR

        print(f"\n{CY}Collection:{R} {r['ns']}")
        print(f"  Index: {r['name']}  [{r['type']}]  {status_color}{r['status']}{R}")

        if r["num_docs"] is not None:
            print(f"  Docs: {r['num_docs']:,}")

        if r["type"] == "fullText":
            dynamic_label = f"{YL}dynamic ⚠{R}" if r["mapping_dynamic"] else f"static ({r['field_count']} fields)"
            print(f"  Mapping: {dynamic_label}")
        elif r["type"] == "vectorSearch":
            print(f"  Vector fields: {r['field_count']}")

        if not obs:
            print(f"  {GR}✔ No issues detected{R}")
        else:
            for o in obs:
                lvl_color = RD if o["level"] == "crit" else YL
                icon = "✖" if o["level"] == "crit" else "⚠"
                print(f"  {lvl_color}{icon}{R} {o['msg']}")
                print(f"    → {o['suggestion']}")

    from collectors.index_inspector import summarize
    s = summarize(reports)
    health_color = RD if s["health"] == "critical" else YL if s["health"] == "degraded" else GR
    print(f"\n{'━' * 58}")
    print(f"  {s['total_indexes']} index(es)  |  "
          f"{RD}{s['crits']} critical{R}, {YL}{s['warns']} warnings{R}, {GR}{s['clean']} clean{R}")
    print(f"  Health: {health_color}{s['health'].upper()}{R}")
    print("━" * 58 + "\n")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=None)
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--in-cluster", action="store_true")
    parser.add_argument("--interval", type=int, default=5,
                        help="Collector interval in seconds")
    parser.add_argument("--allowed-origins", nargs="*", default=None,
                        help="CORS allowed origins (space-separated). Default: localhost only.")
    parser.add_argument("--auth", default=None,
                        help="Enable HTTP Basic Auth. Format: user:password")
    parser.add_argument("--diagnose", action="store_true",
                        help="Run a single diagnostic cycle, print report, and exit. "
                             "Exit code: 0=healthy, 1=degraded, 2=critical")
    parser.add_argument("--report", action="store_true",
                        help="Generate a full status report and print it. Use with --format.")
    parser.add_argument("--format", choices=["text", "markdown", "json"], default="text",
                        help="Report output format: text (default), markdown, json")
    parser.add_argument("--inspect-indexes", action="store_true",
                        help="Inspect all Search index definitions and print a quality report. "
                             "Exit code: 0=healthy, 1=degraded, 2=critical")
    args = parser.parse_args()

    state.TARGET_NAMESPACE = args.namespace

    if K8S_AVAILABLE:
        init_k8s(in_cluster=args.in_cluster)

    if args.uri:
        init_mongo(args.uri)

    if args.report:
        import sys, json as _json
        from report import build_text, build_markdown, build_json
        collector = BackgroundCollector(interval=args.interval)
        log.info("Running single collection cycle for report...")
        collector._collect()
        with state.cache_lock:
            data     = state.metrics_cache.get("data") or {}
            findings = state.metrics_cache.get("advisor") or []
        if args.format == "json":
            print(_json.dumps(build_json(data, findings), indent=2, default=str))
        elif args.format == "markdown":
            print(build_markdown(data, findings))
        else:
            print(build_text(data, findings))
        sys.exit(0)

    if args.inspect_indexes:
        import sys
        from collectors.index_inspector import inspect_search_indexes, summarize
        if not state.mongo_client:
            print("✗  MongoDB not configured — pass --uri <connection_string>")
            sys.exit(2)
        reports = inspect_search_indexes(state.mongo_client)
        _print_index_report(reports)
        s = summarize(reports)
        sys.exit({"healthy": 0, "degraded": 1, "critical": 2}.get(s["health"], 1))

    if args.diagnose:
        import sys
        from advisor import format_diagnosis
        collector = BackgroundCollector(interval=args.interval)
        log.info("Running single diagnostic cycle...")
        collector._collect()
        with state.cache_lock:
            findings = state.metrics_cache.get("advisor") or []
        diag = format_diagnosis(findings)
        _print_diagnosis(diag)
        sys.exit({"healthy": 0, "degraded": 1, "critical": 2}.get(diag["health"], 1))

    basic_auth = None
    if args.auth:
        if ":" not in args.auth:
            parser.error("--auth must be in the format user:password")
        user, _, pwd = args.auth.partition(":")
        basic_auth = BasicAuth(user, pwd)
        log.info(f"🔒 Basic Auth attivata per l'utente: {user}")

    flask_app = create_app(
        allowed_origins=args.allowed_origins,
        basic_auth=basic_auth,
    )

    BackgroundCollector(interval=args.interval).start()

    log.info(f"🚀 Dashboard in esecuzione: http://{args.host}:{args.port}")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask_app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
