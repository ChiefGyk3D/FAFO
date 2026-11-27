# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub security advisories:
open the repository's Security tab and choose "Report a vulnerability".

Please do not open a public issue, pull request or discussion for a suspected vulnerability, and do
not include exploit detail anywhere public. You will get an acknowledgement, and a fix or a
decision, as soon as the maintainer can manage; this is a one-person project.

## Scope

FAFO is a read-only observer. It never kills, dispatches or merges anything, and it never collects
environment variables or transcript text. A bug that breaks one of those properties, leaks a secret
into a snapshot, or lets untrusted text reach a terminal is in scope.

## Supported versions

Only the latest release and the main branch receive fixes.
