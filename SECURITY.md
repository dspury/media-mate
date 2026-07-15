# Security and secret handling

Live credentials, private keys, bearer values, recovery material, and
secret-bearing production exports do not belong in this repository, whether it
is private or public.

## Secret scanning

Install Gitleaks and run:

```bash
./scripts/secret-scan.sh staged
./scripts/secret-scan.sh all
```

Enable the versioned local pre-commit hook with:

```bash
git config core.hooksPath .githooks
```

The hook fails closed when Gitleaks is unavailable. CI downloads the pinned
Gitleaks release, verifies its upstream checksum, and scans both the working
tree and complete fetched history with read-only repository permissions.

Do not bypass a failed scan. If a finding is real, revoke or rotate it first,
then remove the material and rescan. Do not copy a credential into an issue,
pull request, log, or remediation note. Any allowlist exception must be narrow
to the exact rule, path, and proven synthetic or false-positive value.

Before making a private repository public, also review every branch and tag,
Git LFS and submodule content, release and Actions artifacts, generated
archives, source maps, notebooks, fixtures, logs, screenshots, workflows, and
private topology. Repeat the scan from a fresh clone and obtain repository-owner
approval before changing visibility.
