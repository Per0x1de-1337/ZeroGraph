# Triton Accept-Encoding integer overflow DoS (duplicate report)

| Field | Value |
|-------|-------|
| Huntr | https://huntr.com/bounties/541b64b9-6bd2-477d-b9f0-c66575e20848 |
| CWE | CWE-190 (Integer overflow) |
| Target | triton-inference-server/server |
| Severity | High (CVSS 7.5) |
| Zerograph status | **Reported — closed as duplicate** |

## Summary

Static analysis highlighted integer overflow when parsing the `Accept-Encoding` header
on `POST /v2/models/<model>/generate` (`HandleGenerate` → `GetResponseCompressionType`
in `src/http_server.cc`). A malicious header value can crash the server or cause
denial of service.

## Agent tooling

- `zg_scan_int_overflow` on compression / buffer-size handling
- `zg_file_range` and `zg_symbol_source` for handler review

## Disclosure outcome

Submitted to huntr after agent triage. Resolved as **duplicate** of an existing report.
Awaiting upstream fix per huntr program status.
