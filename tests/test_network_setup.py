from __future__ import annotations

import subprocess

import network_setup
import proc_utils


# ------------------------------------------------------------ rule naming --

def test_rule_name_is_built_only_from_id_label_and_port():
    name = network_setup.rule_name("ab12cd34", "Game+1", 7778)
    assert name == "ConanOps-ab12cd34-GamePlus1-7778"


def test_rule_name_strips_anything_unexpected_from_the_id():
    assert network_setup.rule_name('a"b&c%d', "Game", 7777) == "ConanOps-abcd-Game-7777"


def test_ps_str_doubles_every_kind_of_single_quote_and_drops_control_chars():
    assert network_setup._ps_str("Bob's") == "'Bob''s'"
    assert network_setup._ps_str("x\u2019y") == "'x\u2019\u2019y'"
    assert network_setup._ps_str("a\nb\x07") == "'ab'"


def test_hostile_server_name_cannot_break_out_of_the_script(monkeypatch):
    """The old batch-file path ran `& Co ...` as its own (elevated)
    command for a name like  Bob"s & Co. Now the name only ever appears
    inside a single-quoted PowerShell literal in -DisplayName."""
    scripts = []
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: set())
    network_setup.add_firewall_rules("ab12cd34", 7777, 27015, display="Bob\"s & Co 100% ' ; calc")

    add_script = scripts[0]
    assert "-DisplayName 'ConanOps - Bob\"s & Co 100% '' ; calc - Game (7777/UDP)'" in add_script
    assert "-Name 'ConanOps-ab12cd34-Game-7777'" in add_script


def test_add_firewall_rules_disables_exe_block_rules_when_given_the_exe(monkeypatch):
    scripts = []
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: set())
    monkeypatch.setattr(network_setup, "firewall_environment",
                        lambda exe_path="": {"checked": True, "block_rules": ["Conan server"], "third_party": []})
    results = network_setup.add_firewall_rules("s1", 7777, 27015, exe_path="C:\\Servers\\x\\Conan.exe")
    assert "Disable-NetFirewallRule" in scripts[0]
    assert "'C:\\Servers\\x\\Conan.exe'" in scripts[0]
    assert any("blocking the server program" in r.message for r in results)


def test_firewall_environment_parses_json(monkeypatch):
    monkeypatch.setattr(network_setup, "_run_ps_readonly", lambda script, timeout=45.0: subprocess.CompletedProcess(
        [], 0, '{"block":"Conan server","third":["Norton"]}\n', ""))
    env = network_setup.firewall_environment("C:\\x.exe")
    assert env == {"checked": True, "block_rules": ["Conan server"], "third_party": ["Norton"]}


def test_firewall_environment_unchecked_on_failure(monkeypatch):
    monkeypatch.setattr(network_setup, "_run_ps_readonly", lambda script, timeout=45.0: None)
    assert network_setup.firewall_environment("C:\\x.exe")["checked"] is False


# -------------------------------------------------------- status (read-only) --

def test_present_rule_names_parses_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda args, **k: subprocess.CompletedProcess(
        args, 0, "ConanOps-ab12cd34-Game-7777\nConanOps-ab12cd34-Query-27015\n", ""))
    assert network_setup.present_rule_names("ab12cd34") == {
        "ConanOps-ab12cd34-Game-7777", "ConanOps-ab12cd34-Query-27015",
    }


def test_present_rule_names_none_when_powershell_fails(monkeypatch):
    def _raise(*a, **k):
        raise OSError("powershell not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert network_setup.present_rule_names("ab12cd34") is None


def test_present_rule_names_none_on_nonzero_exit(monkeypatch):
    """A failed read (e.g. NetSecurity module missing) is "couldn't
    check", never "everything's fine" -- the old netsh check treated
    any output that wasn't English "No rules match" as success."""
    monkeypatch.setattr(subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 1, "", "boom"))
    assert network_setup.present_rule_names("ab12cd34") is None


def test_firewall_status_reports_per_port(monkeypatch):
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: {
        "ConanOps-s1-Game-7777", "ConanOps-s1-Query-27015",
    })
    results = network_setup.firewall_status("s1", 7777, 27015)
    by_label = {r.message.split(" port")[0]: r.success for r in results}
    assert by_label == {"Game": True, "Game+1": False, "Query": True}
    assert all(r.checked for r in results)


def test_firewall_status_unchecked_when_read_fails(monkeypatch):
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: None)
    results = network_setup.firewall_status("s1", 7777, 27015)
    assert all(not r.checked and not r.success for r in results)


# ------------------------------------------------------------------ changes --

