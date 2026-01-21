from dataclasses import dataclass
import json

SUPPORTED_IGP = {"OSPF", "RIP"}
@dataclass
class Interface:
    ipv6: str
    ngbr: str

@dataclass
class Router:
    name: str
    interfaces: dict[str, Interface]


@dataclass
class AS:
    asn: str
    igp: str
    routers: dict[str, Router]



@dataclass
class Inventory:
    ases: dict[int, AS]
    router_to_as: dict[str, int]


def load_file(path):
    with open(path) as f:
        data = json.load(f)
    return data

def parse_info(path):
    data = load_file(path)
    
    if not isinstance(data, dict) or "AS" not in data:
        raise ValueError("No AS information in the json file")
    as_raw = data["AS"]

    if not isinstance(as_raw, dict) or not as_raw:
        raise ValueError("Empty AS")
    
    ases: dict[int, AS] = {}
    router_to_as: dict[str, int] = {}

    for asn_str, as_body in as_raw.items():
        as_number = int(asn_str)
        if as_number in ases:
            raise ValueError(f"AS {as_number} present more than one time in the set")
        igp = str(as_body.get("igp", "")).upper().strip()

        if igp not in SUPPORTED_IGP:
            raise ValueError(f"{igp} not supported in this project, choose from this list : {SUPPORTED_IGP}")
        
        routers_raw = as_body.get("routers")
        if not routers_raw or not isinstance(routers_raw, dict):
            raise ValueError(f"AS {as_number} has no routers, or verify the syntax of dict structures in python")
        
        routers: dict[str, Router] = {}

        for router_name, router_body in routers_raw.items():
            if router_name in router_to_as:
                raise ValueError(f"router {router_name} is present in different ASes")
            
            int_raw = router_body.get("interfaces")
            interfaces: dict[str, Interface] = {}

            for int_name, int_body in int_raw.items():
                ipv6 = str(int_body.get("ipv6", ""))
                ngbr = str(int_body.get("ngbr", ""))

                interfaces[int_name] = Interface(ipv6=ipv6, ngbr=ngbr)
            routers[router_name] = Router(name=router_name, interfaces=interfaces)
            router_to_as[router_name] = as_number
        ases[as_number] = AS(asn=as_number, igp=igp, routers=routers)
    return Inventory(ases=ases, router_to_as=router_to_as)


def basic_validation(path):
    inventory = parse_info(path)

    for _, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                if interface_body.ngbr and interface_body.ngbr not in inventory.router_to_as:
                    raise ValueError(f"{router_name}:{interface_name} neighbor {interface_body.ngbr!r} not found in inventory")
                
    for _, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                neighbor = interface_body.ngbr
                if not neighbor:
                    continue
                nasn = inventory.router_to_as[neighbor]
                nrouter = inventory.ases[nasn].routers[neighbor]
                long = 0
                for a, b in nrouter.interfaces.items():
                    if b.ngbr == router_name:
                        long += 1
                if long == 0:
                    raise ValueError(f"link not reciprocal in {router_name}, {interface_name}")
                if long > 1:
                    raise ValueError(f"multiple interfaces pointing to {router_name}")
    return inventory

def internal_interfaces(inv, asn):
    as_obj = inv.ases[asn]
    internal: dict[str, set[str]] = {router_name: set() for router_name in as_obj.routers.keys()}

    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            neighbor = interface_body.ngbr
            if neighbor in inv.router_to_as and inv.router_to_as[neighbor] == asn:
                internal[router_name].add(interface_name)
    return internal


def rip_commands(inv, asn):
    as_obj = inv.ases[asn]
    internal = internal_interfaces(inv, asn)
    rip_name = f"AS{asn}"
    out: dict[str, list[str]] = {}

    for router_name, router_body in as_obj.routers.items():
        cmds: list[str] = []
        cmds += ["conf t", "ipv6 unicast-routing"]
        cmds += [f"ipv6 router rip {rip_name}"]
        for interface_name in sorted(internal[router_name]):
            cmds += [f"int {interface_name}", f"ipv6 enable", f"ipv6 rip {rip_name} enable", "no shutdown", "exit"]
        cmds += ["end", "wr mem"]
        out[router_name] = cmds
    return out


