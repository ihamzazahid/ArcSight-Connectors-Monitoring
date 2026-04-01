#!/usr/bin/env python3
"""
ArcSight SmartConnector Prometheus Exporter
Scrapes metrics from ArcSight connectors in /opt/arcsight/connectors/<name>/
and exposes them via a Prometheus-compatible /metrics HTTP endpoint.
"""

import os
import re
import time
import glob
import subprocess
import logging
import argparse
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("arcsight_exporter")

# ─── Configuration ────────────────────────────────────────────────────────────
CONNECTORS_BASE = "/opt/arcsight/connectors"
DEFAULT_PORT    = 9116
SCRAPE_INTERVAL = 30   # seconds between background refreshes
LOG_TAIL_LINES  = 500  # lines to tail from agent.log


# ─── Regex patterns for agent.log parsing ─────────────────────────────────────
# ArcSight log examples:
#   [INFO] EPS: current=1234 average=1100 peak=1500
#   [INFO] Queue depth: 5432
#   [WARN] Cache drop: dropped 12 events
#   [INFO] Connector status: connected
#   [INFO] Cef event sent: 9871234
#   [ERROR] Failed to connect to destination

RE_EPS_CURRENT  = re.compile(r"EPS[:\s]+current[=:\s]+(\d+)", re.IGNORECASE)
RE_EPS_AVERAGE  = re.compile(r"EPS[:\s]+average[=:\s]+(\d+)", re.IGNORECASE)
RE_EPS_PEAK     = re.compile(r"EPS[:\s]+peak[=:\s]+(\d+)", re.IGNORECASE)
RE_QUEUE_DEPTH  = re.compile(r"[Qq]ueue\s+depth[=:\s]+(\d+)")
RE_QUEUE_SIZE   = re.compile(r"[Qq]ueue\s+size[=:\s]+(\d+)")
RE_CACHE_DROP   = re.compile(r"[Cc]ache\s+drop[^:]*:\s+dropped\s+(\d+)", re.IGNORECASE)
RE_CACHE_EVENTS = re.compile(r"[Cc]ached\s+events[=:\s]+(\d+)", re.IGNORECASE)
RE_ERROR_LINE   = re.compile(r"\[ERROR\]|\bERROR\b")
RE_WARN_LINE    = re.compile(r"\[WARN\]|\bWARN\b")
RE_CEF_SENT     = re.compile(r"[Cc]ef\s+event\s+sent[=:\s]+(\d+)")
RE_EVENTS_SENT  = re.compile(r"[Ee]vents?\s+sent[=:\s]+(\d+)")
RE_STATUS_CONN  = re.compile(r"[Cc]onnector\s+status[=:\s]+connected", re.IGNORECASE)
RE_STATUS_DISC  = re.compile(r"[Cc]onnector\s+status[=:\s]+disconnected", re.IGNORECASE)

# Heap parsing from agent.out / stderr: e.g.  Heap: used=234M max=512M
RE_HEAP_USED    = re.compile(r"[Hh]eap[^:]*used[=:\s]+(\d+)", re.IGNORECASE)
RE_HEAP_MAX     = re.compile(r"[Hh]eap[^:]*max[=:\s]+(\d+)", re.IGNORECASE)


# ─── Metric Store ─────────────────────────────────────────────────────────────
class ConnectorMetrics:
    """Holds the last-scraped metrics for a single connector."""

    def __init__(self, name: str, path: str, connector_type: str, version: str):
        self.name           = name
        self.path           = path
        self.connector_type = connector_type
        self.version        = version

        # Process
        self.up             = 0       # 1 = process running
        self.pid            = -1

        # EPS
        self.eps_current    = 0.0
        self.eps_average    = 0.0
        self.eps_peak       = 0.0

        # Queue
        self.queue_depth    = 0
        self.queue_size     = 0       # configured max, from properties

        # Cache / Drop
        self.cache_drops    = 0       # events dropped from cache in window
        self.cached_events  = 0       # events currently cached

        # Errors / Warnings in log window
        self.error_count    = 0
        self.warn_count     = 0

        # Events forwarded
        self.events_sent    = 0

        # JVM heap (MB)
        self.heap_used_mb   = 0.0
        self.heap_max_mb    = 0.0

        # Connectivity (from log status lines)
        self.connected      = -1      # -1=unknown, 0=disconnected, 1=connected

        # Timestamps
        self.last_event_ts  = 0       # unix ts of last log line parsed
        self.scrape_duration_ms = 0.0


