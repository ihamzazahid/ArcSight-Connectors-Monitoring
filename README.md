# ArcSight Connector Prometheus Exporter

A lightweight, zero-dependency Python exporter that scrapes metrics from
ArcSight SmartConnectors installed under `/opt/arcsight/connectors/<name>/`
and exposes them in Prometheus text format on `:9116/metrics`.

---

## Files

| File | Purpose |
|---|---|
| `arcsight_exporter.py` | Main exporter (Python 3.8+, stdlib only) |
| `arcsight_exporter.service` | systemd unit |
| `prometheus_scrape_config.yml` | Prometheus job config snippet |
| `arcsight_alerts.yml` | Alerting rules (copy to Prometheus rules dir) |

---

## Metrics Exposed

| Metric | Description |
|---|---|
| `arcsight_connector_up` | 1 = process running |
| `arcsight_connector_eps_current` | Current EPS |
| `arcsight_connector_eps_average` | Rolling average EPS |
| `arcsight_connector_eps_peak` | Peak EPS in log window |
| `arcsight_connector_queue_depth` | Current queue depth |
| `arcsight_connector_queue_size_configured` | Max queue from agent.properties |
| `arcsight_connector_queue_utilization_ratio` | depth/max (0–1) |
| `arcsight_connector_cache_drops_total` | Dropped events in window |
| `arcsight_connector_cached_events` | Events currently cached |
| `arcsight_connector_log_errors_total` | ERROR lines in log window |
| `arcsight_connector_log_warnings_total` | WARN lines in log window |
| `arcsight_connector_events_sent_total` | CEF events forwarded |
| `arcsight_connector_connected` | 1/0/−1 connected/disconnected/unknown |
| `arcsight_connector_jvm_heap_used_mb` | JVM heap used (MB) |
| `arcsight_connector_jvm_heap_max_mb` | JVM heap max (MB) |
| `arcsight_connector_jvm_heap_utilization_ratio` | used/max (0–1) |
| `arcsight_connector_last_event_timestamp_seconds` | Unix ts of last log parse |
| `arcsight_connector_scrape_duration_ms` | Scrape time per connector |

All metrics carry labels: `connector`, `type`, `version`.

---

## Quick Install

```bash
# 1. Copy the exporter
sudo mkdir -p /opt/arcsight_exporter
sudo cp arcsight_exporter.py /opt/arcsight_exporter/
sudo chmod +x /opt/arcsight_exporter/arcsight_exporter.py

# 2. Test it manually first
sudo python3 /opt/arcsight_exporter/arcsight_exporter.py \
    --base-dir /opt/arcsight/connectors \
    --port 9116 \
    --log-level DEBUG

# In another terminal:
curl http://localhost:9116/metrics

# 3. Install as a service
sudo cp arcsight_exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arcsight_exporter

# Check status
sudo systemctl status arcsight_exporter
sudo journalctl -u arcsight_exporter -f
```

---

## Prometheus Integration

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: arcsight_connectors
    scrape_interval: 30s
    static_configs:
      - targets: ['<exporter-host>:9116']
```

For alerting rules:

```bash
sudo cp arcsight_alerts.yml /etc/prometheus/rules/
# Then in prometheus.yml add:
rule_files:
  - /etc/prometheus/rules/arcsight_alerts.yml
sudo systemctl reload prometheus
```

---

## Tuning the Log Regex

ArcSight log formats vary by connector version. If your `agent.log` uses
different patterns, edit the `RE_*` constants near the top of the exporter:

```python
# Example: your log says "CurrentEPS=1234"
RE_EPS_CURRENT = re.compile(r"CurrentEPS[=:\s]+(\d+)", re.IGNORECASE)
```

Run with `--log-level DEBUG` to see which lines are being parsed.

---

## Firewall

Open port 9116 (or whichever you configure) from your Prometheus host:

```bash
sudo firewall-cmd --add-port=9116/tcp --permanent
sudo firewall-cmd --reload
# or:
sudo ufw allow 9116/tcp
```

---

## Grafana Dashboard Queries (PromQL)

```promql
# All connector status
arcsight_connector_up

# EPS by connector
arcsight_connector_eps_current{connector=~".*"}

# Queue utilization heatmap
arcsight_connector_queue_utilization_ratio * 100

# Cache drops rate
rate(arcsight_connector_cache_drops_total[5m])

# JVM heap %
arcsight_connector_jvm_heap_utilization_ratio * 100

# Connectors with errors in last window
arcsight_connector_log_errors_total > 0

# Dead connectors (down for >5m) — use in alert panel
arcsight_connector_up == 0
```

---

## Notes

- The exporter requires **read access** to connector directories and log files.
  Running as `root` is simplest; or add the service user to the `arcsight` group.
- No external Python packages required — stdlib only.
- Log window is the last **500 lines** of `agent.log` (configurable via
  `LOG_TAIL_LINES` at the top of the script).
- Process detection uses `pgrep` and `/proc/<pid>/status` — both available on
  any standard Linux system.
