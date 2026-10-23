# Background watcher: waits for the League client, then logs LCU state into a file.
# Reuses the probe logic from test_lcu.py. Safe to kill at any time.
#
# Usage: python lcu_watch_background.py [minutes]

import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_lcu import discover, lcu_get, jload  # noqa: E402


def current_champion(info):
    """(championId, pickIntent, cellId) for the local player, or None."""
    try:
        _, body = lcu_get(info, "/lol-champ-select/v1/session")
        session = jload(body) or {}
    except Exception:
        return None
    cell = session.get("localPlayerCellId")
    for member in session.get("myTeam") or []:
        if member.get("cellId") == cell:
            return (member.get("championId"), member.get("championPickIntent"), cell)
    return None

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "lcu_live_log.txt")
MINUTES = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def log(line, fh):
    stamped = "%s  %s" % (time.strftime("%H:%M:%S"), line)
    fh.write(stamped + "\n")
    fh.flush()
    os.fsync(fh.fileno())


def main():
    deadline = time.time() + MINUTES * 60
    with io.open(OUT, "a", encoding="utf-8") as fh:
        log("watcher started, will poll for %d minutes" % MINUTES, fh)
        info = None
        while time.time() < deadline:
            if info is None:
                info = discover()
                if info:
                    log("CLIENT FOUND via %s -> port %s (%s)"
                        % (info["source"], info["port"], info["path"]), fh)
                    for ep in ["/lol-gameflow/v1/gameflow-phase",
                               "/lol-champ-select/v1/current-champion",
                               "/lol-gameflow/v1/session"]:
                        try:
                            status, body = lcu_get(info, ep)
                            log("  %-46s -> %s %s" % (ep, status, body[:300]), fh)
                        except Exception as exc:
                            log("  %-46s -> ERR %s" % (ep, type(exc).__name__), fh)
                    try:
                        _, body = lcu_get(
                            info, "/lol-game-data/assets/v1/champion-summary.json")
                        champs = json.loads(body)
                        log("  champion-summary.json -> %d entries; sample: %s"
                            % (len(champs), json.dumps(champs[1:3], ensure_ascii=False)[:300]), fh)
                    except Exception as exc:
                        log("  champion-summary.json -> ERR %s" % type(exc).__name__, fh)
                time.sleep(5)
                continue

            try:
                _, body = lcu_get(info, "/lol-gameflow/v1/gameflow-phase")
                phase = body.strip().strip('"')
                champ = current_champion(info)
                log("phase=%-14s champSelect=%s" % (phase, champ), fh)
                if phase in ("ChampSelect", "InProgress"):
                    # live client data api is only up during a game
                    try:
                        import ssl
                        import urllib.request
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                        with urllib.request.urlopen(
                                "https://127.0.0.1:2999/liveclientdata/allgamedata",
                                timeout=3, context=ctx) as resp:
                            data = json.loads(resp.read().decode("utf-8", "replace"))
                        ap = data.get("activePlayer", {})
                        log("  LIVE API OK level=%s champ=%s"
                            % (ap.get("level"), ap.get("riotIdGameName") or ap.get("summonerName")), fh)
                    except Exception as exc:
                        log("  LIVE API (2999) not available: %s" % type(exc).__name__, fh)
                time.sleep(5)
            except Exception as exc:
                log("connection lost (%s); re-discovering" % type(exc).__name__, fh)
                info = None
                time.sleep(5)

        log("watcher finished", fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
