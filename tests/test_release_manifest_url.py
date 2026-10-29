"""Regression tests for the Fry Hub auto-update manifest URL written at release time.

BUG-008: ``build_cli._upload_github_release`` embedded the retired org
``Fry-Foundation`` in ``setup_url``, while the committed ``fryhub_version.json``
and ``build_installer.ps1`` use the live org ``Fry-Networks``.  Every installed
Fry Hub downloads that URL verbatim, so a release cut by the tool would 404.

Run: python -m pytest tests/test_release_manifest_url.py -v
     python -m unittest tests.test_release_manifest_url -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import build_cli

LIVE_REPO = "Fry-Networks/HardwareInstaller-Public"
DEAD_ORG = "Fry-Foundation"

# Functional Python sources that construct user-facing / client-fetched URLs.
URL_SOURCES = ("build_cli.py", "installer_main.py")


class UploadGithubReleaseManifestUrlTest(unittest.TestCase):
    def test_setup_url_targets_live_org(self):
        """The manifest the release tool writes must point at the live org."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "FryHubSetup-4.0.31.exe"
            exe.write_bytes(b"fake exe bytes")

            # Point Path(__file__).parent at the temp dir so the tool writes its
            # manifest there instead of over the real repo file, and stub out the
            # `gh release create` call so no network/release happens.
            fake_self = tmp_path / "build_cli.py"
            fake_self.write_text("")
            gh_ok = mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(build_cli, "__file__", str(fake_self)), \
                    mock.patch.object(build_cli.subprocess, "run", return_value=gh_ok):
                build_cli._upload_github_release("4.0.31", exe)

            manifest = json.loads(
                (tmp_path / "fryhub_version.json").read_text(encoding="utf-8")
            )

        expected = (
            f"https://github.com/{LIVE_REPO}/releases/download/"
            "v4.0.31/FryHubSetup-4.0.31.exe"
        )
        self.assertEqual(expected, manifest["setup_url"])
        self.assertNotIn(DEAD_ORG, manifest["setup_url"])

    def test_tool_org_matches_committed_manifest_org(self):
        """The org in the tool's URL must match the org in the shipped manifest."""
        committed = json.loads(
            (REPO_ROOT / "fryhub_version.json").read_text(encoding="utf-8")
        )
        self.assertIn(
            f"https://github.com/{LIVE_REPO}/", committed["setup_url"]
        )


class NoDeadOrgLiteralTest(unittest.TestCase):
    def test_functional_url_sources_have_no_dead_org(self):
        """No functional Python URL may reference the retired org."""
        offenders = []
        for name in URL_SOURCES:
            text = (REPO_ROOT / name).read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if DEAD_ORG in line:
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
        self.assertEqual([], offenders, "retired org still referenced:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
