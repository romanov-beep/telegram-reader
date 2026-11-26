#!/bin/sh
# Run on a build machine with matching target architecture/distro, NOT the 768MB VPS.
# Official source only: https://github.com/tdlib/td / https://tdlib.github.io/td/build.html
set -eu
pin=9d92b49bc5d0d2e29fe518239af647a88f8680e6
if [ "$#" -ne 2 ]; then
    echo "Usage: sh scripts/build-tdlib.sh BUILD_DIRECTORY OUTPUT_DIRECTORY" >&2
    exit 2
fi
build_root=$(realpath -m "$1")
output_root=$(realpath -m "$2")
if [ -e "$build_root/td" ]; then
    echo "Use a fresh build directory (td already exists)." >&2
    exit 2
fi
mkdir -p "$build_root" "$output_root"
git clone --filter=blob:none --no-checkout https://github.com/tdlib/td.git "$build_root/td"
git -C "$build_root/td" checkout --detach "$pin"
actual_pin=$(git -C "$build_root/td" rev-parse HEAD)
test "$actual_pin" = "$pin"
cmake -S "$build_root/td" -B "$build_root/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build_root/build" --target tdjson --parallel 1
install -m 755 "$build_root/build/libtdjson.so" "$output_root/libtdjson.so"
strip --strip-unneeded "$output_root/libtdjson.so"
printf '%s\n' "$pin" > "$output_root/TDLIB_COMMIT"
cd "$output_root"
sha256sum libtdjson.so > SHA256SUMS
echo "Built official TDLib. Check target runtime dependencies before transfer."
