# Contributing a Specimen

Anyone with a free [account](accounts.md) can share a network with the [Collection](collection.md) at [embody.tools/contribute](https://embody.tools/contribute).

## What you contribute

- **TDN (YAML)** — paste your network as TDN. Export it from Embody with ++ctrl+shift+e++ (whole project) or ++ctrl+alt+e++ (current COMP), or copy a COMP with ++ctrl+shift+c++. Both YAML and legacy JSON are accepted.
- **Title, description, tags.**
- **Category, level, and hardware requirements** (e.g. MediaPipe, Kinect Azure, Audio — or none, for stock TouchDesigner).
- **An optional thumbnail image.**

## The capability scan

Every submission is **scanned for capability surfaces** before it goes live — what the network can touch (filesystem, network, subprocess, and so on). The scan returns a verdict:

- **clean** — no notable capabilities; published normally.
- **flagged** — notable surfaces present; published, but surfaced to moderators and shown on the Specimen.
- **blocked** — disallowed surfaces (e.g. obvious malware); the submission is rejected.

This is what lets the Collection stay browsable and copyable safely. The verdict decides how a Specimen pastes: a **clean** one pastes live and fully working, a **flagged** one pastes disarmed (side-effecting surfaces neutralized, but provably pure value expressions kept so it still renders), and a **blocked** one is rejected (see [The Collection](collection.md)).
