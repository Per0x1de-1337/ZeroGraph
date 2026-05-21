# MLflow webhook SSRF (duplicate report)

| Field | Value |
|-------|-------|
| Huntr | https://huntr.com/bounties/04ef100d-06b5-4a70-95b1-b7be23aa8150 |
| CVE | CVE-2026-2393 |
| CWE | CWE-918 (SSRF) |
| Target | mlflow/mlflow |
| Severity | High (CVSS 7.1) |
| Zerograph status | **Reported — closed as duplicate** |

## Summary

The ZeroGraph pentest agent flagged server-side request forgery in MLflow webhook
creation: `_create_webhook()` stores a user-controlled URL and `_send_webhook_request()`
issues HTTP POST requests to it without validation. An authenticated attacker can
probe internal services, cloud metadata endpoints, or arbitrary external hosts.

## Agent tooling

- `zg_trace_sinks` / `zg_trace_flows` on webhook handler code paths
- `zg_scan_shell_injection` and network-adjacent heuristics where applicable

## Disclosure outcome

We submitted this finding to huntr. The program marked it **duplicate** (prior public
or overlapping report). The issue is tracked publicly as CVE-2026-2393 and fixed in
MLflow 3.9.0.
