# Issue 6 — dependency security qualification

11 November 2026 AWST; fork main baseline 96948df3; remediation branch fix/issue6-dependency-security-20261111.
Authenticated GitHub baseline: 67 open Dependabot alerts (4 critical, 19 high, 33 medium, 11 low).
The following per-alert table applies only to the unmerged candidate and is not proof of production remediation.
Development dependency scope does not establish that the package is unreachable.

| Alert | Severity | Package | Manifest | Candidate version | First patched | Candidate status |
| --- | --- | --- | --- | --- | --- | --- |
| #32 | critical | proxy-addr | integrations/openclaw/package-lock.json | 2.0.8 | 2.0.8 | PATCHED-CANDIDATE |
| #91 | critical | dompurify | reme_studio/package-lock.json | 3.4.17 | 3.4.17 | PATCHED-CANDIDATE |
| #118 | critical | react-server-dom-webpack | reme_studio/package.json | 19.3.1 | 19.2.9 | PATCHED-CANDIDATE |
| #119 | critical | react-server-dom-webpack | reme_studio/package-lock.json | 19.3.1 | 19.2.9 | PATCHED-CANDIDATE |
| #5 | high | source-map-js | github-pages/package-lock.json | 1.2.2 | 1.2.2 | PATCHED-CANDIDATE |
| #7 | high | vite | github-pages/package-lock.json | 6.4.4 | 6.4.4 | PATCHED-CANDIDATE |
| #12 | high | @modelcontextprotocol/sdk | integrations/codex/status-app/package-lock.json | 1.32.2 | 1.32.0 | PATCHED-CANDIDATE |
| #33 | high | @modelcontextprotocol/sdk | integrations/openclaw/package-lock.json | 1.32.0 | 1.31.0 | PATCHED-CANDIDATE |
| #36 | high | @modelcontextprotocol/sdk | integrations/openclaw/package-lock.json | 1.32.0 | 1.32.0 | PATCHED-CANDIDATE |
| #48 | high | undici | integrations/openclaw/package-lock.json | 8.12.2 | 8.12.2 | PATCHED-CANDIDATE |
| #49 | high | browserslist | reme_studio/package-lock.json | 4.28.7 | 4.28.7 | PATCHED-CANDIDATE |
| #57 | high | js-yaml | reme_studio/package-lock.json | 4.3.2 | 4.3.2 | PATCHED-CANDIDATE |
| #58 | high | sharp | reme_studio/package-lock.json | 0.35.5 | 0.35.4 | PATCHED-CANDIDATE |
| #65 | high | undici | reme_studio/package-lock.json | 7.30.1 | 7.29.1 | PATCHED-CANDIDATE |
| #82 | high | sharp | reme_studio/package-lock.json | 0.35.5 | 0.35.5 | PATCHED-CANDIDATE |
| #90 | high | dompurify | reme_studio/package-lock.json | 3.4.17 | 3.4.17 | PATCHED-CANDIDATE |
| #99 | high | undici | reme_studio/package-lock.json | 7.30.1 | 7.30.1 | PATCHED-CANDIDATE |
| #105 | high | undici | integrations/openclaw/package-lock.json | 8.12.2 | 8.12.2 | PATCHED-CANDIDATE |
| #106 | high | undici | integrations/openclaw/package-lock.json | 8.12.2 | 8.12.2 | PATCHED-CANDIDATE |
| #107 | high | undici | integrations/openclaw/package-lock.json | 8.12.2 | 8.12.2 | PATCHED-CANDIDATE |
| #112 | high | undici | reme_studio/package-lock.json | 7.30.1 | 7.30.1 | PATCHED-CANDIDATE |
| #113 | high | undici | reme_studio/package-lock.json | 7.30.1 | 7.30.1 | PATCHED-CANDIDATE |
| #114 | high | undici | reme_studio/package-lock.json | 7.30.1 | 7.30.1 | PATCHED-CANDIDATE |

## Other npm audit findings

- ReMe Studio candidate: zero critical/high, 11 moderate. Node 24 npm ci, 52/52 unit, static build and Vinext server build pass.
- OpenClaw candidate: zero critical, two high (openclaw and zod); seven moderate. Node 24 npm ci, typecheck and 11/11 tests pass. npm suggests incompatible version rollback for OpenClaw, which has not been adopted.
- DSH optional integration: 13 high and 7 moderate in inherited pinned dependencies; major-version changes required. These are not accepted exceptions.
- Codex status-app: two high (@openai/mcp-extensions and zod) and no known npm audit fix; these are not accepted exceptions.
- GitHub Pages candidate: zero critical/high; one moderate.

## Closure gates

FHM-007 Python test result: 174 passed, 4 skipped on exact candidate.
Await independent security review and GitHub CI; validate remaining documentation/status-app builds as applicable.
Confirm actual runtime use and reachability for Studio Vinext, OpenClaw, DSH and Codex status-app.
Record explicit owner-approved dispositions for unresolved critical/high findings; do not dismiss advisories or claim zero risk.
After merge, refresh live GitHub Dependabot counts. Issue must remain open until all acceptance checks pass.

Evidence remains private on ProjCoord under /srv/scratch/issue6-*; this source file contains no local endpoints, secrets or private infrastructure details.
