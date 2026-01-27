import os
import sys
import argparse
from pathlib import Path
from utils import *
from addressing import *


class Network: #intent -> filled intent (IPv6 addressing) -> per-router startup conf
    def __init__(self, intent_path, output_dir="./output"):
        self.intent_path = intent_path
        self.output_dir = output_dir
        self.intent_data = None
        self.inventory = None
        self.filled_intent_path = None

    def run(self):
        try:
            self.load_and_validate()
            self.fill_addresses()
            self.generate_configurations()
            print("Configuration generation successful.")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    def load_and_validate(self):
        self.intent_data = load_intent(self.intent_path)
        self.inventory = basic_validation(self.intent_path)

    def fill_addresses(self):
        if self.intent_data is None:
            raise ValueError("intent not loaded")

        filled = fill_ipv6_intra_as(self.intent_data)
        filled = fill_ipv6_ebgp_links(filled)
        filled = fill_loopbacks(filled)

        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.filled_intent_path = str(out_dir / "intent_filled.json")
        save_intent(filled, self.filled_intent_path)

    def generate_configurations(self):
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        if not self.filled_intent_path or not Path(self.filled_intent_path).exists():
            raise ValueError("filled intent not found (fill_addresses failed?)")

        self.inventory = parse_info(self.filled_intent_path)

        for asn, as_obj in self.inventory.ases.items():
            as_dir = os.path.join(self.output_dir, f"AS{asn}")
            Path(as_dir).mkdir(parents=True, exist_ok=True)
            internal = internal_interfaces(self.inventory, asn)
            lbs = loopbacks(self.inventory, asn)
            i_peers = ibgp_peers(self.inventory, asn)
            e_peers = ebgp_peers(self.inventory, asn)
            for router_name in as_obj.routers.keys():
                config = self.build_router_config(
                    router_name=router_name,
                    asn=asn,
                    igp=as_obj.igp,
                    internal_ifaces=sorted(internal.get(router_name, set())),
                    loopback_ip=lbs.get(router_name, ""),
                    ibgp_peer_loops=i_peers.get(router_name, []),
                    ebgp_neighbors=e_peers.get(router_name, []),)

                cfg_path = os.path.join(as_dir, f"{router_name}_config.cfg")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(config)

    def iface_sort_key(self, if_name):
        if if_name.lower().startswith("loopback"):
            return (0, if_name) 
        else :
            return (1, if_name)

    def build_router_config(self, *, router_name, asn, igp, internal_ifaces, loopback_ip, ibgp_peer_loops, ebgp_neighbors):
        if self.inventory is None:
            raise ValueError("inv not loaded")

        if not loopback_ip:
            raise ValueError(f"{router_name} missing Loopback0 IPv6")

        r_obj = self.inventory.ases[asn].routers[router_name]

        def igp_iface_lines():
            if igp == "RIP":
                return [f"ipv6 rip AS{asn} enable"]
            if igp == "OSPF":
                return [f"ipv6 ospf {asn} area 0"]
            raise ValueError(f"Unsupported IGP: {igp}")

        lines = [
            "!",
            f"hostname {router_name}",
            "!",
            "no ip domain lookup",
            "ip cef",
            "ipv6 unicast-routing",
            "ipv6 cef",
            "!",]

        #IGP
        if igp == "RIP":
            lines += [f"ipv6 router rip AS{asn}", "!"]
        elif igp == "OSPF":
            rnum = int(router_name.lstrip("Rr") or "1")
            rid = f"10.10.{asn % 256}.{rnum}"
            lines += [f"ipv6 router ospf {asn}", f"router-id {rid}", "!"]
        else:
            raise ValueError(f"Unsupported IGP: {igp}")

        #interfaces
        for if_name in sorted(r_obj.interfaces.keys(), key=self.iface_sort_key):
            if_obj = r_obj.interfaces[if_name]
            if not if_obj.ipv6:
                continue
            lines += [
                f"interface {if_name}",
                "no ip address",
                "ipv6 enable",
                f"ipv6 address {if_obj.ipv6}",
                "no shutdown",]
            if if_name == "Loopback0" or if_name in internal_ifaces:
                lines += igp_iface_lines()
            lines.append("!")

        #bgp
        rnum = int(router_name.lstrip("Rr") or "1")
        bgp_rid = f"{rnum}.{rnum}.{rnum}.{rnum}"

        lines += [
            f"router bgp {asn}",
            f"bgp router-id {bgp_rid}",
            "bgp log-neighbor-changes",
            "no bgp default ipv4-unicast",]
        
        for peer_loop in sorted(set(ibgp_peer_loops)):
            lines += [
                f"neighbor {peer_loop} remote-as {asn}",
                f"neighbor {peer_loop} update-source Loopback0",]

        for peer_ip, peer_asn in sorted(set(ebgp_neighbors)):
            lines += [f"neighbor {peer_ip} remote-as {peer_asn}"]

        lines += ["address-family ipv6 unicast", f"network {loopback_ip}/128",]

        for peer_loop in sorted(set(ibgp_peer_loops)):
            lines += [f"neighbor {peer_loop} activate", f"neighbor {peer_loop} next-hop-self",]

        for peer_ip, _ in sorted(set(ebgp_neighbors)):
            lines += [f"neighbor {peer_ip} activate"]

        lines += ["exit-address-family", "!", "end", "",]
        return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("intent_file")
    parser.add_argument("-o", "--output", default="./output")
    args = parser.parse_args()
    Network(args.intent_file, args.output).run()

if __name__ == "__main__":
    main()
