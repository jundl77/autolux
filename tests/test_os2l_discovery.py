from unittest.mock import Mock

import pytest
from zeroconf import ServiceStateChange

from lib.clients import os2l_client as discovery


@pytest.fixture(autouse=True)
def isolated_discovery(monkeypatch):
    monkeypatch.setattr(discovery, 'global_services', [])
    monkeypatch.setattr(discovery, 'service_discovery_error', '')
    monkeypatch.setattr(discovery, 'get_ip_addresses_for_all_interfaces',
                        lambda: ['192.168.178.22', '192.168.56.1', '172.19.144.1'])


def announce(addresses, name='SoundSwitch._os2l._tcp.local.', state=ServiceStateChange.Added):
    info = Mock(port=57126, server='show.local.')
    info.name = name
    info.parsed_scoped_addresses.return_value = addresses
    z = Mock()
    z.get_service_info.return_value = info
    discovery.on_service_state_change(z, discovery.OS2L_SERVICE_NAME, name, state)


def test_one_service_with_three_local_interfaces_is_one_soundswitch():
    announce(['172.19.144.1', '192.168.56.1', '192.168.178.22'])
    assert discovery.service_discovery_error == ''
    assert len(discovery.global_services) == 1
    assert discovery.global_services[0].ipv4_address == '192.168.178.22'


def test_repeated_announcements_and_updates_do_not_duplicate_a_service():
    announce(['192.168.178.22'])
    announce(['192.168.178.22'])
    announce(['192.168.56.1'], state=ServiceStateChange.Updated)
    assert len(discovery.global_services) == 1
    assert discovery.global_services[0].ipv4_address == '192.168.56.1'


def test_a_remote_soundswitch_is_not_mistaken_for_the_local_show():
    announce(['192.168.178.245'])
    assert discovery.global_services == []


def test_distinct_service_names_remain_distinct_and_removal_is_by_name():
    announce(['192.168.178.22'], name='one')
    announce(['192.168.178.22'], name='two')
    assert len(discovery.global_services) == 2
    announce([], name='one', state=ServiceStateChange.Removed)
    assert [s.service_name for s in discovery.global_services] == ['two']
