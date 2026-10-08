# Kalshi one-command setup 0.4.1 delivery evidence

Scope: [issue #18](https://github.com/0xinsider/potd-trader/issues/18),
[trader PR #19](https://github.com/0xinsider/potd-trader/pull/19), and
[companion docs PR #438](https://github.com/0xinsider/docs.0xinsider.com/pull/438).
Trevor requested a single install/setup command with terminal prompts on October 8, 2026.

## Source and terminal evidence

Locked sync, Ruff source lint/format, strict Mypy (16 modules), source compilation,
legacy/new CLI help, shell syntax, ShellCheck and wheel/source distribution build passed.
The legacy pre-merge helper invokes unittest; it was excluded under the current no-tests
instruction. Its permitted source/package legs ran directly. No tests or test-only
infrastructure were added, changed or run.

A real terminal session created a stopped demo folder, prompted for environment, and reached
hidden Kalshi key entry. Its terminal ECHO flag was observed disabled. Folder mode was 700,
configuration mode 600, and HALT was present. Ctrl-C stopped cleanly with exit130.
Rerunning the same setup resumed the existing blank stopped configuration and reached the
missing-key prompt without rewriting the account or safety state. The isolated executable
then reported demo/trading stopped. No key values were entered, captured or printed.
An invocation without a controlling terminal refuses before any download.

Independent source review identified and resolved cancellation between order enablement and
watch cleanup, raw validation-value logging, saved-contract re-review and misleading Enter
prompt wording. Enablement and foreground watching share finally cleanup; no current reviewed
mapping means no watcher enablement offer. Existing non-wizard watch behavior is preserved.

## Distribution contract

The pinned bootstrap downloads uv0.12.23 from the official versioned installer and verifies
its SHA256 `b8e6c43099ee9f9a550984d3ad56948457c689e7a99c090b35377234ac241491`.
A real unmanaged private uv install reported that version, provisioned managed Python3.12.15,
and installed all40 locked dependency packages with required hashes and no source builds on
macOS arm64. It does not edit shell profiles or system Python.

Each invocation creates a private runtime without replacing an interpreter used by a watcher.
A sanitized environment prevents inherited installer/index/Python overrides. HTTPS downloads
ignore curl user configuration, enforce HTTPS redirects, and check explicit wheel/requirements
checksums. The installed module uses Python isolated mode and reconnects stdin to /dev/tty.
The generated configuration-local launcher keeps that interpreter and configuration together.

Distribution reads use GitHub/raw/release-assets, Astral, and PyPI/file hosts. User credentials
are collected only after installation and sent only through the existing feed/Kalshi clients.
The wizard privately copies the PEM, retains valid saved credentials/account/ledger/mappings,
shows current picks, prompts for an explicit equivalent contract, and uses the existing full
rule-review phrase and execution guards. All key/account fields remain user supplied.

## Acceptance boundary

Authenticated account/feed planning, contract review with a real released pick, and demo fills
remain unobserved without user-entered keys. No order, live-enable command, or direct production
data write ran. No contract equivalence, coverage, or successful fill is inferred from terminal
setup. Provider-execution behavior remains the reviewed v0.4.0 owner.

The post-merge release receipt records exact merged source, platform checks, published artifacts,
a fresh pinned installer reaching the hidden terminal prompt, PyPI/package verification and
served companion guide. That receipt is attached to the release and issue before closure.

## Re-check commands

```bash
uv sync --locked
uv run --locked ruff check src
uv run --locked ruff format --check src
uv run --locked mypy src
uv run --locked python -m compileall -q src
uv run --locked potd-trader kalshi setup --help
sh -n scripts/setup-kalshi.sh
shellcheck --shell=sh scripts/setup-kalshi.sh
uv build
```

Official installer contracts: [unmanaged installation](https://docs.astral.sh/uv/reference/installer/),
[uv CLI](https://docs.astral.sh/uv/reference/cli/),
[private runtime environment](https://docs.astral.sh/uv/reference/environment/),
[uv0.12.23](https://github.com/astral-sh/uv/releases/tag/0.12.23), and
[curl configuration and HTTPS options](https://curl.se/docs/manpage.html).
