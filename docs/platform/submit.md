# Submitting a Specimen

Anyone with a free [account](accounts.md) can share a network with the [Collection](collection.md) at [embody.tools/submit](https://embody.tools/submit).

## What you submit

- **TDN (YAML)** — paste your network as TDN. Export it from Embody with ++ctrl+shift+e++ (whole project) or ++ctrl+alt+e++ (current COMP), or copy a COMP with ++ctrl+shift+c++. Both YAML and legacy JSON are accepted.
- **Title, description, tags.**
- **Category, difficulty, and hardware requirements** (e.g. MediaPipe, Kinect Azure, Audio — or none, for stock TouchDesigner).
- **An optional thumbnail image.**

## The capability scan

Every submission is **scanned for capability surfaces** before it goes live — what the network can touch (filesystem, network, subprocess, and so on). The scan returns a verdict:

- **clean** — no notable capabilities; published normally.
- **flagged** — notable surfaces present; published, but surfaced to moderators and shown on the Specimen.
- **blocked** — disallowed surfaces (e.g. obvious malware); the submission is rejected.

This is what lets the Collection stay browsable and copyable safely — and why community Specimens paste **inert** by default (see [The Collection](collection.md)).
