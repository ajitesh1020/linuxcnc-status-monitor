#!/usr/bin/env bash
# Build linuxcnc-status-agent_<version>_all.deb into ./dist
#
#   bash packaging/deb/build.sh            # version from status.py
#   bash packaging/deb/build.sh 1.4.1      # explicit version
#
# Needs: dpkg-deb, gzip (any Debian/Ubuntu box, or the GitHub workflow).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/deb"
PKG=linuxcnc-status-agent
SRC_VERSION=$(sed -n 's/^AGENT_VERSION: str = "\(.*\)"/\1/p' "$ROOT/status.py")
VERSION=${1:-$SRC_VERSION}
OUT=${OUT_DIR:-$ROOT/dist}

[ -n "$VERSION" ] || { echo "cannot determine version" >&2; exit 1; }

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

SHARE="$STAGE/usr/share/$PKG"
DOC="$STAGE/usr/share/doc/$PKG"
install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$SHARE" "$DOC" "$STAGE/usr/lib/systemd/user"

# Program
for f in status.py cycle_time_calculator.py program_tracker.py agent_net.py \
         agent_runtime.py agent_journal.py config.example.yaml; do
    install -m 0644 "$ROOT/$f" "$SHARE/$f"
done
install -m 0755 "$ROOT/packaging/lcnc-status-agent" "$STAGE/usr/bin/lcnc-status-agent"
install -m 0644 "$ROOT/packaging/lcnc-status-agent.service" \
    "$STAGE/usr/lib/systemd/user/lcnc-status-agent.service"

# Docs
install -m 0644 "$ROOT/README.md" "$ROOT/PROTOCOL.md" "$DOC/"
gzip -9nc "$ROOT/CHANGELOG.md" > "$DOC/changelog.gz"
cat > "$DOC/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: linuxcnc-status-monitor
Source: https://github.com/ajitesh1020/linuxcnc-status-monitor

Files: *
Copyright: 2025-2026 Ajitesh Kannojia (CNC Tool Tech)
License: GPL-2+
 This program is free software; you can redistribute it and/or modify it under
 the terms of the GNU General Public License as published by the Free Software
 Foundation; either version 2 of the License, or (at your option) any later
 version.
 .
 On Debian systems, the full text of the GNU General Public License version 2
 can be found in /usr/share/common-licenses/GPL-2.
EOF
chmod 0644 "$DOC/copyright"

# Maintainer scripts (shared helpers spliced in at #COMMON#)
for s in postinst prerm postrm; do
    awk -v common="$HERE/common.sh" '
        /^#COMMON#$/ { while ((getline line < common) > 0) print line; next }
        { print }' "$HERE/$s" > "$STAGE/DEBIAN/$s"
    chmod 0755 "$STAGE/DEBIAN/$s"
done

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$HERE/control.in" \
    > "$STAGE/DEBIAN/control"

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB"
echo "Built $DEB"
