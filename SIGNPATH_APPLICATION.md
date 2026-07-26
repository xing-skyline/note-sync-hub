# SignPath Foundation application draft

This file is a copy-ready draft for the project maintainer. It contains no
credentials or private personal information. The application must be submitted
by the maintainer after confirming the information is still accurate.

## Project details

- **Project name:** Note Sync Hub
- **Repository:** <https://github.com/xing-skyline/note-sync-hub>
- **License:** GNU General Public License v3.0 or later (GPL-3.0-or-later)
- **License file:** <https://github.com/xing-skyline/note-sync-hub/blob/main/LICENSE>
- **Code signing policy:** <https://github.com/xing-skyline/note-sync-hub/blob/main/CODE_SIGNING_POLICY.md>
- **Current release:** <https://github.com/xing-skyline/note-sync-hub/releases/tag/v1.2.0>

## Copy-ready application text

Note Sync Hub is a GPL-3.0-or-later open-source Windows desktop application for
safely synchronizing Markdown notes among Joplin, Obsidian, and SiYuan. Users
select the participating applications, inspect a complete read-only preview,
and explicitly confirm before any synchronization changes are applied.

The project currently distributes a portable, single-file Windows executable
built with PyInstaller through public GitHub Releases. The current v1.2.0
executable is unsigned. We are applying for SignPath Foundation code signing so
future releases can provide a verifiable binary origin and reduce
unknown-publisher warnings for users without making unsupported claims about
immediate Microsoft SmartScreen reputation.

The source code, Windows build script, automated tests, GitHub Actions workflow,
and release history are public. The formal unsigned input for signing will be
built from the public repository on a GitHub-hosted Windows runner. The workflow
runs the unit tests and Ruff, builds the PyInstaller executable, verifies its
Windows product and version metadata, and uploads the unsigned artifact. Each
formal signing request will require manual approval by the designated signing
approver. Final SHA-256 checksums will be calculated only after signing.

Note Sync Hub has no telemetry, analytics, advertising, bundled software,
hidden data collection, or closed-source commercial components. It does not
send note contents, credentials, configuration, or usage data to the project
maintainer. It accesses only the Joplin and SiYuan API endpoints and local
Obsidian directory that the user explicitly configures and actively uses.

Certificate private keys will remain protected by the signing service's HSM and
will never be stored in the repository. No API token will be committed to
source code, documentation, artifacts, or workflow logs. Project roles and the
review, build, signing-approval, privacy, and key-management controls are
documented in the public Code signing policy.
