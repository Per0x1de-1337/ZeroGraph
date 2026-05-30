# HuggingFace Transformers unsafe torch.load (duplicate report)

| Field | Value |
|-------|-------|
| Huntr | https://huntr.com/bounties/3c77bb97-e493-493d-9a88-c57f5c536485 |
| CVE | CVE-2026-1839 |
| CWE | CWE-502 (Unsafe deserialization) |
| Target | huggingface/transformers |
| Severity | High |
| Zerograph status | **Reported — closed as duplicate** |

## Summary

`Trainer._load_rng_state()` calls `torch.load()` without `weights_only=True`. With
PyTorch &lt; 2.6, `safe_globals()` is a no-op, so a malicious `rng_state.pth` in a
checkpoint can execute arbitrary code when training is resumed.

## Agent tooling

- `zg_trace_sinks` on deserialization APIs
- `zg_scan_fmt_string` / control-flow review around checkpoint load paths

## Disclosure outcome

Submitted to huntr following agent-generated PoC notes. Closed as **duplicate**;
public CVE-2026-1839 and fix in transformers v5.0.0rc3+.
