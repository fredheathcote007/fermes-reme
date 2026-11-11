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
| #7 | high | vite | github-pages/package-lock.json | 8.3.8 | 6.4.4 | PATCHED-CANDIDATE |
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

- ReMe Studio candidate: zero critical/high, 11 moderate. Node 24 npm ci, 52/52 unit, static/demo and Vinext server builds, and Cloudflare worker-rendered distribution checks pass. Monaco 0.55.1 embedded DOMPurify 3.2.7 despite a patched lockfile; candidate now upgrades Monaco to 0.57.0 and replaces its embedded 3.4.15 sanitizer module with pinned DOMPurify 3.4.17 through a shared Vite plugin. A production-bundle regression test checks the final static, demo and Vinext client assets. These changes require fresh independent review.
- OpenClaw candidate: zero critical, two high (openclaw and zod); seven moderate. Node 24 npm ci, typecheck and 11/11 tests pass. npm suggests incompatible version rollback for OpenClaw, which has not been adopted.
- DSH optional integration: 13 high and 7 moderate in inherited pinned dependencies; major-version changes required. These are not accepted exceptions.
- Codex status-app: two high (@openai/mcp-extensions and zod) and no known npm audit fix; these are not accepted exceptions.
- GitHub Pages candidate: zero critical/high/moderate on VitePress 2.0.0-alpha.21 with the lockfile-resolved Vite 8.3.8 and source-map-js 1.2.2. The previous VitePress 1.6.4 plus Vite 6.4.4 override was out of its declared range and rejected by independent review. Owner decision: retain the exact VitePress 2.0.0-alpha.21 prerelease as a candidate stability/support risk pending release approval; this is not an accepted security exception. Docs tests and build/40-artifact verification pass.

## Closure gates

FHM-007 Python test result: 174 passed, 4 skipped on exact candidate.
Initial independent reviewer returned REQUEST_CHANGES (bundled Monaco sanitizer and VitePress/Vite contract). Both have candidate corrections. On exact HEAD `e3c76144`, GitHub CI reports all 16 applicable checks PASS; publication was skipped. The corrected Cloudflare worker-harness distribution test passes locally. Documentation and status-app builds were also validated locally.
Confirm actual runtime use and reachability for Studio Vinext, OpenClaw, DSH and Codex status-app.
Owner decision: the 17 inherited high package findings remain open and are not accepted exceptions—DSH 13, Codex status-app 2, and OpenClaw 2. Record any further owner-approved dispositions explicitly; do not dismiss advisories or claim zero risk.
After merge, refresh live GitHub Dependabot counts. Issue must remain open until all acceptance checks pass.

Evidence remains private on ProjCoord under /srv/scratch/issue6-*; this source file contains no local endpoints, secrets or private infrastructure details.
