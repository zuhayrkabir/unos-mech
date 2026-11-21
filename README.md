# unos — Mech Royale bot

Python bot for the Builders Buildathon Mech Royale track (mechfight/1).

- `bot.py` — the bot: zone timing, A* navigation, loot (laser first), drone/crate farming for cubes,
  jinking gunfights with lead, laser charge/release, cover ducking during reloads, retreat-to-heal.
- `world.py` — map model built from `/arena` at startup: 2 m line-of-sight grid, 4 m nav grid, time-sliced A*.
- `grid.bin` — the same grid precomputed (`python3 prep.py arena grid.bin`), used only if `/arena` can't be read.
- `mechfight.py` — the kit's wrapper, unchanged.

Code brought in: the kit's `mechfight.py` wrapper (Builders). Standard library only.

Test: `python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" unos-bot`

MIT licensed.
