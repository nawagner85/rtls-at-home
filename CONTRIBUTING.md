# Contributing to RTLS@Home

Thanks for helping. This page covers setup, the rules for a pull request, and the licence terms your contribution
comes under.

## Set up and test

Development setup, running the tests (Home Assistant needs Python 3.14 on Linux; a Docker one-liner is provided)
and the repository layout are in [docs/development.md](docs/development.md).

Before you open a pull request:

1. `python -m pytest` passes.
2. `ruff check .` is clean.
3. `python tools/public_scan.py .` is clean. This repository must never contain real MAC addresses, iBeacon UUIDs,
   IP addresses, hostnames or anyone's personal details. Use documentation values in examples: MACs
   `00:00:5E:00:53:xx`, IPs `192.0.2.x`, the domain `example.com`, the UUID `00112233445566778899aabbccddeeff`.
4. New behaviour comes with a test, and user-visible changes come with a line in [CHANGELOG.md](CHANGELOG.md).

## Sign your commits

Every commit needs a Developer Certificate of Origin sign-off: `git commit -s`. It adds a
`Signed-off-by: Your Name <you@example.com>` line, which certifies that you wrote the change or otherwise have the
right to submit it (<https://developercertificate.org/>).

## Licence of your contribution

By contributing, you agree that your contribution is licensed under the project's licences (PolyForm Noncommercial
1.0.0 for code, CC BY-NC-SA 4.0 for documentation), and you also grant the maintainer a perpetual, worldwide,
royalty-free, irrevocable licence to offer your contribution under other terms, including commercial licences. The
maintainer intends to share commercial licensing income with significant contributors; that is a statement of good
faith, not a contract, and any terms are agreed case by case.

Why the grant: RTLS@Home is free for non-commercial use and sold under separate commercial licences (see the README).
Without the grant, code you contribute could only ever ship under the non-commercial terms, and a commercial licence
would have to leave it out.

*This page is not legal advice. If the terms matter to you, have them reviewed before contributing.*
