# Apache Airflow log symlink arbitrary file read (duplicate report)

| Field | Value |
|-------|-------|
| Huntr | https://huntr.com/bounties/8554ec70-2095-44d7-819c-1f181da22166 |
| CWE | CWE-22 (Path traversal / symlink follow) |
| Target | apache/airflow |
| Severity | Medium (CVSS 6.5) |
| Zerograph status | **Reported — closed as duplicate** |

## Summary

The agent identified information disclosure in `FileTaskHandler._read_from_local`:
log globbing opens files under the task log directory without rejecting symlinks, so a
DAG author can point a log entry at sensitive paths (`airflow.cfg`, keys, `/etc/passwd`).

## Agent tooling

- `zg_trace_flows` from DAG upload paths into log read APIs
- `zg_list_files` / `zg_file_range` for handler confirmation

## Disclosure outcome

Filed on huntr; triage closed the submission as **duplicate**. Issue remains open on
the main branch per huntr metadata at time of report.
