#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Clean-install check: install a built wheel into a fresh virtual environment and run the CLI.
# Usage: scripts/clean_install_check.sh PATH/TO/fafo-X.Y.Z-py3-none-any.whl
# The work directory is made with mktemp -d under the user's cache folder (never /tmp) and is
# the only thing this script removes. State goes to a throwaway directory inside it.
# `fafo doctor` must exit 0 or 1, report `host ok` and print no traceback (scripts/check_doctor_output.py).
set -u
set -o pipefail

die() { printf 'clean_install_check: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 1 ] || die "usage: $0 WHEEL"
wheel=$1
[ -f "$wheel" ] || die "not a file: $wheel"
case $wheel in *.whl) ;; *) die "not a wheel: $wheel" ;; esac
wheel=$(cd "$(dirname -- "$wheel")" && printf '%s/%s' "$PWD" "$(basename -- "$wheel")") || die "cannot resolve wheel path"

cache_root=${XDG_CACHE_HOME:-$HOME/.cache}
mkdir -p -- "$cache_root" || die "cannot create $cache_root"
work=$(mktemp -d -- "$cache_root/fafo-clean-install.XXXXXXXX") || die "mktemp failed"
[ -n "$work" ] && [ -d "$work" ] || die "no work directory"

cleanup() {
    rc=$?
    # Remove only the directory mktemp made: non-empty, inside the cache folder, with our prefix.
    case $work in
        "$cache_root"/fafo-clean-install.?*) [ -d "$work" ] && rm -r -- "$work" ;;
        *) printf 'clean_install_check: refusing to remove %s\n' "$work" >&2 ;;
    esac
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

venv=$work/venv
state=$work/state
mkdir -p -- "$state" || die "cannot create state dir"
export XDG_RUNTIME_DIR=$state
export XDG_STATE_HOME=$state
export XDG_CONFIG_HOME=$state/config
export HOME=$work/home
mkdir -p -- "$HOME" "$XDG_CONFIG_HOME" || die "cannot create home"
snapshot=$state/snapshot.json

step() {
    printf '== %s\n' "$*"
}

step "create venv"
timeout 120 python3 -m venv -- "$venv" < /dev/null || die "venv failed"
step "install wheel"
timeout 600 "$venv/bin/python" -m pip install --disable-pip-version-check --quiet -- "$wheel" < /dev/null \
    || die "pip install failed"

fafo=$venv/bin/fafo
[ -x "$fafo" ] || die "console script missing: $fafo"

step "fafo --version"
timeout 30 "$fafo" --version < /dev/null || die "fafo --version failed"
step "fafo doctor"
# Exit 0 or 1 only (1: some source is down or stale), a `host ok` line, no traceback; the rule lives in
# check_doctor_output.py. Output goes to a file inside the work directory, then is shown.
doctor_out=$work/doctor.out
timeout 120 "$fafo" doctor < /dev/null > "$doctor_out" 2>&1
rc=$?
cat -- "$doctor_out"
printf 'doctor exit=%s\n' "$rc"
here=$(cd "$(dirname -- "$0")" && pwd) || die "cannot resolve script directory"
timeout 30 python3 -I "$here/check_doctor_output.py" "$rc" "$doctor_out" < /dev/null \
    || die "fafo doctor output refused"
step "fafo table"
timeout 30 "$fafo" table --snapshot "$snapshot" < /dev/null || die "fafo table failed"
step "ok"
