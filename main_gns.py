from dataclasses import dataclass
import json
import os
from addressing import *


SUPPORTED_IGP = {"OSPF", "RIP"}

@dataclass
class Interface:
    ipv6: str
    ngbr: str

@dataclass
class Router:
    name : str
    interfaces : dict[str,Interface]

@dataclass
class AS:
    asn : str
    igp : str
    routers : dict[str, Router]

@dataclass
class Inventory:
    ases : dict[int, AS]
    router_to_as : dict[str, int]

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
    
    ases: dict[int, AS]={}
    router_to_as: dict[str, int]={}

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
        
        routers : dict[str, Router]={}

        for router_name, router_body in routers_raw.items():
            if router_name in router_to_as:
                raise ValueError(f"router {router_name} is present in different ASes")
            
            int_raw = router_body.get("interfaces")
            interfaces : dict[str, Interface]={}

            for int_name, int_body in int_raw.items():
                ipv6 = str(int_body.get("ipv6", ""))
                ngbr = str(int_body.get("ngbr", ""))

                #if not ngbr: #check loopback addresses
                    #raise ValueError(f"Router {router_name} is isolated :( )")

                interfaces[int_name] = Interface(ipv6=ipv6, ngbr=ngbr)
            routers[router_name] = Router(name = router_name, interfaces=interfaces)
            router_to_as[router_name] = as_number
        ases[as_number] = AS(asn = as_number, igp =igp, routers = routers)
    return Inventory(ases = ases, router_to_as=router_to_as)


def basic_validation(path):
    inventory = parse_info(path)

    for asn, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                if interface_body.ngbr not in inventory.router_to_as:
                    raise ValueError(f"{router_name}:{interface_name} neighor {interface_body.ngbr!r} not found in inventory")
                
    for asn, as_body in inventory.ases.items():
        for router_name, router_body in as_body.routers.items():
            for interface_name, interface_body in router_body.interfaces.items():
                neighbor = interface_body.ngbr
                nasn = inventory.router_to_as[neighbor]
                nrouter = inventory.ases[nasn].routers[neighbor]
                long = 0
                for a,b in nrouter.interfaces.items():
                    if b.ngbr == router_name:
                        long+=1
                if long==0:
                    raise ValueError(f"link not reciprocal in {router_name}, {interface_name}")
                if long>1:
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
    out: dict[str, list[str]]={}

    for router_name, router_body in as_obj.routers.items():
        cmds: list[str]= []
        cmds += ["conf t", "ipv6 unicast-routing"]
        cmds+= [f"ipv6 router rip {rip_name}"]
        for interface_name in sorted(internal[router_name]):
            cmds+= [f"int {interface_name}", f"ipv6 enable", f"ipv6 rip {rip_name} enable", "no shutdown", "exit"]
        cmds += ["end", "wr mem"]
        out[router_name]=cmds
    return out

def ospf_commands(inv, asn):
    as_obj = inv.ases[asn]
    internal = internal_interfaces(inv, asn)
    out: dict[str, list[str]]={}
    X=1
    for router_name, router_body in as_obj.routers.items():
        cmds: list[str]= []
        cmds += ["conf t", "ipv6 unicast-routing"]
        cmds+= [f"ipv6 router ospf {asn}"]
        cmds+= [f"router-id 10.10.10.{X}"]
        X+=1
        for interface_name in sorted(internal[router_name]):
            cmds+= [f"int {interface_name}", f"ipv6 enable", f"ipv6 ospf {asn} area 0", "no shutdown", "exit"]
        cmds += ["end", "write mem"]
        out[router_name]=cmds
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
            if inv.router_to_as.get(ngbr) != asn:
                external_routers.add(router_name)
                break

    return all_routers, external_routers


def loopback(inv, asn):
    as_obj = inv.ases[asn]
    loop = {}
    for router_name, router_body in as_obj.routers.items():
        for interface_name, interface_body in router_body.interfaces.items():
            if interface_name=="Loopback0":
                loop[router_name] = interface_body.ipv6
    return loop

def ibgp_table(inv, asn):
    all_routers, external_routers = all_and_external_routers(inv, asn)
    ibgp_peers = {}
    for router in all_routers:
        ibgp_peers[router] = [loopback(inv, asn)[routers] for routers in all_routers if routers != router]
    return ibgp_peers