def test_add_firewall_rules_is_one_script_and_reports_observed_truth(monkeypatch):
    scripts = []
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: {"ConanOps-s1-Game-7777"})

    results = network_setup.add_firewall_rules("s1", 7777, 27015)

    assert len(scripts) == 1  # one script -> at most one UAC prompt
    assert scripts[0].count("New-NetFirewallRule") == 3
    assert "Remove-NetFirewallRule" in scripts[0]  # stale rules for this id removed first
    assert [r.success for r in results] == [True, False, False]


def test_add_firewall_rules_says_when_the_prompt_was_declined(monkeypatch):
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda script, timeout=90.0: network_setup.RUN_DECLINED)
    monkeypatch.setattr(network_setup, "present_rule_names", lambda sid: set())
    results = network_setup.add_firewall_rules("s1", 7777, 27015)
    assert all("declined" in r.message for r in results)


def test_remove_firewall_rules_handles_many_servers_in_one_call(monkeypatch):
    scripts = []
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    network_setup.remove_firewall_rules(["s1", "s2"], legacy_names=["Chudville", "Second"])
    assert len(scripts) == 1
    assert "'s1'" in scripts[0] and "'s2'" in scripts[0]
    assert "'Chudville'" in scripts[0] and "'Second'" in scripts[0]


def test_remove_firewall_rules_noop_with_nothing_to_remove(monkeypatch):
    calls = []
    monkeypatch.setattr(network_setup, "_run_ps_privileged", lambda *a, **k: calls.append(a))
    assert network_setup.remove_firewall_rules([]) is True
    assert calls == []