def ospf_commands(inv, asn):
    as_obj = inv.ases[asn]
    internal = internal_interfaces(inv, asn)
    out: dict[str, list[str]] = {}
    X = 1

    for router_name, router_body in as_obj.routers.items():
        cmds: list[str] = []
        cmds += ["conf t", "ipv6 unicast-routing"]
        cmds += [f"ipv6 router ospf {asn}"]
        cmds += [f"router-id 10.10.10.{X}"]
        X += 1
        for interface_name in sorted(internal[router_name]):
            cmds += [f"int {interface_name}", f"ipv6 enable", f"ipv6 ospf {asn} area 0", "no shutdown", "exit"]
        cmds += ["end", "write mem"]
        out[router_name] = cmds
    return out


def all_and_external_routers(inv, asn):
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


def loopback(inv, asn):
    as_obj = inv.ases[asn]
    loop = {}
    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name == "Loopback0":
                #extracting the ip
                ipv6_addr = interface_body.ipv6
                if "/" in ipv6_addr:
                    ipv6_addr = ipv6_addr.split("/")[0]
                loop[router_name] = ipv6_addr
    return loop

def ibgp_table(inv, asn):
    all_routers, external_routers = all_and_external_routers(inv, asn)
    loopbacks = loopback(inv, asn)
    ibgp_peers = {}
    
    for router in all_routers:
        ibgp_peers[router] = [loopbacks[peer_router] for peer_router in all_routers if peer_router != router and peer_router in loopbacks]
    return ibgp_peers

def ibgp_commands(inv, asn):
    as_obj = inv.ases[asn]
    ibgp_peers = ibgp_table(inv, asn)
    loopbacks = loopback(inv, asn)
    out: dict[str, list[str]] = {}

    for router_name, router_body in as_obj.routers.items():
        commands: list[str] = []
        commands += ["conf t", "ipv6 unicast-routing"]
        
        number = int(router_name[1:]) #for routr id
        commands += [f"router bgp {asn}", f"bgp router-id {number}.{number}.{number}.{number}"]

        if router_name not in loopbacks:
            continue
        loop0 = loopbacks[router_name]
        
        for peer_loop in ibgp_peers.get(router_name, []):
            commands += [f"neighbor {peer_loop} remote-as {asn}"]
            commands += [f"neighbor {peer_loop} update-source Loopback0"]

        if ibgp_peers.get(router_name):
            commands += ["address-family ipv6 unicast"]
            for peer_loop in ibgp_peers[router_name]:
                commands += [f"neighbor {peer_loop} activate"]
                commands += [f"neighbor {peer_loop} next-hop-self"]
            commands += ["exit-address-family"]
        
        commands += ["end", "write mem"]
        out[router_name] = commands
    
    return out


def ebgp_table(inv, asn):
    as_obj = inv.ases[asn]
    ebgp_peers: dict[str, list] = {}

    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name.startswith("Loopback"):
                continue
            neighbor = interface_body.ngbr
            if not neighbor:
                continue

            neighbor_asn = inv.router_to_as.get(neighbor)
            if neighbor_asn and neighbor_asn != asn: #neighbor in diff AS
                neighbor_router = inv.ases[neighbor_asn].routers[neighbor]
                for n_if_name, n_if_body in neighbor_router.interfaces.items():
                    if n_if_body.ngbr == router_name:
                        neighbor_ip = n_if_body.ipv6
                        if "/" in neighbor_ip:
                            neighbor_ip = neighbor_ip.split("/")[0]
                        
                        if router_name not in ebgp_peers:
                            ebgp_peers[router_name] = []
                        
                        ebgp_peers[router_name].append({
                            "neighbor_ip": neighbor_ip,
                            "neighbor_asn": neighbor_asn,
                            "local_interface": interface_name})
                        break

    return ebgp_peers


def ebgp_commands(inv, asn):
    as_obj = inv.ases[asn]
    ebgp_peers = ebgp_table(inv, asn)
    out: dict[str, list[str]] = {}

    for router_name, router_body in as_obj.routers.items():
        commands: list[str] = []
        commands += ["conf t", "ipv6 unicast-routing"]
        number = int(router_name[1:])
        commands += [f"router bgp {asn}", f"bgp router-id {number}.{number}.{number}.{number}"]
        if router_name in ebgp_peers:
            for peer_info in ebgp_peers[router_name]:
                peer_ip = peer_info["neighbor_ip"]
                peer_asn = peer_info["neighbor_asn"]
                
                commands += [f"neighbor {peer_ip} remote-as {peer_asn}"]
            commands += ["address-family ipv6 unicast"]
            for peer_info in ebgp_peers[router_name]:
                peer_ip = peer_info["neighbor_ip"]
                commands += [f"neighbor {peer_ip} activate"]
            commands += ["exit-address-family"]
        
        commands += ["end", "write mem"]
        out[router_name] = commands
    
    return out