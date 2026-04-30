# MystNodes SDK Client — Windows amd64 staging

This directory bundles `sdk_client.exe` into the BM (Bandwidth Miner) installer EXE at build
time. The PyInstaller spec `datas` entry stages `SDK/` wholesale, so placing the binary here
is sufficient for the build to pick it up.

## Required artifact

- `sdk_client.exe` — Windows amd64 native binary, obtained from MystNodes B2B contact.

## Why not bundled in this repo

The Linux Docker image `mysteriumnetwork/mystnodes-sdk-client:latest` contains a binary named
`client` (Linux ELF, musl-linked). For Windows amd64 deployment we need the Windows-native
binary, which is NOT in the public Docker image as of Track 4 Phase B (Apr 2026). Operator
MUST obtain this binary from the MystNodes B2B contact before running `build_installer.ps1`.

## Until then

The build will fail at binary staging with a missing-file error. That is the intended gate —
we do not ship installers without the SDK binary.

## Track 4 Phase B reference

`/c/tmp/track4_recon_1777471988/TRACK4_RECON_VERDICT.md` — token validation against the Linux
binary confirmed VALID against `proxy.mystnodes.com:443` QUIC.
