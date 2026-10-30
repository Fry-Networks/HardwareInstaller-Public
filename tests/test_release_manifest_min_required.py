"""Regression tests for the ``min_required`` force-update floor at release time.

R3: ``build_cli._upload_github_release`` rebuilt ``fryhub_version.json`` from a
fresh dict and wrote ``min_required`` ONLY when ``--min-required`` was passed.
A release cut without that flag therefore republished the manifest with no
force-update floor, silently -- even though the committed manifest carries a
``min_required`` value and every client reads it to decide whether to force an
update (see ``core/hub_updater.py``).

These tests pin the invariant: cutting a release without the flag cannot
silently produce a manifest that is missing ``min_required``.

Run: python -m pytest tests/test_release_manifest_min_required.py -v
     python -m unittest tests.test_release_manifest_min_required -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import build_cli


def _prior_manifest(min_required):
    """A previously published manifest, as it sits in the repo root."""
    manifest = {
        "manifest_version": "1.0.0",
        "hub_version": "4.0.31",
        "setup_url": (
            "https://github.com/Fry-Networks/HardwareInstaller-Public/"
            "releases/download/v4.0.31/FryHubSetup-4.0.31.exe"
        ),
        "setup_sha256": "0" * 64,
    }
    if min_required is not None:
        manifest["min_required"] = min_required
    return manifest


class _ReleaseHarness(unittest.TestCase):
    def _cut_release(self, *, prior, min_required=None):
        """Run ``_upload_github_release`` in an isolated temp manifest dir.

        ``prior`` is the manifest that already exists on disk (or ``None``).
        Returns ``(resulting_manifest, captured_stderr)``.
        """
        tmp = tempfile.mkdtemp(prefix="r3-manifest-")
        tmp_path = Path(tmp)
        exe = tmp_path / "FryHubSetup-4.0.32.exe"
        exe.write_bytes(b"fake exe bytes")

        if prior is not None:
            (tmp_path / "fryhub_version.json").write_text(
                json.dumps(prior, indent=2) + "\n", encoding="utf-8"
            )

        fake_self = tmp_path / "build_cli.py"
        fake_self.write_text("")
        gh_ok = mock.Mock(returncode=0, stdout="", stderr="")

        stderr_buf = io.StringIO()
        with mock.patch.object(build_cli, "__file__", str(fake_self)), \
                mock.patch.object(build_cli.subprocess, "run", return_value=gh_ok), \
                redirect_stderr(stderr_buf):
            build_cli._upload_github_release(
                "4.0.32", exe, min_required=min_required
            )

        result = json.loads(
            (tmp_path / "fryhub_version.json").read_text(encoding="utf-8")
        )
        return result, stderr_buf.getvalue()


class MinRequiredCarryForwardTest(_ReleaseHarness):
    def test_release_without_flag_carries_forward_previous_min_required(self):
        """A cut without --min-required must keep the previous floor, not drop it."""
        prior = _prior_manifest("4.0.20")
        result, _ = self._cut_release(prior=prior, min_required=None)
        self.assertIn(
            "min_required", result,
            "manifest lost min_required: a release cut without the flag "
            "silently removed the force-update floor",
        )
        self.assertEqual("4.0.20", result["min_required"])

    def test_explicit_flag_overrides_previous_min_required(self):
        """An explicit --min-required still wins over the carried-forward value."""
        prior = _prior_manifest("4.0.20")
        result, _ = self._cut_release(prior=prior, min_required="4.0.31")
        self.assertEqual("4.0.31", result["min_required"])

    def test_explicit_flag_present_with_no_prior_manifest(self):
        """Explicit flag writes min_required even when no prior manifest existed."""
        result, _ = self._cut_release(prior=None, min_required="4.0.31")
        self.assertEqual("4.0.31", result["min_required"])


class MinRequiredOmissionIsLoudTest(_ReleaseHarness):
    def test_omission_with_no_recoverable_value_warns_on_stderr(self):
        """With nothing to carry forward, dropping the floor must NOT be silent."""
        result, err = self._cut_release(prior=None, min_required=None)
        # Nothing to recover -> field legitimately absent ...
        self.assertNotIn("min_required", result)
        # ... but the operator must be told, not left to discover it in prod.
        self.assertIn("WARNING", err)
        self.assertIn("min_required", err)

    def test_omission_when_prior_has_no_floor_warns_on_stderr(self):
        """A prior manifest that itself lacks the floor must also warn, not stay silent."""
        prior = _prior_manifest(None)
        result, err = self._cut_release(prior=prior, min_required=None)
        self.assertNotIn("min_required", result)
        self.assertIn("WARNING", err)
        self.assertIn("min_required", err)


if __name__ == "__main__":
    unittest.main()
