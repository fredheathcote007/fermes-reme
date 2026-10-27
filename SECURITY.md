# Security policy

Fermes ReMe is a community fork of [AgentScope ReMe](https://github.com/agentscope-ai/ReMe).
The upstream Apache-2.0 license, copyright notices, and attribution remain in this repository. This policy covers
security issues in this fork, including its workflows, packaging, integrations, and fork-specific changes.

## Report a vulnerability privately

Please do not open a public issue, discussion, pull request, or chat message for an unpatched vulnerability. Use
GitHub's private vulnerability reporting for this fork:

[Report a vulnerability to fredheathcote007/fermes-reme](https://github.com/fredheathcote007/fermes-reme/security/advisories/new)

Include the affected version or commit, impact, reproduction steps, relevant configuration, and a safe way to follow
up. The maintainer will use the advisory to coordinate triage, a fix, and disclosure. If the issue is demonstrably
only in unchanged upstream ReMe code, please also consult the [upstream security policy](https://github.com/agentscope-ai/ReMe/security).

If GitHub does not offer the private advisory form, do not disclose the details publicly; contact
[@fredheathcote007](https://github.com/fredheathcote007) through GitHub and request a private channel.

## Keep reports safe

- Never include API keys, access tokens, passwords, cookies, private keys, `.env` files, or other secrets.
- Never attach private ReMe workspace files, memory, transcripts, personal data, or customer data.
- Use a minimal sanitized reproduction with placeholders and synthetic memory content.
- Revoke or rotate any credential that may have been exposed, and say only that rotation occurred in the report.

The same rules apply to public bug reports and pull requests. Do not use this repository's issue tracker to disclose
confidential vulnerability details.

## Publishing safety

This fork's package and container publication workflows are disabled by default. Publishing requires the maintainer to
explicitly enable the `FERMES_ENABLE_PUBLISHING` repository variable after reviewing the release and security setup.
