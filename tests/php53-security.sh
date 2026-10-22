#!/bin/bash
# Build only the PHP core, CLI and FPM needed for the security regression suite.
# The production build separately checks its full extension set before installation.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
TEST_WORK=$(mktemp -d)
trap 'rm -rf "$TEST_WORK"' EXIT

run() {
    local log="$TEST_WORK/$1.log"
    shift
    "$@" >"$log" 2>&1 || { tail -n 60 "$log" >&2; return 1; }
}

if [[ $# == 1 ]]; then
    cp "$1" "$TEST_WORK/php-5.3.29.tar.xz"
else
    curl -fsSL --retry 3 https://www.php.net/distributions/php-5.3.29.tar.xz -o "$TEST_WORK/php-5.3.29.tar.xz"
fi
echo "8438c2f14ab8f3d6cd2495aa37de7b559e33b610f9ab264f0c61b531bf0c262d  $TEST_WORK/php-5.3.29.tar.xz" | sha256sum -c -
tar -xJf "$TEST_WORK/php-5.3.29.tar.xz" -C "$TEST_WORK"
cd "$TEST_WORK/php-5.3.29"
patch --batch --forward --fuzz=0 -p1 -i "$REPO/roles/ispconfig_web/files/php-5.3.29-bailout.patch"
export CFLAGS='-O2 -g0 -fcommon -std=gnu89 -Wno-error=implicit-function-declaration -Wno-error=int-conversion -Wno-error=incompatible-pointer-types -Wno-error=implicit-int'
run configure ./configure --disable-all --enable-cli --enable-fpm --enable-session --enable-json --without-pear
run make make -j"${JOBS:-2}"
python3 "$REPO/tests/php53-security.py" "$PWD/sapi/cli/php" "$PWD/sapi/fpm/php-fpm"
