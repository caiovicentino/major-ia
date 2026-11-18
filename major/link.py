"""Conexão com o anel no macOS.

O macOS pareia o anel sozinho como dispositivo HID e, enquanto essa ligação existe, o anel
para de anunciar: um scan não o encontra. Por isso tentamos primeiro reaproveitar a ligação
do sistema e só depois escanear.
"""
import asyncio
from collections import defaultdict
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
from bleak.backends.device import BLEDevice
from CoreBluetooth import CBUUID

from . import protocol as p

Handler = Callable[[p.Frame], None]


class Ring:
    def __init__(self) -> None:
        self.client: BleakClient | None = None
        self._handlers: list[Handler] = []
        self._waiters: dict[int, list[asyncio.Future]] = defaultdict(list)
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    def on_frame(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def off_frame(self, handler: Handler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def connect(self, tries: int = 8, scan_timeout: float = 15) -> bool:
        manager = CentralManagerDelegate()
        await manager.wait_until_ready()
        for _ in range(tries):
            device = self._system_device(manager) or await BleakScanner.find_device_by_filter(
                lambda d, adv: p.MANUFACTURER_ID in adv.manufacturer_data, timeout=scan_timeout)
            if device is None:
                continue
            client = BleakClient(device, timeout=20)
            try:
                await client.connect()
                await client.start_notify(p.NOTIFY, self._on_notify)
            except Exception:
                await asyncio.sleep(1)
                continue
            self.client = client
            return True
        return False

    @staticmethod
    def _system_device(manager) -> BLEDevice | None:
        services = [CBUUID.UUIDWithString_(u) for u in ("56FF", "180F", "1812", "FEF5")]
        for peripheral in manager.central_manager.retrieveConnectedPeripheralsWithServices_(services):
            if (peripheral.name() or "").startswith("SR116"):
                return BLEDevice(peripheral.identifier().UUIDString(), peripheral.name(), (peripheral, manager))
        return None

    def _on_notify(self, _char, data: bytearray) -> None:
        frame = p.parse(bytes(data))
        if frame is None:
            return
        for future in self._waiters.pop(frame.cmd, []):
            if not future.done():
                future.set_result(frame)
        for handler in self._handlers:
            handler(frame)

    async def send(self, cmd: int, payload: bytes = b"") -> None:
        async with self._write_lock:  # escritas simultâneas travam o CoreBluetooth
            await asyncio.wait_for(self.client.write_gatt_char(p.WRITE, p.build(cmd, payload), response=False), 5)

    async def request(self, cmd: int, payload: bytes = b"", reply: int | None = None, timeout: float = 4) -> p.Frame | None:
        future = asyncio.get_running_loop().create_future()
        self._waiters[reply if reply is not None else cmd].append(future)
        await self.send(cmd, payload)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None

    async def rssi(self) -> int | None:
        """Força do sinal do anel medida pelo Mac, em dBm, ou None se não der para ler. Medido em 15/11/2026: com o
        anel no dedo, a poucos metros do Mac, o sinal ficava entre -68 e -76 e a ligação caía a cada 2 a 4 minutos;
        com o anel parado ao lado do Mac, entre -66 e -71, ela ficou 8 minutos sem cair."""
        try:
            return int(await asyncio.wait_for(self.client._backend.get_rssi(), 2))
        except Exception:
            return None

    async def disconnect(self) -> None:
        if self.client:
            try:
                await asyncio.wait_for(self.client.disconnect(), 5)
            except Exception:
                pass
