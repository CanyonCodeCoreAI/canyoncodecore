"""
CanyonOS standard color palette.

The green->white gradient introduced by the `canyonos init` banner, reused
across the CLI so everything shares one look. `GREEN` is the primary brand
color; `WHITE` the secondary; `GRADIENT` the full ramp for multi-line output.
"""

GREEN = "#2BD17E"
WHITE = "#FFFFFF"

# Primary -> secondary ramp (used for the init banner, top to bottom).
GRADIENT = [
    "#2BD17E",
    "#55DA98",
    "#80E3B2",
    "#AAEDCB",
    "#D5F6E5",
    "#FFFFFF",
]
