from dataclasses import dataclass
import json
import re
SUPPORTED_IGP = {"OSPF", "RIP"}


@dataclass
class Interface:
    ipv6: str
    ngbr: str
    relationship: str = ""


@dataclass
class Router:
    name: str
    interfaces: dict[str, Interface]


@dataclass
class AS:
    asn: int
    igp: str
    routers: dict[str, Router]


@dataclass
class Inventory:
    ases: dict[int, AS]
    router_to_as: dict[str, int]


def load_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def router_number(router_name: str) -> int:
    """
    Extracts the numeric part of a router name like 'R3' -> 3.
    Falls back to 1 if not found.
    """
    m = re.match(r"^[Rr](\d+)$", router_name.strip())
    if not m:
        return 1
    return int(m.group(1))


def router_id_v4(router_name: str, base: str = "10.10.10.") -> str:
    n = router_number(router_name)
    # Keep it within 1..254 for safety on classic IOS.
    last = max(1, min(254, n))
    return f"{base}{last}"


def parse_info(path: str) -> Inventory:
    data = load_file(path)

    if not isinstance(data, dict) or "AS" not in data:
        raise ValueError("No AS information in the json file")

    as_raw = data["AS"]
    if not isinstance(as_raw, dict) or not as_raw:
        raise ValueError("Empty AS set")

    ases: dict[int, AS] = {}
    router_to_as: dict[str, int] = {}

    for asn_str, as_body in as_raw.items():
        as_number = int(asn_str)
        if as_number in ases:
            raise ValueError(f"AS {as_number} present more than one time in the set")

        igp = str(as_body.get("igp", "")).upper().strip()
        if igp not in SUPPORTED_IGP:
            raise ValueError(f"{igp} not supported, choose from {SUPPORTED_IGP}")

        routers_raw = as_body.get("routers")
        if not routers_raw or not isinstance(routers_raw, dict):
            raise ValueError(f"AS {as_number} has no routers, or invalid dict syntax")

        routers: dict[str, Router] = {}

        for router_name, router_body in routers_raw.items():
            if router_name in router_to_as:
                raise ValueError(f"router {router_name} is present in different ASes")

            int_raw = router_body.get("interfaces")
            if not isinstance(int_raw, dict) or not int_raw:
                raise ValueError(f"{router_name} has no interfaces or invalid interface dict")

            interfaces: dict[str, Interface] = {}
            for int_name, int_body in int_raw.items():
                ipv6 = str(int_body.get("ipv6", "")).strip()
                ngbr = str(int_body.get("ngbr", "")).strip()
                relationship = str(int_body.get("relationship", "")).strip()
                interfaces[int_name] = Interface(ipv6=ipv6, ngbr=ngbr, relationship=relationship)

            routers[router_name] = Router(name=router_name, interfaces=interfaces)
            router_to_as[router_name] = as_number

        ases[as_number] = AS(asn=as_number, igp=igp, routers=routers)

    return Inventory(ases=ases, router_to_as=router_to_as)


def basic_validation(path: str) -> Inventory:
    """
    Validates:
      - Every neighbor exists
      - Every link is reciprocal (exactly one interface on the other router points back)
    """
    inventory = parse_info(path)

    # neighbor existence
    for _, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                if interface_body.ngbr and interface_body.ngbr not in inventory.router_to_as:
                    raise ValueError(
                        f"{router_name}:{interface_name} neighbor {interface_body.ngbr!r} not found in inventory"
                    )

    # reciprocity and uniqueness
    for _, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                neighbor = interface_body.ngbr
                if not neighbor:
                    continue

                nasn = inventory.router_to_as[neighbor]
                nrouter = inventory.ases[nasn].routers[neighbor]

                matches = 0
                for _, n_if_body in nrouter.interfaces.items():
                    if n_if_body.ngbr == router_name:
                        matches += 1

                if matches == 0:
                    raise ValueError(f"link not reciprocal: {router_name}:{interface_name} -> {neighbor}")
                if matches > 1:
                    raise ValueError(f"multiple interfaces on {neighbor} pointing to {router_name} (ambiguous link)")

    return inventory


def internal_interfaces(inv: Inventory, asn: int) -> dict[str, set[str]]:
    """
    Returns interfaces considered internal to the AS:
      - Loopback0
      - Interfaces whose neighbor is in the same AS
    """
    as_obj = inv.ases[asn]
    internal: dict[str, set[str]] = {router_name: set() for router_name in as_obj.routers.keys()}

    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name == "Loopback0":
                internal[router_name].add(interface_name)
                continue

            neighbor = interface_body.ngbr
            if neighbor and neighbor in inv.router_to_as and inv.router_to_as[neighbor] == asn:
                internal[router_name].add(interface_name)

    return internal


def rip_commands(inv: Inventory, asn: int) -> dict[str, list[str]]:
    """
    RIPng process config (interface activation is done under interfaces).
    """
    as_obj = inv.ases[asn]
    rip_name = f"AS{asn}"
    out: dict[str, list[str]] = {}
    for router_name in as_obj.routers.keys():
        out[router_name] = [f"ipv6 router rip {rip_name}"]
    return out


def ospf_commands(inv: Inventory, asn: int) -> dict[str, list[str]]:
    """
    OSPFv3 process config.
    IMPORTANT: router-id must be a subcommand of 'ipv6 router ospf <pid>'.
    """
    as_obj = inv.ases[asn]
    out: dict[str, list[str]] = {}
    for router_name in as_obj.routers.keys():
        rid = router_id_v4(router_name)
        out[router_name] = [f"ipv6 router ospf {asn}", f" router-id {rid}"]
    return out


