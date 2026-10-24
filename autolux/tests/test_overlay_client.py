import pytest

from autolux.lib.clients.overlay_client import DmxOverlays, MAX_NUM_DMX_DEVICES


def test_overlay_capacity_rejects_extra_entry_without_corrupting_full_packet():
    overlays = DmxOverlays()
    for index in range(MAX_NUM_DMX_DEVICES):
        assert overlays.add_overlay(index, 1, [index], is_active=True) == index
    full_packet = overlays.pack()

    with pytest.raises(ValueError, match="out of available DMX devices"):
        overlays.add_overlay(0, 1, [255], is_active=True)

    assert overlays.get_num_overlays() == MAX_NUM_DMX_DEVICES
    assert overlays.pack() == full_packet
