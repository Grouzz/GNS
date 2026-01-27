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
    asn: int
    igp: str
    routers: dict[str, Router]


@dataclass
class Inventory:
    ases: dict[int, AS]
    router_to_as: dict[str, int]

def load_file(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_info(path):
    data = load_file(path)
    if not isinstance(data, dict) or "AS" not in data:
        raise ValueError("no AS information in the json file")
    as_raw = data["AS"]
    if not isinstance(as_raw, dict) or not as_raw:
        raise ValueError("Empty AS section")
    ases = {}
    router_to_as = {}

    for asn_str, as_body in as_raw.items():
        as_number = int(asn_str)
        igp = str(as_body.get("igp", "")).upper().strip()
        if igp not in SUPPORTED_IGP:
            raise ValueError(f"IGP '{igp}' not supported")

        routers_raw = as_body.get("routers")
        if not isinstance(routers_raw, dict) or not routers_raw:
            raise ValueError(f"AS{as_number}: missing/empty routers")

        routers = {}
        for router_name, router_body in routers_raw.items():
            int_raw = router_body.get("interfaces")
            if not isinstance(int_raw, dict) or not int_raw:
                raise ValueError(f"AS{as_number}:{router_name}: missing interfaces")

            interfaces = {}
            for int_name, int_body in int_raw.items():
                ipv6 = str(int_body.get("ipv6", "")).strip()
                ngbr = str(int_body.get("ngbr", "")).strip()
                interfaces[int_name] = Interface(ipv6=ipv6, ngbr=ngbr)

            if router_name in router_to_as:
                raise ValueError(f"collision:'{router_name}' appears in multiple AS")

            routers[router_name] = Router(name=router_name, interfaces=interfaces)
            router_to_as[router_name] = as_number

        ases[as_number] = AS(asn=as_number, igp=igp, routers=routers)

    return Inventory(ases=ases, router_to_as=router_to_as)


def find_reverse_interface(inv, src_router, dst_router):
    dst_asn = inv.router_to_as.get(dst_router)
    if dst_asn is None:
        return None
    dst_obj = inv.ases[dst_asn].routers[dst_router]
    for if_name, if_obj in dst_obj.interfaces.items():
        if (if_obj.ngbr or "").strip() == src_router:
            return if_name
    return None


def basic_validation(path):
    inv = parse_info(path)

    for asn, as_obj in inv.ases.items():
        for r_name, r_obj in as_obj.routers.items():
            for if_name, if_obj in r_obj.interfaces.items():
                ng = (if_obj.ngbr or "").strip()
                if not ng:
                    continue
                if ng == r_name:
                    raise ValueError(f"{r_name}:{if_name} points to itself")
                if ng not in inv.router_to_as:
                    raise ValueError(f"{r_name}:{if_name} unknown neighbor '{ng}'")

                rev_if = find_reverse_interface(inv, r_name, ng)
                if rev_if is None:
                    raise ValueError(f"link is  not symmetric: {r_name}:{if_name} -> {ng}")
    return inv


def internal_interfaces(inv, asn):
    as_obj = inv.ases[asn]
    internal = {r: set() for r in as_obj.routers.keys()}
    for r_name, r_obj in as_obj.routers.items():
        for if_name, if_obj in r_obj.interfaces.items():
            ng = (if_obj.ngbr or "").strip()
            if ng and inv.router_to_as.get(ng) == asn:
                internal[r_name].add(if_name)
    return internal


def router_num(name):
    s = name.lstrip("R")
    try:
        return int(s)
    except ValueError:
        return 10**9


def loopbacks(inv, asn):
    as_obj = inv.ases[asn]
    out = {}
    for r_name, r_obj in as_obj.routers.items():
        if "Loopback0" in r_obj.interfaces and r_obj.interfaces["Loopback0"].ipv6:
            out[r_name] = r_obj.interfaces["Loopback0"].ipv6.split("/")[0]
    return out


def ibgp_peers(inv, asn):
    lbs = loopbacks(inv, asn)
    routers = sorted(inv.ases[asn].routers.keys(), key=router_num)
    return {r: [lbs[p] for p in routers if p != r and p in lbs] for r in routers}


def build_link_map(inv):
    link_map = {}
    for _asn, as_obj in inv.ases.items():
        for r_name, r_obj in as_obj.routers.items():
            for if_name, if_obj in r_obj.interfaces.items():
                ng = (if_obj.ngbr or "").strip()
                if not ng:
                    continue
                rev_if = find_reverse_interface(inv, r_name, ng)
                if not rev_if:
                    continue
                link_map[(r_name, if_name)] = (ng, rev_if)
    return link_map


def ebgp_peers(inv, asn):
    as_obj = inv.ases[asn]
    link_map = build_link_map(inv)
    out = {}
    for r_name, r_obj in as_obj.routers.items():
        for if_name, if_obj in r_obj.interfaces.items():
            if if_name.lower().startswith("loopback"):
                continue
            ng = (if_obj.ngbr or "").strip()
            if not ng:
                continue
            ng_asn = inv.router_to_as.get(ng)
            if ng_asn is None or ng_asn == asn:
                continue

            ng_if = None
            if (r_name, if_name) in link_map:
                _, ng_if = link_map[(r_name, if_name)]
            if not ng_if:
                continue
            ng_router = inv.ases[ng_asn].routers[ng]
            ng_ip = ng_router.interfaces[ng_if].ipv6.split("/")[0]
            out.setdefault(r_name, []).append((ng_ip, ng_asn))

    return out
