# upgrade_from_myst_probes — fixture corpus

Captured by Track 4 Fix #2 state-sim recon. Used by `tests/test_upgrade_from_myst.py` to validate parser behavior in `core/upgrade_from_myst.py` against both PRESENT and ABSENT states without requiring live OS state during CI.

## Layout

| File | Signal | Probe command |
|------|--------|---------------|
| F1_myst_data_ls.txt + F1_dir_exists.txt | F1 myst-data/ | `ls -la <path>/myst-data` + dir-exists check |
| F2_windows_myst_sdk_ls.txt + F2_dir_exists.txt | F2 SDK/windows-myst-sdk/ | `ls -la <path>/SDK/windows-myst-sdk` + dir-exists check |
| F3_mysterium_ls.txt + F3_dir_exists.txt | F3 mysterium/ | `ls -la <path>/mysterium` + dir-exists check |
| F4_mysterium_json_ls.txt + F4_file_exists.txt + F4_size.txt | F4 config/mysterium.json | `ls -la` + file-exists + size |
| F5_myst_exe_ls.txt + F5_file_exists.txt + F5_md5.txt | F5 myst.exe binary | `ls -la` + file-exists + md5 |
| S1_sc_query.txt | S1 MysteriumNode service | `sc.exe query MysteriumNode` |
| S2_nssm_status.txt | S2 nssm-managed MysteriumNode | `nssm status MysteriumNode` |
| FW_specific_rules.txt | FW1-3 named rules | `Get-NetFirewallRule -DisplayName 'MysteriumNode-*'` |
| FW_wildcard_count.txt + FW_wildcard_list.txt | FW wildcard fallback | `Get-NetFirewallRule -DisplayName '*Mysterium*'` count + list |
| R1_reg_query.txt | R1 registry key | `reg query HKLM\SYSTEM\CurrentControlSet\Services\MysteriumNode` |

Each file contains raw stdout+stderr from the probe followed by `EXIT=<code>` on the last line. Parsers in `core/upgrade_from_myst.py` must consume this raw form, not summaries.

## PRESENT state captured against

- File signals: restored pre-pivot state at /c/ProgramData/FryNetworks/miner-BM/ (from /tmp/v4_retry3_1777391979/miner-BM_full.tar.gz, md5 2a269c53620bd1c46295d8214bb79e2b)
- F1 myst-data/: dummy mkdir in stage dir (pre-pivot tarball lacks myst-data/)
- OS state: dummy MysteriumNode service via nssm + 3 dummy firewall rules + auto-created registry key

## ABSENT state captured against

- Empty reference directory + post-teardown OS state

## Encoding notes for parsers

Files captured directly from Windows tool output. Parsers in `core/upgrade_from_myst.py` and tests in `tests/test_upgrade_from_myst.py` must:
- Read with `encoding='utf-8', errors='replace'` to handle any non-UTF-8 bytes (some Windows tools emit code-page-encoded output)
- Normalize line endings (`\r\n` -> `\n`) before regex/substring matching
- Treat `nssm status` output specially — older nssm versions emit UTF-16-LE; newer versions emit UTF-8. Detect via BOM or first-byte heuristic
- Use substring/regex matching (e.g., `"FAILED 1060" in content`) rather than line-equality matching, since whitespace and column-width may vary across Windows versions

## Regeneration

Re-run Track 4 Fix #2 state-sim recon prompt.
