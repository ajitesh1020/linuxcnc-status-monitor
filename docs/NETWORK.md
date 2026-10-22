# Networking

## Short version

- **No static IP is needed on either PC.** The agent broadcasts on the local
  network; the dashboard listens on UDP port 5005 and receives every machine on
  the same network, whatever IP each PC has today.
- The office PC may be on **Wi-Fi or cable** — as long as it's on the same
  network (same router) as the machine.
- If nothing arrives, it's almost always the **Windows firewall** — see below.

---

## Why the old setup kept breaking

The agent used to send to one fixed IP (`monitor_pc_ip: "193.168.0.3"`). The
office PC gets its address from the router (DHCP), so after a reboot it could
come back with a different one and the machine kept sending to the old
address. Broadcast removes that dependency completely.

## How broadcast works here

With `monitor_pc_ip: "auto"` the agent sends each packet to the broadcast
address of every usable interface (e.g. `192.168.0.255`), re-checked every
30 s so a Wi-Fi reconnect or new DHCP lease is picked up automatically.

It never broadcasts on:

- the **Mesa `hm2_eth` link** — detected automatically (the board's
  `00:60:1b:…` MAC in the ARP table), so real-time traffic is untouched;
- loopback / VPN point-to-point links;
- anything listed in `exclude_interfaces`.

With `monitor_interface` set, it sends only on that one card — see
[Restricted pilot network](#restricted-pilot-network).

`lcnc-status-agent --check` prints exactly where packets are going.

Traffic is small: ~2 KB per second per running machine, one packet per 30 s
when idle.

## Do I need a static IP on the LinuxCNC PC?

**No.** The agent only sends; the dashboard identifies machines by
`machine_name` (default: the PC's hostname), not by IP.

If you want one anyway (e.g. to SSH in), a static IP does **not** break the
internet as long as you also set the **gateway** (your router, e.g.
`192.168.0.1`) and **DNS** (the router, or `8.8.8.8`). The usual causes of "no
internet after setting a static IP" are:

1. Gateway/DNS left empty.
2. The IP chosen is inside the router's DHCP range and clashes with another
   device — pick one outside it, or better, use a **DHCP reservation** on the
   router (same IP every time, no settings on the PC).
3. **Mesa card on the same network card.** The `hm2_eth` link must be its own
   network card with a static address in the Mesa range (e.g. `10.10.10.1/8`)
   and **no gateway**. Internet and the dashboard go through a second card or
   Wi-Fi, which stays on DHCP. A gateway set on the Mesa card steals the
   default route and kills internet access.

Recommended LinuxCNC PC layout:

| Interface | Used for | Address |
|---|---|---|
| NIC 1 | Mesa card only | static `10.10.10.1/8`, **no gateway, no DNS** |
| NIC 2 or Wi-Fi | LAN / internet / dashboard | DHCP (or router reservation) |

## Wi-Fi notes

Broadcast works over Wi-Fi on normal home/office routers. It is blocked when
the router has **client / AP isolation** ("guest network") enabled — then
devices can't see each other at all. Either turn isolation off for that
network, connect one side by cable, or use a fixed address (next section).

## When broadcast isn't possible

Some managed networks drop broadcast, or the dashboard is on another subnet
(e.g. across a VLAN / VPN). Then set explicit targets:

```yaml
monitor_pc_ip: "office-pc.local"          # host name, re-resolved every 30 s
monitor_pc_ip: "192.168.0.50"             # fixed IP (use a router DHCP reservation)
monitor_pc_ip: "auto, 10.20.0.15"         # broadcast AND a remote dashboard
```

## Restricted pilot network

For a pilot in a real plant (industrial readiness review, finding F07), keep
the monitoring traffic on its own network and say exactly who may talk to
whom. Status packets are not encrypted: anyone on the network can read them,
and without a site key anyone can pretend to be a machine.

1. **A dedicated monitoring network** (its own switch or VLAN) for the LinuxCNC
   PCs and the dashboard PC, with no route to it from untrusted networks (guest
   Wi-Fi, the internet). The Mesa card stays on its own card and is never part
   of it.
2. **Send on one interface only** — the card on the monitoring network:

   ```yaml
   monitor_interface: "enp2s0"     # the monitoring-network card, NOT the Mesa card
   monitor_pc_ip: "192.168.50.10"  # the dashboard PC (or "auto" = broadcast on enp2s0 only)
   ```

   The agent then sends from that card's address, broadcasts only on that card
   (never the 255.255.255.255 fallback), and drops any target the kernel would
   route out of another card (logged). If the card is missing, down, has no
   IPv4 address, is listed in `exclude_interfaces` or has a Mesa board on it,
   **nothing is sent** until that is fixed (re-checked every 30 s). No root
   rights are needed.
3. **History port on the same card, for the dashboard only:**

   ```yaml
   journal_bind: "192.168.50.21"   # this PC's address on enp2s0
   journal_allow: "192.168.50.10"  # the dashboard PC
   ```

4. **Site key** on every machine and in FloorWatch (`site_key`, PROTOCOL.md
   §8.1): packets are signed, history is given only to signed requests, and
   FloorWatch can refuse unsigned machines. It proves "knows the key", not
   which machine; it is not encryption.
5. **Host firewalls, both directions.** Dashboard PC: allow inbound UDP 5005
   only from the machines' addresses; allow outbound UDP 5006 to them.
   LinuxCNC PCs: allow inbound UDP 5006 only from the dashboard PC; the agent
   itself only sends to UDP 5005.
6. **Check each machine:** `lcnc-status-agent --check` shows the interface, its
   address and broadcast address, where packets go, the history port binding
   and whether packets are signed.

This follows defense-in-depth practice (e.g. CISA's ICS recommended practices);
it is not a compliance claim. For untrusted or shared networks, authenticated
per-device messages with encryption are still to come (MES plan Phase 2).

## Windows firewall (dashboard PC)

The first time the dashboard runs, Windows asks whether to allow it on
networks — tick **Private** (and **Public** if your office network is set to
Public). If you missed it:

1. Settings → Network & Internet → your connection → set **Network profile
   type: Private**.
2. Or allow it manually (admin PowerShell):

   ```powershell
   New-NetFirewallRule -DisplayName "LinuxCNC Dashboard UDP 5005" -Direction Inbound -Protocol UDP -LocalPort 5005 -Action Allow
   ```

To test without the dashboard, run `python examples/udp_receiver.py` on the
office PC — if it prints packets, the network is fine.
