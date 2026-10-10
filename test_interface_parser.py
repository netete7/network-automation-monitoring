

sample_output = """
Flags: D - DYNAMIC; X - DISABLED; I - INACTIVE, R - RUNNING; S - SLAVE; P - PASSTHROUGH
 0   R   name="ether1" type="ether" mtu=1500
 1   X   name="ether2" type="ether" mtu=1500
 2       name="ether3" type="ether" mtu=1500
 3   R   name="lo" type="loopback" mtu=65536
"""

for line in sample_output.splitlines():
    parts = line.strip().split()

    if not parts or not parts[0].isdigit():
        continue

    name_part = next(
        (part for part in parts if part.startswith('name="')),
        None
    )

    if name_part is None:
        continue

    interface_name = name_part.split('"')[1]
    name_index = parts.index(name_part)
    flags = parts[1:name_index]

    is_running = "R" in flags

    print(
        f"Interface: {interface_name} | "
        f"Status: {'UP' if is_running else 'DOWN'}"
    )