def loopback(inv: Inventory, asn: int) -> dict[str, str]:
    as_obj = inv.ases[asn]
    loop: dict[str, str] = {}
    for router_name, router_body in as_obj.routers.items():
        if "Loopback0" not in router_body.interfaces:
            continue
        ipv6_addr = router_body.interfaces["Loopback0"].ipv6
        if "/" in ipv6_addr:
            ipv6_addr = ipv6_addr.split("/")[0]
        loop[router_name] = ipv6_addr
    return loop


def all_and_external_routers(inv: Inventory, asn: int) -> tuple[set[str], set[str]]:
    """
    External router = has at least one interface to a router in another AS (excluding loopbacks).
    """
    as_obj = inv.ases[asn]
    all_routers = set(as_obj.routers.keys())
    external_routers = set()

    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name.startswith("Loopback"):
                continue
            ngbr = interface_body.ngbr
            if ngbr and inv.router_to_as.get(ngbr) != asn:
                external_routers.add(router_name)
                break

    return all_routers, external_routers


def ibgp_table(inv: Inventory, asn: int) -> dict[str, list[str]]:
    """
    Full-mesh iBGP peers inside AS, using loopback IPv6 addresses.
    """
    all_routers, _ = all_and_external_routers(inv, asn)
    loopbacks = loopback(inv, asn)
    ibgp_peers: dict[str, list[str]] = {}

    for router in all_routers:
        ibgp_peers[router] = [
            loopbacks[peer_router]
            for peer_router in all_routers
            if peer_router != router and peer_router in loopbacks
        ]
    return ibgp_peers


def ebgp_table(inv: Inventory, asn: int) -> dict[str, list[dict]]:
    """
    Collects eBGP neighbors for routers in the AS.
    Returns dict: router_name -> list of {neighbor_ip, neighbor_asn, local_interface}
    """
    as_obj = inv.ases[asn]
    ebgp_peers: dict[str, list[dict]] = {}

    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name.startswith("Loopback"):
                continue
            neighbor = interface_body.ngbr
            if not neighbor:
                continue

            neighbor_asn = inv.router_to_as.get(neighbor)
            if neighbor_asn and neighbor_asn != asn:
                neighbor_router = inv.ases[neighbor_asn].routers[neighbor]
                for _, n_if_body in neighbor_router.interfaces.items():
                    if n_if_body.ngbr == router_name:
                        neighbor_ip = n_if_body.ipv6
                        if "/" in neighbor_ip:
                            neighbor_ip = neighbor_ip.split("/")[0]
                        ebgp_peers.setdefault(router_name, []).append(
                            {
                                "neighbor_ip": neighbor_ip,
                                "neighbor_asn": neighbor_asn,
                                "local_interface": interface_name,
                                "neighbor_name": neighbor,
                            }
                        )
                        break

    return ebgp_peers


def ibgp_commands(inv: Inventory, asn: int) -> dict[str, list[str]]:
    """
    Basic iBGP without policies. Kept for --no-policies mode.
    Returns a ready-to-paste BGP block (proper indentation included).
    """
    as_obj = inv.ases[asn]
    ibgp_peers = ibgp_table(inv, asn)
    loopbacks = loopback(inv, asn)
    out: dict[str, list[str]] = {}

    for router_name in as_obj.routers.keys():
        rid = router_id_v4(router_name, base=f"{router_number(router_name)}.{router_number(router_name)}.{router_number(router_name)}.")
        # Keep old style router-id for compatibility if someone relies on it, but ensure valid.
        rid = f"{router_number(router_name)}.{router_number(router_name)}.{router_number(router_name)}.{router_number(router_name)}"

        lines: list[str] = [f"router bgp {asn}", f" bgp router-id {rid}", " no bgp default ipv4-unicast"]

        for peer_loop in ibgp_peers.get(router_name, []):
            lines += [f" neighbor {peer_loop} remote-as {asn}", f" neighbor {peer_loop} update-source Loopback0"]

        lines += [" address-family ipv6 unicast"]
        # originate local loopback
        lo = as_obj.routers[router_name].interfaces.get("Loopback0")
        if lo and lo.ipv6:
            lines += [f"  network {lo.ipv6}"]
        for peer_loop in ibgp_peers.get(router_name, []):
            lines += [f"  neighbor {peer_loop} activate", f"  neighbor {peer_loop} next-hop-self"]
        lines += [" exit-address-family"]

        out[router_name] = lines

    return out


def ebgp_commands(inv: Inventory, asn: int) -> dict[str, list[str]]:
    """
    Basic eBGP without policies. Kept for --no-policies mode.
    Returns a ready-to-paste BGP block (proper indentation included).
    """
    as_obj = inv.ases[asn]
    ebgp_peers = ebgp_table(inv, asn)
    out: dict[str, list[str]] = {}

    for router_name in as_obj.routers.keys():
        rid = f"{router_number(router_name)}.{router_number(router_name)}.{router_number(router_name)}.{router_number(router_name)}"
        lines: list[str] = [f"router bgp {asn}", f" bgp router-id {rid}", " no bgp default ipv4-unicast"]

        for peer in ebgp_peers.get(router_name, []):
            lines += [f" neighbor {peer['neighbor_ip']} remote-as {peer['neighbor_asn']}"]

        lines += [" address-family ipv6 unicast"]
        lo = as_obj.routers[router_name].interfaces.get("Loopback0")
        if lo and lo.ipv6:
            lines += [f"  network {lo.ipv6}"]
        for peer in ebgp_peers.get(router_name, []):
            lines += [f"  neighbor {peer['neighbor_ip']} activate"]
        lines += [" exit-address-family"]

        out[router_name] = lines
    return out
