# Network Automation & Monitoring Lab

A learning project that progresses from basic device reachability checks to SSH-based MikroTik inventory and Prometheus/Grafana monitoring.

## Architecture

**MikroTik CHR → Python/Netmiko exporter → Prometheus → Grafana → Telegram**

The exporter connects to RouterOS over SSH, reads interface state and per-interface traffic rates, and serves metrics on port `8000`. Prometheus scrapes the endpoint and evaluates the included alert rules. Grafana can visualize Prometheus data and route alert notifications through a Telegram contact point configured in Grafana.

## Repository contents

- `monitor.py` — ping reachability, latency, and packet-loss report for a sample device list.
- `devices.py` — sample targets using documentation-only IP ranges. Replace them with lab targets before running.
- `network_ssh.py` — read-only RouterOS identity, interface, and IP-address inventory.
- `exporter.py` — Prometheus exporter for device reachability, interface state, running-interface count, and traffic rates.
- `prometheus.yml`, `alert_rules.yml` — scrape job and device/interface-down Prometheus alert rules.
- `test_interface_parser.py` — small example demonstrating parsing of representative RouterOS interface output.

## Requirements

- Python 3.10 or newer
- A reachable MikroTik CHR or RouterOS device with SSH enabled
- Prometheus and Grafana for dashboards and alert notifications

Install Python dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configure credentials

Create a dedicated RouterOS monitoring account with only the permissions needed to read interface and IP information. Restrict SSH access to the machine running the exporter.

Copy `.env.example` to `.env` as a private reference, then set the values in your shell before starting a script. The application reads environment variables; it does not load `.env` automatically.

```sh
export ROUTER_HOST='your-router-address'
export ROUTER_USERNAME='your-monitoring-user'
export ROUTER_PASSWORD='your-new-router-password'
```

Never commit `.env` or place real credentials in source files. A lab password was exposed in the project/chat history; rotate it before running or publishing. If a credential was committed in an earlier Git revision, rotate it and clean the repository history before making it public.

## Run the project

For the basic ping report, edit `devices.py` to use your own lab addresses and run:

```sh
python monitor.py
```

For RouterOS inventory:

```sh
python network_ssh.py
```

For the Prometheus exporter:

```sh
python exporter.py
```

The exporter listens on `METRICS_PORT` (default `8000`) and polls every `POLL_INTERVAL_SECONDS` (default `10`). Check `http://localhost:8000/metrics` locally. Prometheus must be able to reach that endpoint; change the target in `prometheus.yml` if Prometheus runs in another container or host. Do not expose the metrics port to the public internet.

## Metrics

| Metric | Meaning |
|---|---|
| `network_device_up` | `1` when the SSH connection succeeds; `0` for authentication or connection failure |
| `network_interface_up{interface="…"}` | `1` when RouterOS reports the running flag; otherwise `0` |
| `network_interfaces_up` | Number of running interfaces in the latest successful collection |
| `network_interface_rx_bps{interface="…"}` | Latest received bit rate reported by RouterOS |
| `network_interface_tx_bps{interface="…"}` | Latest transmitted bit rate reported by RouterOS |

The earlier code declared an RX error-rate metric but did not collect it, so it is not included in the current exporter.

## Prometheus, Grafana, and Telegram

Start Prometheus with `prometheus.yml` and `alert_rules.yml`. In Grafana, add Prometheus as a data source and create or import a dashboard using the metrics above. Configure the Telegram bot token and chat destination as a Grafana contact point/secret, then connect it to an appropriate notification policy. Do not commit Grafana exports containing tokens, chat IDs, or other sensitive values.

The supplied Prometheus rules cover device-down and interface-down conditions. The prior conversation reported testing alerts, but the checked-in project did not include dashboard or Grafana alert/contact-point exports. Verify firing and recovery in your own environment before claiming those exports are included or treating the configuration as portable.

## Testing and evidence

The parser example uses representative RouterOS output and is useful as a manual reference. The prior conversation also recorded a live `/metrics` check showing running interfaces. It did not establish from the available files a repeatable end-to-end alert test artifact. Avoid disabling the SSH management interface to test; use a safe lab interface or a separate simulated metric source.

## Limitations

- This is a learning lab, not a hardened production exporter.
- Traffic is queried for each running interface in sequence; polling time grows with interface count.
- Interface labels that disappear from RouterOS are not currently removed from Prometheus client state until exporter restart.
- A per-interface traffic-query error is logged while interface-state collection continues; review logs and validate output against your RouterOS version.
- The sample ping script parses common macOS/Linux `ping` output and may need adjustment on other platforms.
- The README and configuration cannot guarantee alert delivery without the actual Grafana data source, contact point, and notification policy.

## GitHub publication checklist

- Rotate exposed RouterOS credentials; use a dedicated least-privilege account.
- Confirm `.env` is ignored and scan tracked files and Git history for secrets.
- Review exported Grafana files and screenshots for tokens, chat IDs, host addresses, and personal information.
- Add a license if you want others to reuse the project.
- Test the setup from a clean clone and record the tested software versions.
