# Keras get_file zip slip (duplicate report)


| Field            | Value                                                                                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Huntr            | [https://huntr.com/bounties/c8bcd0aa-35e6-4d96-a52b-e2bab8958502](https://huntr.com/bounties/c8bcd0aa-35e6-4d96-a52b-e2bab8958502) |
| CWE              | CWE-23 (Relative path traversal)                                                                                                   |
| Target           | keras-team/keras                                                                                                                   |
| Severity         | Informational on huntr (zip slip)                                                                                                  |
| Zerograph status | **Reported — closed as duplicate**                                                                                                 |


## Summary

`keras.utils.get_file(..., extract=True)` validates archive members against the process
CWD instead of the extraction target, allowing `../../` members to write outside the
intended directory (arbitrary file write under CWD).

## Agent tooling

- `zg_scan_bounds_guard` and archive-extraction call sites
- `zg_run_graph_script` for path join / extract helpers in `file_utils.py`

## Disclosure outcome

Reported via huntr after agent review. Marked **duplicate**; upstream fix landed on
keras (see huntr report — fixed by maintainer).