def test_privileged_run_elevates_once_when_not_admin(monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    calls = []
    monkeypatch.setattr(proc_utils, "run_elevated_and_wait", lambda exe, params, timeout=60.0: (calls.append(params), 0)[1])
    assert network_setup._run_ps_privileged("exit 0") == network_setup.RUN_OK
    assert len(calls) == 1 and "-Command" in calls[0]
    assert "-EncodedCommand" not in calls[0]  # a classic antivirus trigger


def test_privileged_run_reports_declined(monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    monkeypatch.setattr(proc_utils, "run_elevated_and_wait",
                        lambda exe, params, timeout=60.0: proc_utils.ELEVATION_DECLINED)
    assert network_setup._run_ps_privileged("exit 0") == network_setup.RUN_DECLINED


def test_run_elevated_and_wait_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.run_elevated_and_wait("x.exe", "") == proc_utils.ELEVATION_FAILED


# --------------------------------------------------------------------- UPnP --

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_DESC = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device>
<serviceList>
<service><serviceType>urn:schemas-upnp-org:service:WANPPPConnection:1</serviceType><controlURL>ppp</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType><controlURL>ctl/IPConn</controlURL></service>
</serviceList></device></deviceList></device></deviceList></device></root>"""


def test_services_at_joins_relative_control_urls_and_prefers_ip_connection(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: _FakeResp(_DESC))
    devs = network_setup._services_at("http://192.168.1.1:5000/rootDesc.xml", 1.0)
    assert devs[0].service_type.endswith("WANIPConnection:1")
    assert devs[0].control_url == "http://192.168.1.1:5000/ctl/IPConn"  # no leading slash -- still joined right


def test_services_at_survives_garbage(monkeypatch):
    import http.client
    import urllib.request

    def bad(*a, **k):
        raise http.client.BadStatusLine("garbage")
    monkeypatch.setattr(urllib.request, "urlopen", bad)
    assert network_setup._services_at("http://192.168.1.1/x.xml", 1.0) == []


def test_soap_returns_upnp_error_code_from_fault(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    fault = (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
             b'<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>718</errorCode>'
             b'</UPnPError></detail></s:Fault></s:Body></s:Envelope>')

    def raise_fault(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(fault))
    monkeypatch.setattr(urllib.request, "urlopen", raise_fault)
    dev = network_setup.UpnpDevice("http://192.168.1.1/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1")
    fields, code = network_setup._soap(dev, "AddPortMapping", [])
    assert fields is None and code == 718


def test_map_port_takes_over_its_own_stale_mapping(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1")
    calls = []
    state = {"added": 0}

    def fake_soap(device, action, args, timeout=5.0):
        calls.append(action)
        if action == "AddPortMapping":
            state["added"] += 1
            return (None, 718) if state["added"] == 1 else ({}, None)
        if action == "GetSpecificPortMappingEntry":
            if state["added"] == 1:
                return {"NewInternalClient": "192.168.1.20", "NewPortMappingDescription": "ConanOps s1 Game"}, None
            return {"NewInternalClient": "192.168.1.50"}, None
        if action == "DeletePortMapping":
            return {}, None
        return None, None

    monkeypatch.setattr(network_setup, "_soap", fake_soap)
    assert network_setup.map_port(dev, 7777, "192.168.1.50", "ConanOps s1 Game", server_id="s1") == network_setup.MAP_OK
    assert "DeletePortMapping" in calls


def test_map_port_never_takes_over_another_devices_mapping(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1")

    def fake_soap(device, action, args, timeout=5.0):
        if action == "AddPortMapping":
            return None, 718
        if action == "GetSpecificPortMappingEntry":
            return {"NewInternalClient": "192.168.1.99", "NewPortMappingDescription": "Plex"}, None
        if action == "DeletePortMapping":
            raise AssertionError("must not delete someone else's mapping")
        return None, None

    monkeypatch.setattr(network_setup, "_soap", fake_soap)
    assert network_setup.map_port(dev, 7777, "192.168.1.50", "ConanOps s1 Game", server_id="s1") == network_setup.MAP_CONFLICT


def test_remove_upnp_mappings_only_removes_this_servers(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1")
    monkeypatch.setattr(network_setup, "list_port_mappings", lambda device: [
        {"NewExternalPort": "7777", "NewProtocol": "UDP", "NewPortMappingDescription": "ConanOps s1 Game"},
        {"NewExternalPort": "7779", "NewProtocol": "UDP", "NewPortMappingDescription": "ConanOps s2 Game"},
        {"NewExternalPort": "32400", "NewProtocol": "TCP", "NewPortMappingDescription": "Plex"},
    ])
    deleted = []
    monkeypatch.setattr(network_setup, "delete_port_mapping",
                        lambda device, port, proto="UDP", timeout=5.0: (deleted.append(port), True)[1])
    n = network_setup.remove_upnp_mappings("s1", local_ip="192.168.1.50", device=dev)
    assert n == 1 and deleted == [7777]


def test_reconcile_upnp_flags_double_nat(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", "urn:schemas-upnp-org:service:WANIPConnection:1")
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: dev)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: 0)
    monkeypatch.setattr(network_setup, "map_port", lambda *a, **k: network_setup.MAP_OK)
    monkeypatch.setattr(network_setup, "get_external_ip", lambda device, timeout=5.0: "100.64.3.2")
    r = network_setup.reconcile_upnp("s1", "192.168.1.50", 7777, 27015)
    assert r["upnp_available"] and r["game_port_forwarded"] and r["double_nat"]


def test_reconcile_upnp_without_router(monkeypatch):
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    r = network_setup.reconcile_upnp("s1", "192.168.1.50", 7777, 27015)
    assert r["upnp_available"] is False


# ------------------------------------------------------------------- proc_utils --

def test_is_admin_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.is_admin() is False


def test_is_admin_reads_iuseranadmin_on_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")

    class FakeShell32:
        def IsUserAnAdmin(self):
            return 1

    class FakeWindll:
        shell32 = FakeShell32()

    import ctypes
    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)

    assert proc_utils.is_admin() is True


def test_is_admin_false_if_the_win32_call_itself_fails(monkeypatch):
    """Not elevated is always the safe fallback -- worst case, this
    triggers an elevation prompt that wasn't strictly needed."""
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")

    class FakeShell32:
        def IsUserAnAdmin(self):
            raise OSError("nope")

    class FakeWindll:
        shell32 = FakeShell32()

    import ctypes
    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)

    assert proc_utils.is_admin() is False


def test_shell_execute_runas_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "linux")
    assert proc_utils.shell_execute_runas("cmd.exe", "/c echo hi") is False


def test_shell_execute_runas_true_on_success(monkeypatch):
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, exe, params, cwd, show):
            assert verb == "runas"
            return 42  # > 32 means success per the Win32 docs

    class FakeWindll:
        shell32 = FakeShell32()

    import ctypes
    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)

    assert proc_utils.shell_execute_runas("cmd.exe", "/c echo hi") is True


def test_shell_execute_runas_false_when_uac_declined(monkeypatch):
    """ShellExecuteW returns a small error code (<= 32), not an
    exception, when the UAC prompt is declined."""
    monkeypatch.setattr(proc_utils.sys, "platform", "win32")

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, exe, params, cwd, show):
            return 5  # ERROR_ACCESS_DENIED

    class FakeWindll:
        shell32 = FakeShell32()

    import ctypes
    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)

    assert proc_utils.shell_execute_runas("cmd.exe", "/c echo hi") is False
