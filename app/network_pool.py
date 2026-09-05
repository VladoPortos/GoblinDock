"""Parsing and usable-address rules for persisted static network pools."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterator, Optional, Union


IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class StaticPoolError(ValueError):
    """A persisted static pool is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class StaticPool:
    network: IPNetwork
    start: IPAddress
    end: IPAddress
    gateway: Optional[IPAddress]

    def is_reserved(self, address: IPAddress) -> bool:
        """Return whether an address is unusable infrastructure space."""
        if address == self.network.network_address:
            return True
        if isinstance(self.network, ipaddress.IPv4Network) and \
                address == self.network.broadcast_address:
            return True
        return self.gateway is not None and address == self.gateway

    def iter_usable(self) -> Iterator[IPAddress]:
        """Yield usable addresses, defensively skipping reserved legacy slots."""
        address_type = type(self.network.network_address)
        for value in range(int(self.start), int(self.end) + 1):
            address = address_type(value)
            if not self.is_reserved(address):
                yield address

    @property
    def usable_total(self) -> int:
        """Count usable slots in O(1), including for very large IPv6 ranges."""
        total = int(self.end) - int(self.start) + 1
        reserved = {self.network.network_address}
        if isinstance(self.network, ipaddress.IPv4Network):
            reserved.add(self.network.broadcast_address)
        if self.gateway is not None:
            reserved.add(self.gateway)
        return total - sum(self.start <= address <= self.end for address in reserved)


def parse_static_pool(
    subnet_cidr: str,
    range_start: str,
    range_end: str,
    gateway: str = "",
) -> StaticPool:
    """Parse a complete static pool while retaining safe legacy reserved ranges."""
    if not (range_start or "").strip() or not (range_end or "").strip():
        raise StaticPoolError("a static network requires both range_start and range_end")
    try:
        network = ipaddress.ip_network((subnet_cidr or "").strip(), strict=False)
    except ValueError as exc:
        raise StaticPoolError(
            "a static network needs a valid subnet_cidr (e.g. 10.0.50.0/24)"
        ) from exc
    try:
        start = ipaddress.ip_address(range_start.strip())
    except ValueError as exc:
        raise StaticPoolError("range_start must be a valid IP address") from exc
    try:
        end = ipaddress.ip_address(range_end.strip())
    except ValueError as exc:
        raise StaticPoolError("range_end must be a valid IP address") from exc
    if start.version != end.version or start.version != network.version:
        raise StaticPoolError("subnet and static range must use the same IP family")
    if start > end:
        raise StaticPoolError("range_start must be <= range_end")
    if start not in network or end not in network:
        raise StaticPoolError("the IP range is outside the subnet")

    parsed_gateway: Optional[IPAddress] = None
    if (gateway or "").strip():
        try:
            parsed_gateway = ipaddress.ip_address(gateway.strip())
        except ValueError as exc:
            raise StaticPoolError("gateway must be a valid IP address") from exc
        if parsed_gateway.version != network.version:
            raise StaticPoolError("subnet and gateway must use the same IP family")
        if parsed_gateway not in network:
            raise StaticPoolError("gateway is outside the subnet")

    return StaticPool(network, start, end, parsed_gateway)
