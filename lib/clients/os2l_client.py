import time
import logging
import datetime
import netifaces
from typing import List, Optional
from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf, ServiceInfo
from threading import Thread
from lib.clients.os2l_sender import Os2lSender
import lib.clients.os2l_messages as os2l_messages
from lib.analyser.music_analyser import MusicAnalyser

OS2L_SERVICE_NAME = '_os2l._tcp.local.'


class Os2lService:
    def __init__(self, ipv4_address: str, port: int, service_name: str, server: str):
        self.ipv4_address: str = ipv4_address
        self.port: int = port
        self.service_name: str = service_name
        self.server: str = server

    def __str__(self):
        return f"[service_connection: {self.ipv4_address}:{self.port}, service_name: {self.service_name}, server: {self.server}]"


global_services: List[Os2lService] = []
service_discovery_error: str = ''


def get_ip_addresses_for_all_interfaces():
    ips = []
    for iface in netifaces.interfaces():
        iface_data = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in iface_data:
            ips.extend(address['addr'] for address in iface_data[netifaces.AF_INET])
    return ips


def on_service_state_change(zeroconf: Zeroconf,
                            service_type: str,
                            name: str,
                            state_change: ServiceStateChange) -> None:
    global global_services, service_discovery_error
    logging.info(f'[os2l-discovery] service state changed: [name: {name}, type {service_type}, state change: {state_change}]')

    local_ips = get_ip_addresses_for_all_interfaces()

    if state_change is ServiceStateChange.Removed:
        global_services[:] = [service for service in global_services if service.service_name != name]
        return

    if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
        try:
            service_info: Optional[ServiceInfo] = zeroconf.get_service_info(service_type, name)
        except:
            logging.warning(f'[os2l-discovery] errored out on finding service info for name={name}, service_type={service_type}, state_change={state_change.name}')
            return
        if service_info:
            ipv4_addresses: List[str] = service_info.parsed_scoped_addresses(IPVersion.V4Only)
            kept_addresses = [address for address in local_ips if address in ipv4_addresses]
            if len(kept_addresses) == 0:
                return

            os2l_service = Os2lService(kept_addresses[0], service_info.port, service_info.name, service_info.server)
            logging.info(f'[os2l-discovery] found: {os2l_service}')
            global_services[:] = [service for service in global_services
                                  if service.service_name != service_info.name]
            global_services.append(os2l_service)


class Os2lClient:
    def __init__(self, playback_delay_sec: float = 0.0):
        self._playback_delay_sec = playback_delay_sec
        self.os2l_sender: Os2lSender = Os2lSender(playback_delay_sec=playback_delay_sec)

    def set_analyser(self, analyser: MusicAnalyser):
        self.os2l_sender.set_analyser(analyser)

    def start(self):
        # zeroconf blocks and cannot run on the asyncio loop: run it on a thread and join before returning.
        thread = Thread(target=self._find_services)
        thread.start()
        thread.join()

        if service_discovery_error != '':
            raise RuntimeError(f"unable to find correct os2l service: {service_discovery_error}")

        assert len(global_services) == 1, f"more than one os2l service found: {global_services}"
        service = global_services[0]
        self.os2l_sender.start(service.ipv4_address, service.port)
        logging.info(f'[os2l] connected to service: {service})')

    def stop(self):
        if self.os2l_sender.is_running:
            logging.info(f'[os2l] stopping os2l client')
            self.os2l_sender.stop()

    def on_sound_start(self, time_elapsed_ms: int, beat_pos: float, first_downbeat_ms: float, bpm: float):
        self.os2l_sender.send_message(
            os2l_messages.logon_message()
            + os2l_messages.song_loaded_message(time_elapsed_ms, beat_pos, first_downbeat_ms, bpm)
            + os2l_messages.play_start_message(), delay_sec=self._playback_delay_sec)

    def on_sound_stop(self):
        self.os2l_sender.send_message(os2l_messages.play_stop_message(),
                                     delay_sec=self._playback_delay_sec)

    async def send_beat(self, change: bool, pos: int, bpm: float, strength: float):
        message = os2l_messages.beat_message(change, pos, bpm, strength)
        self.os2l_sender.send_message(message)

    def _find_services(self):
        global service_discovery_error
        global_services.clear()
        service_discovery_error = ''
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        service_names = [OS2L_SERVICE_NAME]
        ServiceBrowser(zeroconf, service_names, handlers=[on_service_state_change])
        logging.info('[os2l-discovery] searching for soundswitch services..')
        search_start = datetime.datetime.now()
        while len(global_services) == 0:
            if service_discovery_error != '':
                break
            if datetime.datetime.now() - search_start > datetime.timedelta(seconds=10):
                service_discovery_error = 'unable to find soundswitch service after 10sec'
            time.sleep(0.1)
        zeroconf.close()
