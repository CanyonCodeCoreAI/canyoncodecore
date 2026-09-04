Lightweight CLI for CanyonOS

Serves as a thin API layer, connecting to the global controller container.

## Serve

`canyonos serve -c config/global_controller.yaml` starts the local CanyonOS dashboard. It reads
`database.url` from the config and writes only `CANYONOS_`-prefixed settings to the current `.env`,
leaving other lines unchanged.


### To Republish to PyPi

```Terminal
cd cli
# Go into, pyproject.toml, and increment version number 
rm -rf dist/ # Removes the old distro, causes conflicts

uv build
uv publish # Needs PyPi Auth Token, ask Saaketh
```