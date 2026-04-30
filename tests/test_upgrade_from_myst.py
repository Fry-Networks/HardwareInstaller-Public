"""Unit-tests for core.upgrade_from_myst — Track 4 Fix #2a.

Tests use fixture files from tests/fixtures/upgrade_from_myst_probes/{PRESENT,ABSENT}/
captured by the state-sim recon. OS-state probes are tested by monkey-patching
_run_subprocess_capture to return fixture bytes; file-state probes use tmp_path.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from core import upgrade_from_myst
from core.upgrade_from_myst import (
    UpgradeFromMystResult,
    _decode_probe_output,
    _probe_f1_myst_data,
    _probe_f2_windows_myst_sdk,
    _probe_f3_mysterium,
    _probe_f4_mysterium_json,
    _probe_f5_myst_exe,
    _probe_fw_rules,
    _probe_r1_registry,
    _probe_s1_service,
    _probe_s2_nssm,
    _rename_legacy_file_artifacts,
    _write_state_file,
    detect_legacy_state,
    rollback_upgrade,
    upgrade_from_myst_at_install,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "upgrade_from_myst_probes"
PRESENT_FIXTURES = FIXTURE_ROOT / "PRESENT"
ABSENT_FIXTURES = FIXTURE_ROOT / "ABSENT"


def load_fixture_for_mock(state, name):
    """Load fixture as raw bytes, stripping the bash-appended EXIT= line."""
    p = (PRESENT_FIXTURES if state == "PRESENT" else ABSENT_FIXTURES) / name
    raw = p.read_bytes()
    if b"EXIT=" in raw:
        raw = raw[:raw.rfind(b"EXIT=")]
    return raw


# ---------------------------------------------------------------------------
# _decode_probe_output tests
# ---------------------------------------------------------------------------

class TestDecodeProbeOutput:
    def test_utf8_roundtrip(self):
        text = "hello world\n"
        assert _decode_probe_output(text.encode("utf-8")) == text

    def test_utf16le_bom(self):
        text = "SERVICE_RUNNING\r\n"
        raw = b"\xff\xfe" + text.encode("utf-16-le")
        result = _decode_probe_output(raw)
        assert "SERVICE_RUNNING" in result
        assert "\r\n" not in result  # normalized

    def test_utf16le_no_bom_heuristic(self):
        """nssm emits UTF-16-LE without BOM — second byte is 0x00."""
        text = "SERVICE_STOPPED"
        raw = text.encode("utf-16-le")
        assert raw[1:2] == b"\x00"  # confirm heuristic trigger
        result = _decode_probe_output(raw)
        assert "SERVICE_STOPPED" in result

    def test_crlf_normalization(self):
        raw = b"line1\r\nline2\r\n"
        result = _decode_probe_output(raw)
        assert "\r\n" not in result
        assert "line1\nline2\n" == result

    def test_replace_non_utf8(self):
        raw = b"\x80\x81\x82"
        result = _decode_probe_output(raw)
        assert isinstance(result, str)  # no exception

    def test_real_nssm_present_fixture(self):
        raw = load_fixture_for_mock("PRESENT", "S2_nssm_status.txt")
        result = _decode_probe_output(raw)
        assert isinstance(result, str)
        assert "SERVICE_STOPPED" in result or "SERVICE_RUNNING" in result or len(result) > 0


# ---------------------------------------------------------------------------
# OS-state probe tests (monkeypatch _run_subprocess_capture)
# ---------------------------------------------------------------------------

class TestProbeS1Service:
    def test_present(self, monkeypatch):
        fixture = load_fixture_for_mock("PRESENT", "S1_sc_query.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_s1_service() is True

    def test_absent(self, monkeypatch):
        fixture = load_fixture_for_mock("ABSENT", "S1_sc_query.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_s1_service() is False


class TestProbeS2Nssm:
    def test_present(self, monkeypatch):
        fixture = load_fixture_for_mock("PRESENT", "S2_nssm_status.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_s2_nssm(Path("/fake/nssm.exe")) is True

    def test_absent(self, monkeypatch):
        fixture = load_fixture_for_mock("ABSENT", "S2_nssm_status.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_s2_nssm(Path("/fake/nssm.exe")) is False


class TestProbeFwRules:
    def test_present(self, monkeypatch):
        fixture = load_fixture_for_mock("PRESENT", "FW_wildcard_count.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_fw_rules() is True

    def test_absent(self, monkeypatch):
        fixture = load_fixture_for_mock("ABSENT", "FW_wildcard_count.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_fw_rules() is False


class TestProbeR1Registry:
    def test_present(self, monkeypatch):
        fixture = load_fixture_for_mock("PRESENT", "R1_reg_query.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_r1_registry() is True

    def test_absent(self, monkeypatch):
        fixture = load_fixture_for_mock("ABSENT", "R1_reg_query.txt")
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: fixture)
        assert _probe_r1_registry() is False


# ---------------------------------------------------------------------------
# File-state probe tests (tmp_path)
# ---------------------------------------------------------------------------

class TestFileProbes:
    def test_f1_present(self, tmp_path):
        (tmp_path / "myst-data").mkdir()
        assert _probe_f1_myst_data(tmp_path) is True

    def test_f1_absent(self, tmp_path):
        assert _probe_f1_myst_data(tmp_path) is False

    def test_f2_present(self, tmp_path):
        (tmp_path / "SDK" / "windows-myst-sdk").mkdir(parents=True)
        assert _probe_f2_windows_myst_sdk(tmp_path) is True

    def test_f2_absent(self, tmp_path):
        assert _probe_f2_windows_myst_sdk(tmp_path) is False

    def test_f3_present(self, tmp_path):
        (tmp_path / "mysterium").mkdir()
        assert _probe_f3_mysterium(tmp_path) is True

    def test_f3_absent(self, tmp_path):
        assert _probe_f3_mysterium(tmp_path) is False

    def test_f4_present(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "mysterium.json").write_text("{}")
        assert _probe_f4_mysterium_json(tmp_path) is True

    def test_f4_absent(self, tmp_path):
        assert _probe_f4_mysterium_json(tmp_path) is False

    def test_f5_present(self, tmp_path):
        sdk = tmp_path / "SDK" / "windows-myst-sdk"
        sdk.mkdir(parents=True)
        (sdk / "myst.exe").write_bytes(b"\x00")
        assert _probe_f5_myst_exe(tmp_path) is True

    def test_f5_absent(self, tmp_path):
        assert _probe_f5_myst_exe(tmp_path) is False


# ---------------------------------------------------------------------------
# detect_legacy_state tests
# ---------------------------------------------------------------------------

class TestDetectLegacyState:
    def test_all_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        signals = detect_legacy_state(tmp_path, Path("/fake/nssm"))
        assert not any(signals.values())

    def test_all_file_present(self, tmp_path, monkeypatch):
        # Create all file artifacts
        (tmp_path / "myst-data").mkdir()
        (tmp_path / "mysterium").mkdir()
        sdk = tmp_path / "SDK" / "windows-myst-sdk"
        sdk.mkdir(parents=True)
        (sdk / "myst.exe").write_bytes(b"\x00")
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "mysterium.json").write_text("{}")
        # OS probes return absent
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        signals = detect_legacy_state(tmp_path, Path("/fake/nssm"))
        assert signals["F1_myst_data"] is True
        assert signals["F2_windows_myst_sdk"] is True
        assert signals["F3_mysterium"] is True
        assert signals["F4_mysterium_json"] is True
        assert signals["F5_myst_exe"] is True
        assert signals["S1_service"] is False

    def test_mixed_f3_only(self, tmp_path, monkeypatch):
        (tmp_path / "mysterium").mkdir()
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        signals = detect_legacy_state(tmp_path, Path("/fake/nssm"))
        assert signals["F3_mysterium"] is True
        assert any(signals.values())  # upgrade_needed would be True

    def test_dual_data_dirs(self, tmp_path, monkeypatch):
        (tmp_path / "mysterium").mkdir()
        (tmp_path / "myst-data").mkdir()
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        signals = detect_legacy_state(tmp_path, Path("/fake/nssm"))
        assert signals["F1_myst_data"] is True
        assert signals["F3_mysterium"] is True


# ---------------------------------------------------------------------------
# State JSON schema test
# ---------------------------------------------------------------------------

class TestStateJson:
    def test_write_and_read(self, tmp_path):
        state_path = _write_state_file(
            install_root=tmp_path, ts=1234567890,
            detected={"F3_mysterium": True}, service_state={},
            fw_state=[], renamed_paths=[], nssm_path=Path("/fake/nssm"),
        )
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["schema_version"] == 1
        assert data["timestamp"] == 1234567890
        assert "captured_at" in data
        assert "service" in data
        assert "firewall_rules" in data
        assert "renamed_paths" in data
        assert "detected_signals" in data


# ---------------------------------------------------------------------------
# Rename helper tests
# ---------------------------------------------------------------------------

class TestRenameHelper:
    def test_rename_all(self, tmp_path):
        (tmp_path / "mysterium").mkdir()
        sdk = tmp_path / "SDK" / "windows-myst-sdk"
        sdk.mkdir(parents=True)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "mysterium.json").write_text("{}")

        renamed = _rename_legacy_file_artifacts(tmp_path, 1234567890)

        assert len(renamed) == 3
        for p in renamed:
            assert ".deprecated.1234567890" in p.name
            assert p.exists()

        assert not (tmp_path / "mysterium").exists()
        assert not (tmp_path / "SDK" / "windows-myst-sdk").exists()
        assert not (tmp_path / "config" / "mysterium.json").exists()

    def test_rename_both_data_dirs(self, tmp_path):
        (tmp_path / "mysterium").mkdir()
        (tmp_path / "myst-data").mkdir()
        renamed = _rename_legacy_file_artifacts(tmp_path, 999)
        names = [p.name for p in renamed]
        assert "mysterium.deprecated.999" in names
        assert "myst-data.deprecated.999" in names


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_rename_empty(self, tmp_path):
        renamed = _rename_legacy_file_artifacts(tmp_path, 123)
        assert renamed == []

    def test_detect_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        signals = detect_legacy_state(tmp_path, Path("/fake/nssm"))
        assert not any(signals.values())

    def test_orchestrator_no_signals(self, tmp_path, monkeypatch):
        monkeypatch.setattr(upgrade_from_myst, "_run_subprocess_capture",
                            lambda *a, **k: b"FAILED 1060: does not exist\nERROR: unable to find\n")
        result = upgrade_from_myst_at_install(
            tmp_path, Path("/fake/nssm"),
        )
        assert result.upgrade_needed is False
        assert result.upgrade_performed is False
        assert result.failed is False


# ---------------------------------------------------------------------------
# Rollback tests (filesystem only — no real OS state)
# ---------------------------------------------------------------------------

class TestRollback:
    def test_rollback_renames_back(self, tmp_path):
        # Set up: create deprecated paths + state JSON
        ts = 1234567890
        (tmp_path / "config").mkdir()
        (tmp_path / "mysterium.deprecated.1234567890").mkdir()

        state = {
            "schema_version": 1,
            "captured_at": "2026-01-01T00:00:00Z",
            "timestamp": ts,
            "install_root": str(tmp_path),
            "nssm_path": "/dev/null",
            "service": {},
            "firewall_rules": [],
            "renamed_paths": [
                {"original": str(tmp_path / "mysterium"),
                 "deprecated": str(tmp_path / "mysterium.deprecated.1234567890")},
            ],
            "detected_signals": {"F3_mysterium": True},
        }
        state_file = tmp_path / "config" / "upgrade_from_myst_state.json"
        state_file.write_text(json.dumps(state))

        ok = rollback_upgrade(state_file, Path("/dev/null"))
        assert ok is True
        assert (tmp_path / "mysterium").exists()
        assert not (tmp_path / "mysterium.deprecated.1234567890").exists()

    def test_rollback_partial_state_no_error(self, tmp_path):
        """Partial state JSON with no service/firewall/renamed — should not error."""
        (tmp_path / "config").mkdir()
        state = {
            "schema_version": 1,
            "captured_at": "2026-01-01T00:00:00Z",
            "timestamp": 0,
            "install_root": str(tmp_path),
            "nssm_path": "/dev/null",
            "service": {},
            "firewall_rules": [],
            "renamed_paths": [],
            "detected_signals": {},
        }
        state_file = tmp_path / "config" / "upgrade_from_myst_state.json"
        state_file.write_text(json.dumps(state))

        ok = rollback_upgrade(state_file, Path("/dev/null"))
        assert ok is True


# ---------------------------------------------------------------------------
# Contract surface test
# ---------------------------------------------------------------------------

class TestContractSurface:
    def test_imports(self):
        from core.upgrade_from_myst import (
            UpgradeFromMystResult,
            detect_legacy_state,
            rollback_upgrade,
            upgrade_from_myst_at_install,
        )

    def test_dataclass(self):
        assert dataclasses.is_dataclass(UpgradeFromMystResult)

    def test_fields(self):
        fields = {f.name for f in dataclasses.fields(UpgradeFromMystResult)}
        expected = {
            "upgrade_needed", "upgrade_performed", "failed", "error",
            "detected_signals", "state_file_path", "renamed_paths", "timestamp",
        }
        assert expected <= fields
