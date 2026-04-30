"""DEPRECATED — myst.exe / TequilAPI provisioning was the wrong product for BM.

BM revenue model uses MystNodes SDK Client (sdk_client.exe), not the regular Mysterium dVPN
node. See core/mystnodes_sdk_provisioning.py for the replacement.

This shim exists only to surface stray imports as hard errors. All public functions raise
ImportError at call time. The full historical implementation is preserved on branch
track-3/tos-gate (commits past 98d412d). Branch track-4/sdk-client-pivot is the canonical
post-pivot architecture for BM.

If you imported this module by accident: switch to core.mystnodes_sdk_provisioning.
"""

_DEPRECATION_MSG = (
    "core.mysterium_provisioning is deprecated post Track 4 pivot. "
    "BM uses sdk_client.exe via core.mystnodes_sdk_provisioning. "
    "See branch track-3/tos-gate for the historical myst.exe implementation."
)


def provision_mysterium_at_install(*args, **kwargs):
    raise ImportError(_DEPRECATION_MSG)


def cleanup_mysterium_on_failure(*args, **kwargs):
    raise ImportError(_DEPRECATION_MSG)
