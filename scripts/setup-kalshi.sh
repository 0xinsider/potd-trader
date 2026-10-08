#!/bin/sh
# Run from a terminal:
# curl -fsSL https://raw.githubusercontent.com/0xinsider/potd-trader/v0.4.1/scripts/setup-kalshi.sh | sh
# Keep the body in a function: a truncated download cannot start the installation.

main() {
    set -eu
    umask 077

    version=0.4.1
    uv_version=0.12.23
    python_version=3.12.15
    uv_installer_sha=b8e6c43099ee9f9a550984d3ad56948457c689e7a99c090b35377234ac241491
    release_url="https://github.com/0xinsider/potd-trader/releases/download/v$version"
    wheel="potd_trader-$version-py3-none-any.whl"

    fail() {
        printf 'Kalshi setup stopped: %s\n' "$1" >&2
        exit 1
    }

    case "$(uname -s)" in
        Darwin|Linux) ;;
        *) fail "this installer supports macOS and Linux. Use the source setup on Windows." ;;
    esac
    if ! ( : </dev/tty ) 2>/dev/null; then
        fail "run this command in your own terminal so it can ask for information privately."
    fi
    case "${HOME:-}" in
        /*) ;;
        *) fail "HOME must name your absolute private home directory." ;;
    esac
    command -v curl >/dev/null 2>&1 || fail "curl is required."
    command -v mktemp >/dev/null 2>&1 || fail "mktemp is required."
    command -v awk >/dev/null 2>&1 || fail "awk is required."
    if command -v sha256sum >/dev/null 2>&1; then
        hash_command=sha256sum
    elif command -v shasum >/dev/null 2>&1; then
        hash_command=shasum
    else
        fail "sha256sum or shasum is required to verify downloads."
    fi

    digest() {
        if [ "$hash_command" = sha256sum ]; then
            sha256sum "$1" | awk '{ print $1 }'
        else
            shasum -a 256 "$1" | awk '{ print $1 }'
        fi
    }

    fetch() {
        # Ignore .curlrc; allow only verified HTTPS, including redirects.
        env -i HOME="$HOME" PATH="$PATH" curl --disable --fail --silent --show-error \
            --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
            --connect-timeout 20 --max-time 180 --retry 2 --output "$2" "$1"
    }

    base="$HOME"
    for component in .local share potd-trader kalshi; do
        base="$base/$component"
        [ ! -L "$base" ] || fail "the installation folder must not contain symbolic links."
        mkdir -p "$base"
    done
    chmod 700 "$base"
    # Every invocation gets a new runtime. Never replace an interpreter used by a watcher.
    runtime=$(mktemp -d "$base/$version.XXXXXX")
    printf 'Installing potd-trader %s in a private runtime.\n' "$version" >&2
    printf 'Your shell profiles and system Python stay unchanged.\n' >&2

    fetch "https://astral.sh/uv/$uv_version/install.sh" "$runtime/uv-install.sh"
    [ "$(digest "$runtime/uv-install.sh")" = "$uv_installer_sha" ] \
        || fail "the uv installer checksum did not match. Nothing was executed."
    # A clean environment prevents inherited download mirrors or installer overrides.
    env -i HOME="$runtime" PATH="$PATH" UV_UNMANAGED_INSTALL="$runtime/uv" \
        /bin/sh "$runtime/uv-install.sh" --quiet
    [ "$("$runtime/uv/uv" --version | awk '{ print $2 }')" = "$uv_version" ] \
        || fail "the installed uv version did not match."

    fetch "$release_url/SHA256SUMS" "$runtime/SHA256SUMS"
    for asset in "$wheel" requirements.txt; do
        expected=$(awk -v name="$asset" '
            $2 == name { count++; checksum=$1 }
            END {
                if (count != 1 || length(checksum) != 64 || checksum ~ /[^0-9a-f]/) exit 1;
                print checksum
            }' "$runtime/SHA256SUMS") \
            || fail "the release checksum manifest has no unique checksum for $asset."
        fetch "$release_url/$asset" "$runtime/$asset"
        [ "$(digest "$runtime/$asset")" = "$expected" ] \
            || fail "the downloaded $asset checksum did not match."
    done

    run_uv() {
        env -i HOME="$runtime" PATH="$PATH" UV_PYTHON_INSTALL_DIR="$runtime/python" \
            UV_CACHE_DIR="$runtime/cache" "$runtime/uv/uv" --no-config "$@"
    }
    run_uv venv --no-project --managed-python --python "$python_version" "$runtime/venv"
    run_uv pip install --python "$runtime/venv/bin/python" --require-hashes --no-build \
        --default-index https://pypi.org/simple -r "$runtime/requirements.txt"
    run_uv pip install --python "$runtime/venv/bin/python" --no-deps --no-index \
        "$runtime/$wheel"
    [ "$("$runtime/venv/bin/python" -I -m potd_trader.cli --version)" = "potd-trader $version" ] \
        || fail "the installed trader version did not match."

    printf '\nInstallation complete. The terminal wizard will ask for the remaining details.\n' >&2
    printf 'Runtime: %s\n\n' "$runtime" >&2
    # Reattach stdin after curl | sh. Python isolated mode ignores PYTHONPATH/user-site.
    # Keep this runtime if setup is interrupted: a saved launcher may already reference it.
    exec env -i HOME="$HOME" PATH="$PATH" TERM="${TERM:-dumb}" \
        "$runtime/venv/bin/python" -I -m potd_trader.cli kalshi setup "$@" </dev/tty
}

main "$@"
