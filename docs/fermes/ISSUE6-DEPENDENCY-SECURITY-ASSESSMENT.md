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

- ReMe Studio candidate: zero critical/high, 11 moderate. Node 24 npm ci, 52/52 unit, static/demo and Vinext server builds, and Cloudflare worker-rendered distribution checks pass. Monaco 0.55.1 embedded DOMPurify 3.2.7 despite a patched lockfile; candidate now upgrades Monaco to 0.57.0 and replaces its embedded 3.4.15 sanitizer module with pinned DOMPurify 3.4.17 through a shared Vite plugin. A production-bundle regression test checks the final static, demo and Vinext client assets. Independent Claude source review returned GO for an interim mitigation, with full merge/release qualification still outstanding.
- OpenClaw candidate: zero critical/high, eight moderate after pinning Zod 4.6.7 as a transitive override. Node 24 npm ci, build/typecheck, 11/11 tests and package test PASS. No OpenClaw downgrade was adopted. The override applies to this repository's dependency tree, not automatically to external consumers.
- DSH optional integration: zero critical/high, seven moderate after a Zod 4.6.7 override. Node 24 npm ci, build/typecheck, 76/76 tests and package tests across five supported DSH versions PASS. The inherited HIGH package findings were propagated from vulnerable Zod 4.4.3.
- Codex status app: zero critical/high/moderate after overriding the nested Zod 4.4.3 dependency to 4.6.7. Node 24 npm ci, self-contained HTML build and two runtime MCP-schema/bundle smoke tests PASS.
- GitHub Pages candidate: zero critical/high/moderate on VitePress 2.0.0-alpha.21 with the lockfile-resolved Vite 8.3.8 and source-map-js 1.2.2. The previous VitePress 1.6.4 plus Vite 6.4.4 override was out of its declared range and rejected by independent review. Owner approval is still required for the exact VitePress 2.0.0-alpha.21 prerelease's stability/support risk; this is not an accepted security exception. Docs tests and build/40-artifact verification pass.

## Closure gates

FHM-007 focused Python regression repeated after Zod integration: 174 passed, 4 skipped in 17.54 seconds on commit 28f1c131 (before this evidence-only documentation update).
Independent Claude source review returned GO as an interim mitigation at `e3c76144`; reviewer hardening suggestions were implemented in a subsequent commit. On `e3c76144`, GitHub CI reported all 16 applicable checks PASS (publication skipped). The Zod override received a separate independent Claude GO after npm dependency-tree, lockfile-integrity and status-app schema tests addressed its verification findings. The updated head requires fresh GitHub CI before integration acceptance.
Confirm actual runtime use and reachability for Studio Vinext, OpenClaw, DSH and Codex status-app.
The 17 inherited HIGH npm audit package-tree findings in DSH, Codex status app and OpenClaw were eliminated in the candidate by the verified Zod 4.6.7 override. This is not an owner-approved risk exception and not proof of deployed remediation. Maintain an explicit owner and update trigger for exact transitive overrides; verify runtime exposure and re-scan GitHub after merge. No HIGH or CRITICAL npm findings remain in the five audited JavaScript lockfile trees; moderate findings and unexamined manifests require separate disposition.
After merge, refresh live GitHub Dependabot counts. Issue must remain open until all acceptance checks pass.

Evidence remains private on ProjCoord under /srv/scratch/issue6-*; this source file contains no local endpoints, secrets or private infrastructure details.
