import os
import sys
from pathlib import Path

from utils import (
    parse_info,
    basic_validation,
    rip_commands,
    ospf_commands,
    internal_interfaces,
    ibgp_commands,
    ebgp_commands,
)
from addressing import (
    load_intent,
    save_intent,
    fill_ipv6_intra_as,
    fill_ipv6_ebgp_links,
    fill_loopbacks,
)
from policies import (
    validate_relationships,
    build_bgp_with_policies,
)


class Network:
    def __init__(self, intent_path: str, output_dir: str = "./output", use_policies: bool = True):
        self.intent_path = intent_path
        self.output_dir = output_dir
        self.use_policies = use_policies
        self.intent_data = None
        self.inventory = None

    def run(self) -> None:
        try:
            self.load_and_validate()
            print("loading intent file: done")

            if self.use_policies:
                self.validate_policies()
                print("validating bgp relationships: done")

            self.fill_addresses()
            print("filling ipv6 addresses: done")

            self.validate_filled_intent()
            print("all interfaces have IPs + correct reciprocity: done")

            self.generate_configurations()
            print("confs generated: done")

        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    def load_and_validate(self) -> None:
        self.intent_data = load_intent(self.intent_path)
        self.inventory = basic_validation(self.intent_path)

    def validate_policies(self) -> None:
        validate_relationships(self.intent_data)

    def fill_addresses(self) -> None:
        self.intent_data = fill_ipv6_intra_as(self.intent_data)
        self.intent_data = fill_ipv6_ebgp_links(self.intent_data)
        self.intent_data = fill_loopbacks(self.intent_data)

        filled_path = os.path.join(os.path.dirname(self.intent_path) or ".", "intent_filled.json")
        save_intent(self.intent_data, filled_path)

    def validate_filled_intent(self) -> None:
        filled_path = os.path.join(os.path.dirname(self.intent_path) or ".", "intent_filled.json")
        self.inventory = basic_validation(filled_path)

        for asn, as_obj in self.inventory.ases.items():
            for router_name, router_obj in as_obj.routers.items():
                for if_name, if_obj in router_obj.interfaces.items():
                    if not if_obj.ipv6:
                        raise ValueError(f"Missing IPv6 address: {router_name}:{if_name}")

    def generate_configurations(self) -> None:
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        filled_path = os.path.join(os.path.dirname(self.intent_path) or ".", "intent_filled.json")
        self.inventory = parse_info(filled_path)

        for asn, as_obj in self.inventory.ases.items():
            as_dir = os.path.join(self.output_dir, f"AS{asn}")
            Path(as_dir).mkdir(parents=True, exist_ok=True)

            #igp process
            if as_obj.igp == "RIP":
                igp_cmds = rip_commands(self.inventory, asn)
            elif as_obj.igp == "OSPF":
                igp_cmds = ospf_commands(self.inventory, asn)
            else:
                raise ValueError(f"Unsupported IGP: {as_obj.igp}")

            #bgp
            bgp_bundle = {}
            if self.use_policies:
                bgp_bundle = build_bgp_with_policies(self.inventory, asn, self.intent_data)
            else:
                #iBGP + eBGP blocks
                ibgp = ibgp_commands(self.inventory, asn) if len(as_obj.routers) > 1 else {}
                ebgp = ebgp_commands(self.inventory, asn)
                for r in as_obj.routers.keys():
                    block = []
                    if ibgp.get(r):
                        block = ibgp[r]
                    if ebgp.get(r):
                        block = ebgp[r]
                    bgp_bundle[r] = {"global": [], "bgp": block}
            for router_name in as_obj.routers.keys():
                bundle = bgp_bundle.get(router_name, {"global": [], "bgp": []})
                config = self.build_router_config(
                    router_name=router_name,
                    asn=asn,
                    igp_process_lines=igp_cmds.get(router_name, []),
                    policy_global_lines=bundle["global"],
                    bgp_block_lines=bundle["bgp"],
                )

                config_file = os.path.join(as_dir, f"{router_name}_startup.cfg")
                with open(config_file, "w", encoding="utf-8") as f:
                    f.write(config)

    def build_router_config(
        self,
        router_name: str,
        asn: int,
        igp_process_lines: list[str],
        policy_global_lines: list[str],
        bgp_block_lines: list[str],
    ) -> str:
        lines: list[str] = []

        #base for conf file
        lines += [
            "!",
            f"hostname {router_name}",
            "!",
            "no ip domain lookup",
            "ip cef",
            "ipv6 unicast-routing",
            "ipv6 cef",
            "!",
        ]

        #igp process
        lines += igp_process_lines
        lines.append("!")

        #int
        router_obj = self.inventory.ases[asn].routers[router_name]
        internal = internal_interfaces(self.inventory, asn)
        igp_type = self.inventory.ases[asn].igp

        #loopback first, then others
        sorted_interfaces = sorted(
            router_obj.interfaces.items(),
            key=lambda x: (0 if "Loopback" in x[0] else 1, x[0]),
        )

        for if_name, if_obj in sorted_interfaces:
            if not if_obj.ipv6:
                continue

            lines += [
                f"interface {if_name}",
                " no ip address",
                " ipv6 enable",
                f" ipv6 address {if_obj.ipv6}",
            ]

            #enable igp on internal+loopback
            if if_name in internal.get(router_name, set()):
                if igp_type == "RIP":
                    lines.append(f" ipv6 rip AS{asn} enable")
                elif igp_type == "OSPF":
                    lines.append(f" ipv6 ospf {asn} area 0")

            lines += [" no shutdown", "!"]

        if policy_global_lines:
            lines += policy_global_lines
            lines.append("!")

        #bgp block
        if bgp_block_lines:
            lines += bgp_block_lines
            lines.append("!")

        lines.append("end")
        return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="network conf automation")
    parser.add_argument("intent_file", help="path to the intent json file")
    parser.add_argument("-o", "--output", default="./output", help="output directory for configs")
    parser.add_argument("--no-policies", action="store_true", help="disable bgp policy automation (ie keeping onlybasic bgp)")
    args = parser.parse_args()
    generator = Network(args.intent_file, args.output, use_policies=not args.no_policies)
    generator.run()


if __name__ == "__main__":
    main()