# ─── Properties Reader ────────────────────────────────────────────────────────
def read_agent_properties(connector_path: str) -> dict:
    """Parse agent.properties into a dict."""
    props = {}
    prop_file = os.path.join(connector_path, "current", "user", "agent", "agent.properties")
    if not os.path.isfile(prop_file):
        # Try alternate locations
        for alt in [
            os.path.join(connector_path, "agent.properties"),
            os.path.join(connector_path, "current", "config", "agent.properties"),
        ]:
            if os.path.isfile(alt):
                prop_file = alt
                break
        else:
            return props

    try:
        with open(prop_file, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
    except Exception as e:
        log.warning("Cannot read %s: %s", prop_file, e)
    return props


def find_log_file(connector_path: str) -> Optional[str]:
    """Find the agent.log file under the connector directory."""
    candidates = [
        os.path.join(connector_path, "current", "logs", "agent.log"),
        os.path.join(connector_path, "logs", "agent.log"),
        os.path.join(connector_path, "agent.log"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Glob fallback
    found = glob.glob(os.path.join(connector_path, "**", "agent.log"), recursive=True)
    return found[0] if found else None


def find_out_file(connector_path: str) -> Optional[str]:
    """Find agent.out (JVM stdout) for heap stats."""
    candidates = [
        os.path.join(connector_path, "current", "logs", "agent.out"),
        os.path.join(connector_path, "logs", "agent.out"),
        os.path.join(connector_path, "agent.out"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    found = glob.glob(os.path.join(connector_path, "**", "agent.out"), recursive=True)
    return found[0] if found else None


# ─── Process Detection ────────────────────────────────────────────────────────
def detect_process(connector_path: str) -> tuple[int, int]:
    """
    Returns (is_up, pid).
    Looks for a java process with the connector path in its classpath/args.
    Also checks for a PID file.
    """
    # 1. PID file
    for pid_file in glob.glob(os.path.join(connector_path, "**", "*.pid"), recursive=True):
        try:
            pid = int(Path(pid_file).read_text().strip())
            proc_exists = os.path.isdir(f"/proc/{pid}")
            if proc_exists:
                return 1, pid
        except Exception:
            pass

    # 2. pgrep by connector path substring
    connector_name = os.path.basename(connector_path.rstrip("/"))
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", connector_path], stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.decode().split() if p.strip()]
        if pids:
            return 1, pids[0]
    except subprocess.CalledProcessError:
        pass

    # 3. pgrep by name pattern (arcsight connector daemon)
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", f"arcsight.*{connector_name}"], stderr=subprocess.DEVNULL
        )
        pids = [int(p) for p in out.decode().split() if p.strip()]
        if pids:
            return 1, pids[0]
    except subprocess.CalledProcessError:
        pass

    return 0, -1


def get_jvm_heap_from_pid(pid: int) -> tuple[float, float]:
    """
    Try to read heap info from /proc/<pid>/status (RSS as proxy)
    or from jcmd if available.
    Returns (used_mb, max_mb) — both 0 if unavailable.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        vm_rss = re.search(r"VmRSS:\s+(\d+)\s+kB", status)
        vm_size = re.search(r"VmSize:\s+(\d+)\s+kB", status)
        rss  = int(vm_rss.group(1))  / 1024 if vm_rss  else 0.0
        size = int(vm_size.group(1)) / 1024 if vm_size else 0.0
        return round(rss, 1), round(size, 1)
    except Exception:
        return 0.0, 0.0


# ─── Log Parser ───────────────────────────────────────────────────────────────
def parse_log_tail(log_file: str, n_lines: int = LOG_TAIL_LINES) -> dict:
    """
    Tail the last n_lines of agent.log and extract metrics.
    Returns a dict of found values.
    """
    results = {
        "eps_current":  None,
        "eps_average":  None,
        "eps_peak":     None,
        "queue_depth":  None,
        "cache_drops":  0,
        "cached_events":None,
        "error_count":  0,
        "warn_count":   0,
        "events_sent":  None,
        "connected":    -1,
        "last_event_ts":0,
    }

    try:
        proc = subprocess.run(
            ["tail", "-n", str(n_lines), log_file],
            capture_output=True, text=True, timeout=5
        )
        lines = proc.stdout.splitlines()
    except Exception as e:
        log.debug("Log tail failed for %s: %s", log_file, e)
        return results

    for line in lines:
        if RE_ERROR_LINE.search(line):
            results["error_count"] += 1
        if RE_WARN_LINE.search(line):
            results["warn_count"] += 1

        m = RE_EPS_CURRENT.search(line)
        if m:
            results["eps_current"] = float(m.group(1))

        m = RE_EPS_AVERAGE.search(line)
        if m:
            results["eps_average"] = float(m.group(1))

        m = RE_EPS_PEAK.search(line)
        if m:
            results["eps_peak"] = float(m.group(1))

        m = RE_QUEUE_DEPTH.search(line)
        if m:
            results["queue_depth"] = int(m.group(1))

        m = RE_CACHE_DROP.search(line)
        if m:
            results["cache_drops"] += int(m.group(1))

        m = RE_CACHE_EVENTS.search(line)
        if m:
            results["cached_events"] = int(m.group(1))

        m = RE_CEF_SENT.search(line) or RE_EVENTS_SENT.search(line)
        if m:
            results["events_sent"] = int(m.group(1))

        if RE_STATUS_CONN.search(line):
            results["connected"] = 1
        elif RE_STATUS_DISC.search(line):
            results["connected"] = 0

    if lines:
        results["last_event_ts"] = int(time.time())

    return results


def parse_out_file_heap(out_file: str) -> tuple[float, float]:
    """Parse heap info from agent.out tail."""
    try:
        proc = subprocess.run(
            ["tail", "-n", "100", out_file],
            capture_output=True, text=True, timeout=3
        )
        text = proc.stdout
        used = RE_HEAP_USED.search(text)
        mx   = RE_HEAP_MAX.search(text)
        return (
            float(used.group(1)) if used else 0.0,
            float(mx.group(1))   if mx   else 0.0,
        )
    except Exception:
        return 0.0, 0.0


# ─── Connector Discovery ──────────────────────────────────────────────────────
def discover_connectors(base_dir: str) -> list[ConnectorMetrics]:
    """
    Walk /opt/arcsight/connectors/<name>/ and return a list of ConnectorMetrics
    for each discovered connector.
    """
    connectors = []
    try:
        entries = sorted(os.listdir(base_dir))
    except FileNotFoundError:
        log.error("Base directory not found: %s", base_dir)
        return connectors

    for name in entries:
        full_path = os.path.join(base_dir, name)
        if not os.path.isdir(full_path):
            continue

        props = read_agent_properties(full_path)
        connector_type = props.get(
            "agents[0].devicetype",
            props.get("connector.type", "unknown")
        )
        version = props.get(
            "connector.version",
            props.get("agents[0].version", "unknown")
        )
        queue_size = int(props.get("queue.size", props.get("agents[0].queuesize", 0)))

        cm = ConnectorMetrics(
            name=name,
            path=full_path,
            connector_type=connector_type,
            version=version,
        )
        cm.queue_size = queue_size
        connectors.append(cm)
        log.info("Discovered connector: %s (type=%s)", name, connector_type)

    return connectors


# ─── Scrape Logic ─────────────────────────────────────────────────────────────
def scrape_connector(cm: ConnectorMetrics):
    """Update cm in-place with fresh metrics from process + log."""
    t0 = time.time()

    # 1. Process up/PID
    cm.up, cm.pid = detect_process(cm.path)

    # 2. JVM heap
    if cm.up and cm.pid > 0:
        cm.heap_used_mb, cm.heap_max_mb = get_jvm_heap_from_pid(cm.pid)

    # 2b. Heap from agent.out (may override/complement)
    out_file = find_out_file(cm.path)
    if out_file:
        u, mx = parse_out_file_heap(out_file)
        if u > 0:
            cm.heap_used_mb = u
        if mx > 0:
            cm.heap_max_mb = mx

    # 3. Log parsing
    log_file = find_log_file(cm.path)
    if log_file:
        r = parse_log_tail(log_file)
        if r["eps_current"]  is not None: cm.eps_current  = r["eps_current"]
        if r["eps_average"]  is not None: cm.eps_average  = r["eps_average"]
        if r["eps_peak"]     is not None: cm.eps_peak     = r["eps_peak"]
        if r["queue_depth"]  is not None: cm.queue_depth  = r["queue_depth"]
        if r["cached_events"]is not None: cm.cached_events= r["cached_events"]
        if r["events_sent"]  is not None: cm.events_sent  = r["events_sent"]
        cm.cache_drops    = r["cache_drops"]
        cm.error_count    = r["error_count"]
        cm.warn_count     = r["warn_count"]
        cm.connected      = r["connected"]
        cm.last_event_ts  = r["last_event_ts"]
    else:
        log.debug("No agent.log found under %s", cm.path)

    cm.scrape_duration_ms = round((time.time() - t0) * 1000, 2)


# ─── Prometheus Text Format ───────────────────────────────────────────────────
def format_label(name: str, connector_type: str, version: str) -> str:
    return (
        f'connector="{name}",'
        f'type="{connector_type}",'
        f'version="{version}"'
    )


def render_metrics(connectors: list[ConnectorMetrics]) -> str:
    lines = []

    def h(name, help_text, mtype="gauge"):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")

    h("arcsight_connector_up",
      "1 if the connector process is running, 0 otherwise")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_up{{{lb}}} {cm.up}')

    h("arcsight_connector_eps_current",
      "Current events per second processed by the connector")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_eps_current{{{lb}}} {cm.eps_current}')

    h("arcsight_connector_eps_average",
      "Average events per second (rolling window from log)")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_eps_average{{{lb}}} {cm.eps_average}')

    h("arcsight_connector_eps_peak",
      "Peak events per second observed in the log window")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_eps_peak{{{lb}}} {cm.eps_peak}')

    h("arcsight_connector_queue_depth",
      "Current internal event queue depth")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_queue_depth{{{lb}}} {cm.queue_depth}')

    h("arcsight_connector_queue_size_configured",
      "Configured maximum queue size from agent.properties")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_queue_size_configured{{{lb}}} {cm.queue_size}')

    h("arcsight_connector_queue_utilization_ratio",
      "Queue depth as a ratio of configured max (0-1). -1 if max unknown")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        ratio = (cm.queue_depth / cm.queue_size) if cm.queue_size > 0 else -1
        lines.append(f'arcsight_connector_queue_utilization_ratio{{{lb}}} {round(ratio, 4)}')

    h("arcsight_connector_cache_drops_total",
      "Total events dropped from cache in the last log window", "counter")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_cache_drops_total{{{lb}}} {cm.cache_drops}')

    h("arcsight_connector_cached_events",
      "Number of events currently held in cache")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_cached_events{{{lb}}} {cm.cached_events}')

    h("arcsight_connector_log_errors_total",
      "Number of ERROR lines in the last log window", "counter")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_log_errors_total{{{lb}}} {cm.error_count}')

    h("arcsight_connector_log_warnings_total",
      "Number of WARN lines in the last log window", "counter")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_log_warnings_total{{{lb}}} {cm.warn_count}')

    h("arcsight_connector_events_sent_total",
      "Cumulative CEF events forwarded to destination", "counter")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_events_sent_total{{{lb}}} {cm.events_sent}')

    h("arcsight_connector_connected",
      "1 if connector reports connected to destination, 0 disconnected, -1 unknown")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_connected{{{lb}}} {cm.connected}')

    h("arcsight_connector_jvm_heap_used_mb",
      "JVM heap memory currently used by the connector (MB)")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_jvm_heap_used_mb{{{lb}}} {cm.heap_used_mb}')

    h("arcsight_connector_jvm_heap_max_mb",
      "JVM heap max memory configured for the connector (MB)")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_jvm_heap_max_mb{{{lb}}} {cm.heap_max_mb}')

    h("arcsight_connector_jvm_heap_utilization_ratio",
      "JVM heap used/max ratio (0-1). -1 if max unavailable")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        ratio = (cm.heap_used_mb / cm.heap_max_mb) if cm.heap_max_mb > 0 else -1
        lines.append(f'arcsight_connector_jvm_heap_utilization_ratio{{{lb}}} {round(ratio, 4)}')

    h("arcsight_connector_last_event_timestamp_seconds",
      "Unix timestamp of last successful log parse (proxy for last event seen)")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_last_event_timestamp_seconds{{{lb}}} {cm.last_event_ts}')

    h("arcsight_connector_scrape_duration_ms",
      "Time taken to scrape this connector in milliseconds")
    for cm in connectors:
        lb = format_label(cm.name, cm.connector_type, cm.version)
        lines.append(f'arcsight_connector_scrape_duration_ms{{{lb}}} {cm.scrape_duration_ms}')

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ─── HTTP Handler ─────────────────────────────────────────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    connectors: list[ConnectorMetrics] = []

    def do_GET(self):
        if self.path in ("/metrics", "/metrics/"):
            payload = render_metrics(self.connectors).encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path in ("/", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)


# ─── Background Scraper ───────────────────────────────────────────────────────
def background_scraper(connectors: list[ConnectorMetrics], interval: int):
    """Continuously re-scrape all connectors every `interval` seconds."""
    while True:
        for cm in connectors:
            try:
                scrape_connector(cm)
                log.info(
                    "Scraped %s | up=%d eps=%.0f queue=%d drops=%d errors=%d heap=%.0fMB",
                    cm.name, cm.up, cm.eps_current,
                    cm.queue_depth, cm.cache_drops,
                    cm.error_count, cm.heap_used_mb,
                )
            except Exception as e:
                log.exception("Error scraping %s: %s", cm.name, e)
        time.sleep(interval)


# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ArcSight SmartConnector Prometheus Exporter"
    )
    parser.add_argument(
        "--base-dir", default=CONNECTORS_BASE,
        help=f"Base directory containing connector subdirs (default: {CONNECTORS_BASE})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--interval", type=int, default=SCRAPE_INTERVAL,
        help=f"Seconds between background scrapes (default: {SCRAPE_INTERVAL})"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)

    log.info("Starting ArcSight Exporter | base=%s port=%d interval=%ds",
             args.base_dir, args.port, args.interval)

    connectors = discover_connectors(args.base_dir)
    if not connectors:
        log.warning("No connectors discovered under %s — will still serve /metrics",
                    args.base_dir)

    # Initial scrape before starting HTTP
    for cm in connectors:
        try:
            scrape_connector(cm)
        except Exception as e:
            log.warning("Initial scrape failed for %s: %s", cm.name, e)

    # Start background scraper thread
    t = threading.Thread(
        target=background_scraper,
        args=(connectors, args.interval),
        daemon=True,
    )
    t.start()

    # Attach connectors to handler
    MetricsHandler.connectors = connectors

    server = HTTPServer(("0.0.0.0", args.port), MetricsHandler)
    log.info("Serving metrics at http://0.0.0.0:%d/metrics", args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
