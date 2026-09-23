# Shared by postinst / prerm (copied into each at build time).
UNIT=lcnc-status-agent.service

# Run `systemctl --user <args>` for every logged-in regular user.
for_each_user() {
    command -v loginctl >/dev/null 2>&1 || return 0
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        [ "$uid" -ge 1000 ] 2>/dev/null || continue
        user=$(id -nu "$uid" 2>/dev/null) || continue
        systemctl --user -M "$user@" "$@" >/dev/null 2>&1 || true
    done
}