def ibgp_commands(inv, asn):
    as_obj = inv.ases[asn]
    ibgp_peers = ibgp_table(inv, asn)
    out: dict[str, list[str]]={}
    for router_name, router_body in as_obj.routers.items():
        commands : list[str]=[]
        commands += ["conf t", "ipv6 unicast-routing"]
        number = int(router_name[1:])
        commands += [f"router bgp {asn}", f"bgp router-id {number}.{number}.{number}.{number}"]
        for e, f in ibgp_peers.items():
            if e!=router_name:
                loop = loopback(inv, asn)[e]
                loop0 = loopback(inv, asn)[router_name]
                commands += [f"neighbor {loop} remote-as {asn}", f"neighbor {loop} update-source {loop0}"]
                commands += ["address-family ipv6 unicast", f"neighbor {loop} activate", f"neighbor {loop} next-hop-self"]
        commands += ["end", "write mem"]
        out[router_name] = commands
    return out


def ebgp_table(inv, asn):
    all_routers, external_routers = all_and_external_routers(inv, asn)
    ebgp_peers = {}
    for router in external_routers:
        for asn2, as_body in inv.ases.items():
            for router_name, router_body in as_body.routers.items():
                for interface_name, interface_body in router_body.interfaces.items():
                    if router_name != router and interface_body.ngbr == router and asn != asn2:
                        ebgp_peers[router] = [router_name, interface_body.ipv6]
    return ebgp_peers


def ebgp_commands(inv, asn):
    as_obj = inv.ases[asn]
    peers = ibgp_table(inv, asn)
    out: dict[str, list[str]]={}
    for router_name, router_body in as_obj.routers.items():
        commands : list[str]=[]
        commands += ["conf t", "ipv6 unicast-routing"]
        number = int(router_name[1:])
        commands += [f"router bgp {asn}", f"bgp router-id {number}.{number}.{number}.{number}"]
        for e, f in peers.items():
            if e!=router_name:
                loop = loopback(inv, asn)[e]
                loop0 = loopback(inv, asn)[router_name]
                commands += [f"neighbor {loop} remote-as {asn}"]
                commands += ["address-family ipv6 unicast", f"neighbor {loop} activate"]
                commands += ["exit-address-family"]
        commands += ["end", "write mem"]
        out[router_name] = commands
    return out


path1 = "/home/kali/Desktop/gns_pro/intent2.json" #path to intent file
inventory = parse_info(path1)

print(rip_commands(inventory, 101), "\n \n", rip_commands(inventory, 102))

""" def creating_output_files():
    inv = parse_info(path1)
    for asn, as_body in inventory.ases.items():
        newpath = f"/home/kali/Desktop/gns_pro/output/{asn}"

        as_igp = as_body.igp

        if as_igp == "RIP":
            comm = rip_commands(inv, asn)
        if as_igp == "OSPF":
            comm += ospf_commands(inv, asn)
        if not os.path.exists(newpath):
            os.makedirs(newpath)
        
        for router_name, router_body in as_body.routers.items():
            newpath_router = f"/home/kali/Desktop/gns_pro/output/{asn}/{router_name}"
            if not os.path.exists(newpath_router):
                os.makedirs(newpath_router)
            for interface_name, interface_body in router_body.interfaces.items():
                with open(f"/home/kali/Desktop/gns_pro/output/{asn}/{router_name}", 'w') as f:
                    f.write() """

"""
client - provider (minimum vital)
and then :
peer-client-provider (3 ASes)

X:Y with X=asn_receiver and Y=asn_sender
ip bgp-community new-format
+ neighbor send community
"""

"""
community definitions:

conf t
 ip community-list standard COMM-CUST permit 65000:100
 ip community-list standard COMM-PEER permit 65000:200
 ip community-list standard COMM-PROV permit 65000:300
end

tag + local-pref :

conf t
 route-map IN-FROM-CUST permit 10
  set community 65000:100 additive
  set local-preference 200

 route-map IN-FROM-PEER permit 10
  set community 65000:200 additive
  set local-preference 100

 route-map IN-FROM-PROV permit 10
  set community 65000:300 additive
  set local-preference 50
end
"""
