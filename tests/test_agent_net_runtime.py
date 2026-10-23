"""Tests for send-target discovery and the LinuxCNC process watch.
Run: python -m unittest -v
"""
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import agent_net  # noqa: E402
import agent_runtime  # noqa: E402

IP_JSON = [
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"],
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "enp2s0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"],
     "addr_info": [{"family": "inet", "local": "192.168.0.120", "prefixlen": 24,
                    "broadcast": "192.168.0.255"}]},
    {"ifname": "wlp3s0", "flags": ["BROADCAST", "UP"],
     "addr_info": [{"family": "inet", "local": "10.1.5.7", "prefixlen": 22}]},
    {"ifname": "enp1s0", "flags": ["BROADCAST", "UP"],          # Mesa link
     "addr_info": [{"family": "inet", "local": "10.10.10.1", "prefixlen": 8}]},
    {"ifname": "tun0", "flags": ["POINTOPOINT", "UP"],
     "addr_info": [{"family": "inet", "local": "10.8.0.2", "prefixlen": 32}]},
]

ARP = """IP address       HW type     Flags       HW address            Mask     Device
10.10.10.10      0x1         0x6         00:60:1b:12:34:56     *        enp1s0
192.168.0.1      0x1         0x2         a4:2b:b0:01:02:03     *        enp2s0
"""


class BroadcastDiscoveryTests(unittest.TestCase):
    def test_mesa_link_detected_from_arp(self):
        self.assertEqual(agent_net.mesa_interfaces(ARP), {"enp1s0"})

    def test_lan_and_wifi_broadcasts_mesa_skipped(self):
        pairs = agent_net.broadcast_addresses(IP_JSON, exclude={"enp1s0"})
        self.assertEqual(pairs, [("enp2s0", "192.168.0.255"),
                                 ("wlp3s0", "10.1.7.255")])   # computed from /22

    def test_user_exclude(self):
        pairs = agent_net.broadcast_addresses(IP_JSON, exclude={"enp1s0", "wlp3s0"})
        self.assertEqual([b for _, b in pairs], ["192.168.0.255"])

    def test_target_list_parsing(self):
        self.assertEqual(agent_net.parse_targets(""), ["auto"])
        self.assertEqual(agent_net.parse_targets(" 192.168.0.5 , pc.local "),
                         ["192.168.0.5", "pc.local"])

    def test_explicit_ip_needs_no_discovery(self):
        self.assertEqual(agent_net.resolve_targets("192.168.0.5", []), ["192.168.0.5"])


