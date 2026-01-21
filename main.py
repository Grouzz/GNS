import os
import sys
from pathlib import Path

from utils import (
    parse_info,
    basic_validation,
    rip_commands,
    ospf_commands,
    ibgp_commands,
    ebgp_commands,
)
from addressing import(
    load_intent,
    save_intent,
    fill_ipv6_intra_as,
    fill_ipv6_ebgp_links,
    fill_loopbacks,
)

class Network:
    def __init__(self, intent_path: str, output_dir: str = "./output"):
        self.intent_path = intent_path
        self.output_dir = output_dir
        self.intent_data = None
        self.inventory = None

    def run(self):
        try:
            self.load_and_validate()
            print("loading intent file : done")
            self.fill_addresses()
            print("filling IPv6 addresses : done")
            self.validate_filled_intent()
            print("all int have IPs + correct reciprocity : done")
            self.generate_configurations()
            print("configurations generated : done")

        except Exception as e:
            print(f"check error:{e}")
            sys.exit(1)

    def load_and_validate(self):
        self.intent_data = load_intent(self.intent_path)
        self.inventory = basic_validation(self.intent_path)

    def fill_addresses(self):
        self.intent_data = fill_ipv6_intra_as(self.intent_data)
        self.intent_data = fill_ipv6_ebgp_links(self.intent_data)
        self.intent_data = fill_loopbacks(self.intent_data)

        filled_path = self.intent_path.replace(".json", "_filled.json")
        save_intent(self.intent_data, filled_path)

    def validate_filled_intent(self):
        filled_path = self.intent_path.replace(".json", "_filled.json")
        self.inventory = basic_validation(filled_path)

        for asn, as_obj in self.inventory.ases.items():
            for router_name, router_obj in as_obj.routers.items():
                for if_name, if_obj in router_obj.interfaces.items():
                    if not if_obj.ipv6:
                        raise ValueError(f"Missing IPv6 address: {router_name}:{if_name}")

    def generate_configurations(self):
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        filled_path = self.intent_path.replace(".json", "_filled.json")
        self.inventory = parse_info(filled_path)

        for asn, as_obj in self.inventory.ases.items():
            as_dir = os.path.join(self.output_dir, f"AS{asn}")
            Path(as_dir).mkdir(parents=True, exist_ok=True)

            if as_obj.igp == "RIP":
                igp_cmds = rip_commands(self.inventory, asn)
            elif as_obj.igp == "OSPF":
                igp_cmds = ospf_commands(self.inventory, asn)
            else:
                raise ValueError(f"Unsupported IGP: {as_obj.igp}")

            ibgp_cmds = {}
            if len(as_obj.routers) > 1:
                ibgp_cmds = ibgp_commands(self.inventory, asn)

            ebgp_cmds = ebgp_commands(self.inventory, asn)

            for router_name in as_obj.routers.keys():
                config = self.build_router_config(
                    router_name,
                    asn,
                    igp_cmds.get(router_name, []),
                    ibgp_cmds.get(router_name, []),
                    ebgp_cmds.get(router_name, []),
                )

                config_file = os.path.join(as_dir, f"{router_name}_config.txt")
                with open(config_file, "w") as f:
                    f.write(config)

    def build_router_config(self, router_name, asn, igp_cmds, ibgp_cmds, ebgp_cmds):
        lines = []

        lines.append("enable")
        lines.append("configure terminal")
        lines.append(f"hostname {router_name}")
        lines.append("ipv6 unicast-routing")
        lines.append("exit")
        lines.append("")

        router_obj = self.inventory.ases[asn].routers[router_name]
        for if_name, if_obj in router_obj.interfaces.items():
            if if_obj.ipv6:
                lines.append("configure terminal")
                lines.append(f"interface {if_name}")
                lines.append("ipv6 enable")
                lines.append(f"ipv6 address {if_obj.ipv6}")
                lines.append("no shutdown")
                lines.append("exit")
                lines.append("exit")
                lines.append("")

        if igp_cmds:
            lines += igp_cmds
            lines.append("")

        if ibgp_cmds:
            lines += ibgp_cmds
            lines.append("")

        if ebgp_cmds:
            lines += ebgp_cmds
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Network Configuration Automation System")
    parser.add_argument("intent_file", help="Path to the intent JSON file")
    parser.add_argument(
        "-o", "--output",
        default="./output",
        help="Output directory for configuration files (default: ./output)",
    )

    args = parser.parse_args()

    generator = Network(args.intent_file, args.output)
    generator.run()


if __name__ == "__main__":
    main()
