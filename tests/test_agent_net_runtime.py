"""Tests for send-target discovery and the LinuxCNC process watch.
Run: python -m unittest -v
"""
import os
import sys
import tempfile
import unittest

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