class MonitorInterfaceTests(unittest.TestCase):
    """monitor_interface: send only on one interface (MES plan P0.5)."""

    def plan(self, name, exclude=(), arp=ARP):
        with mock.patch.object(agent_net, "_ip_json", return_value=IP_JSON), \
                mock.patch.object(agent_net, "_read", return_value=arp):
            return agent_net.plan_interface(name, exclude)

    def test_interface_address_and_broadcast(self):
        self.assertEqual(agent_net.interface_ipv4(IP_JSON, "enp2s0"),
                         ("192.168.0.120", "192.168.0.255"))
        self.assertEqual(agent_net.interface_ipv4(IP_JSON, "wlp3s0"),
                         ("10.1.5.7", "10.1.7.255"))           # computed from /22
        self.assertEqual(agent_net.interface_ipv4(IP_JSON, "tun0"), ("10.8.0.2", None))
        self.assertIsNone(agent_net.interface_ipv4(IP_JSON, "eth9"))
        down = [{"ifname": "eth1", "flags": ["BROADCAST"],
                 "addr_info": [{"family": "inet", "local": "192.168.5.2", "prefixlen": 24}]}]
        self.assertIsNone(agent_net.interface_ipv4(down, "eth1"))

    def test_usable_interface(self):
        p = self.plan("enp2s0")
        self.assertEqual((p.address, p.broadcast, p.problem), ("192.168.0.120", "192.168.0.255", ""))
        self.assertIn("192.168.0.255", p.describe())

    def test_mesa_excluded_and_missing_interfaces_are_refused(self):
        self.assertIn("Mesa", self.plan("enp1s0").problem)
        self.assertIn("exclude_interfaces", self.plan("enp2s0", exclude=["enp2s0"]).problem)
        self.assertIn("missing", self.plan("eth9").problem)
        for name in ("enp1s0", "eth9"):
            p = self.plan(name)
            self.assertIsNone(p.address)
            self.assertEqual(agent_net.resolve_targets("auto,192.168.0.5", [], p), [])

    def test_auto_is_only_that_interfaces_broadcast(self):
        p = self.plan("wlp3s0")
        with mock.patch.object(agent_net, "discover_broadcast_targets") as everywhere:
            self.assertEqual(agent_net.resolve_targets("auto", [], p), ["10.1.7.255"])
        everywhere.assert_not_called()
        self.assertEqual(agent_net.resolve_targets("auto", [], self.plan("tun0", arp="")), [])

    def test_fixed_targets_kept_only_if_routed_through_the_interface(self):
        p = self.plan("enp2s0")
        routes = {"192.168.0.5": "enp2s0", "10.10.10.10": "enp1s0", "8.8.8.8": "enp2s0"}
        calls = []

        def route(dest, src):
            calls.append((dest, src))
            return routes.get(dest)

        with mock.patch.object(agent_net, "route_device", side_effect=route), \
                self.assertLogs("agent_net", "WARNING") as logs:
            got = agent_net.resolve_targets("auto,192.168.0.5,10.10.10.10,8.8.8.8,192.0.2.9", [], p)
        self.assertEqual(got, ["192.168.0.255", "192.168.0.5", "8.8.8.8"])
        self.assertTrue(all(src == "192.168.0.120" for _, src in calls))
        self.assertEqual(sum("10.10.10.10" in m or "192.0.2.9" in m for m in logs.output), 2)

    def test_without_monitor_interface_nothing_changes(self):
        with mock.patch.object(agent_net, "route_device") as route:
            self.assertEqual(agent_net.resolve_targets("192.168.0.5", [], None), ["192.168.0.5"])
        route.assert_not_called()


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("ip"), "Linux ip command")
class MonitorInterfaceSendTests(unittest.TestCase):
    """Real sockets on the loopback interface."""

    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(2)
        self.addCleanup(self.rx.close)
        self.port = self.rx.getsockname()[1]

    def sender(self, setting, interface):
        s = agent_net.UdpSender(setting, self.port, interface=interface)
        self.addCleanup(s.close)
        return s

    def test_packets_leave_from_the_interface_address(self):
        s = self.sender("127.0.0.1", "lo")
        self.assertEqual((s.interface_plan.address, s.targets), ("127.0.0.1", ["127.0.0.1"]))
        self.assertEqual(s._sock.getsockname()[0], "127.0.0.1")
        self.assertTrue(s.send(b'{"type":"status"}'))
        data, src = self.rx.recvfrom(2048)
        self.assertEqual((data, src[0]), (b'{"type":"status"}', "127.0.0.1"))

    def test_missing_interface_sends_nothing(self):
        with self.assertLogs("agent_net", "WARNING"):
            s = self.sender("127.0.0.1", "no-such-if0")
        self.assertIn("missing", s.interface_plan.problem)
        self.assertEqual(s.targets, [])
        self.assertIsNone(s._sock)
        self.assertFalse(s.send(b"x"))
        self.assertIsNone(s._sock)                     # no unbound socket made instead
        self.rx.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(2048)

    def test_address_change_rebinds(self):
        s = self.sender("127.0.0.1", "lo")
        first = s._sock
        moved = agent_net.InterfacePlan("lo", "127.0.0.2", "127.255.255.255")
        with mock.patch.object(agent_net, "plan_interface", return_value=moved), \
                mock.patch.object(agent_net, "route_device", return_value="lo"):
            s.refresh()
        self.assertIsNot(s._sock, first)
        self.assertEqual(s._sock.getsockname()[0], "127.0.0.2")
        self.assertTrue(s.send(b"y"))
        self.assertEqual(self.rx.recvfrom(2048)[1][0], "127.0.0.2")

    def test_default_sender_is_unbound(self):
        s = self.sender("127.0.0.1", "")
        self.assertIsNone(s.interface_plan)
        self.assertEqual(s._sock.getsockname()[0], "0.0.0.0")


class LinuxCNCWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _proc(self, pid, comm):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")

    def test_detects_start_and_stop(self):
        self._proc(1, "systemd")
        self._proc(200, "python3")
        w = agent_runtime.LinuxCNCWatch(proc_root=self.proc, rescan_s=0)
        self.assertFalse(w.alive())
        self._proc(4321, "linuxcncsvr")
        self.assertTrue(w.alive())
        import shutil
        shutil.rmtree(os.path.join(self.proc, "4321"))
        self.assertFalse(w.alive())

    def test_milltask_also_counts(self):
        self._proc(77, "milltask")
        self.assertEqual(agent_runtime.find_linuxcnc_pid(self.proc), 77)


@unittest.skipIf(os.name == "nt", "fcntl locking is POSIX-only")
class SingleInstanceTests(unittest.TestCase):
    def test_second_lock_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agent.lock")
            first = agent_runtime.single_instance(path)
            self.assertIsNotNone(first)
            self.assertIsNone(agent_runtime.single_instance(path))
            first.close()
            again = agent_runtime.single_instance(path)
            self.assertIsNotNone(again)
            again.close()


if __name__ == "__main__":
    unittest.main()
