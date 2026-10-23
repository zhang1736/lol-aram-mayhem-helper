# LCU connectivity probe - Day 1 acceptance test.
#
# Discovery no longer depends on WMI/CIM (which this sandbox denies) or on the
# lockfile (which the Tencent client writes as a 0-byte file). It reads the
# LeagueClientUx.exe command line through NtQueryInformationProcess instead.
# See lcu_cred.py for details.
#
# Usage:
#   python test_lcu.py             one-shot report
#   python test_lcu.py --wait 300  poll up to N seconds for the client to appear
#   python test_lcu.py --watch 60  poll every 2s for N seconds, print phase changes

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lcu_cred import find as find_credentials  # noqa: E402

TIMEOUT = 8
LIVE_API = "https://127.0.0.1:2999/liveclientdata/allgamedata"

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(HERE, "lcu_report.txt")
_BUFFER = []


def out(text=""):
    """Print and also collect for the UTF-8 report file (console is GBK here)."""
    _BUFFER.append(text)
    try:
        print(text)
    except Exception:
        print(text.encode("ascii", "replace").decode("ascii"))


def flush_report():
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_BUFFER) + "\n")


try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def lcu_get(info, endpoint, timeout=TIMEOUT):
    """GET an LCU endpoint (self-signed cert, so verification is disabled)."""
    url = "%s://127.0.0.1:%d%s" % (info.get("protocol", "https"), info["port"], endpoint)
    auth = base64.b64encode(("riot:%s" % info["password"]).encode()).decode()
    req = urllib.request.Request(url, headers={
        "Authorization": "Basic %s" % auth,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def try_get(info, endpoint):
    """Return (status, body) and never raise."""
    try:
        return lcu_get(info, endpoint)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:300]
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def jload(body):
    try:
        return json.loads(body)
    except Exception:
        return None


def discover(verbose=False):
    info = find_credentials()
    if info and verbose:
        out("  discovery source : %s" % info["source"])
        out("  target           : %s" % info.get("path", "-"))
    return info


def report():
    out("=" * 72)
    out("LCU probe  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    out("=" * 72)

    info = discover(verbose=True)
    if not info:
        out("")
        out("[RESULT] LCU: FAILED")
        out("  LeagueClientUx.exe 未运行，或无法读取其命令行。")
        flush_report()
        return 1

    out("  region           : %s" % (info.get("region") or "-"))
    out("  locale           : %s" % (info.get("locale") or "-"))
    out("  rso_platform_id  : %s" % (info.get("rso_platform_id") or "-"))

    # ---- handshake ----
    status, body = try_get(info, "/lol-gameflow/v1/gameflow-phase")
    connected = status == 200
    out("")
    out("LCU: %s" % ("CONNECTED" if connected else "FAILED"))
    out("Port: %s" % info["port"])
    out("Token: %s" % ("已获取" if info.get("password") else "未获取"))
    if not connected:
        out("Gameflow: 不可访问 (%s)" % status)
        out("  body: %s" % str(body)[:200])
        flush_report()
        return 1

    phase = str(body).strip().strip('"')
    out("Gameflow: %s" % phase)

    # ---- /lol-gameflow/v1/session ----
    out("")
    out("-" * 72)
    out("GET /lol-gameflow/v1/session")
    out("-" * 72)
    st, bd = try_get(info, "/lol-gameflow/v1/session")
    out("HTTP %s" % st)
    session = jload(bd)
    if isinstance(session, dict):
        gd = session.get("gameData") or {}
        out("  phase            : %s" % session.get("phase"))
        out("  gameClient.phase : %s" % ((session.get("gameClient") or {}).get("phase")))
        out("  queueId          : %s" % gd.get("queueId"))
        out("  queue.type       : %s" % ((gd.get("queue") or {}).get("type")))
        out("  gameMode         : %s" % gd.get("gameMode"))
        out("  map              : %s" % ((gd.get("map") or {}).get("name")))
        out("  gameName         : %s" % gd.get("gameName"))
        out("  isGameFinished   : %s" % gd.get("isGameFinished"))
        out("  raw keys         : %s" % sorted(session.keys()))
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "lcu_gameflow_session.json"), "w", encoding="utf-8") as fh:
            json.dump(session, fh, ensure_ascii=False, indent=1)
        out("  (完整 JSON 已写入 research/lcu_gameflow_session.json)")
    else:
        out("  body: %s" % str(bd)[:300])

    # ---- /lol-champ-select/v1/session ----
    out("")
    out("-" * 72)
    out("GET /lol-champ-select/v1/session")
    out("-" * 72)
    st, bd = try_get(info, "/lol-champ-select/v1/session")
    cs_status = st
    out("HTTP %s" % st)
    champ = None
    if st == 200:
        cs = jload(bd) or {}
        local_cell = cs.get("localPlayerCellId")
        out("  localPlayerCellId: %s" % local_cell)
        team = cs.get("myTeam") or []
        out("  myTeam (%d):" % len(team))
        for m in team:
            mark = "  <== 本地玩家" if m.get("cellId") == local_cell else ""
            out("    cellId=%-3s championId=%-5s pickIntent=%-5s assignedPosition=%-6s %s%s"
                  % (m.get("cellId"), m.get("championId"), m.get("championPickIntent"),
                     m.get("assignedPosition"), m.get("summonerId"), mark))
            if m.get("cellId") == local_cell:
                champ = m.get("championId") or m.get("championPickIntent")
        out("  timer.phase      : %s" % ((cs.get("timer") or {}).get("phase")))
        out("ChampSelect: 可访问")
        try:
            out_path = os.path.join(HERE, "lcu_champ_select.json")
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(cs, fh, ensure_ascii=False, indent=1)
            out("  (完整 JSON 已写入 research/lcu_champ_select.json)")
        except OSError:
            pass
    elif st == 404:
        out("ChampSelect: 当前不在选人（404，属正常状态）")
    else:
        out("ChampSelect: 不可访问 (%s) %s" % (st, str(bd)[:160]))

    # ---- LCU: active player ----
    out("")
    out("-" * 72)
    out("GET /lol-live-client/v1/active-player")
    out("-" * 72)
    st, bd = try_get(info, "/lol-live-client/v1/active-player")
    ap_status = st
    out("HTTP %s" % st)
    ap_lcu = jload(bd)
    if st == 200 and isinstance(ap_lcu, dict):
        out("  %s" % json.dumps(ap_lcu, ensure_ascii=False)[:400])
        out("Active Player: 可访问")
    else:
        ap_lcu = None
        out("  body: %s" % str(bd)[:200])
        out("Active Player: 不可访问（通常只在游戏内可用）")

    # ---- Live Client Data API (port 2999) ----
    out("")
    out("-" * 72)
    out("GET https://127.0.0.1:2999/liveclientdata/allgamedata")
    out("-" * 72)
    level = None
    try:
        with urllib.request.urlopen(LIVE_API, timeout=4, context=_ctx()) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        ap = data.get("activePlayer") or {}
        level = ap.get("level")
        out("HTTP 200")
        out("  activePlayer.level        : %s" % level)
        out("  activePlayer.summonerName : %s"
              % (ap.get("riotIdGameName") or ap.get("summonerName")))
        out("  activePlayer.championName : %s" % ap.get("championName") or "")
        names = [p.get("championName") for p in (data.get("allPlayers") or [])]
        out("  allPlayers (%d)           : %s" % (len(names), names))
        out("  gameData.gameTime         : %s" % (data.get("gameData") or {}).get("gameTime"))
        out("Level: 可用")
    except Exception as exc:
        out("不可访问: %s" % type(exc).__name__)
        out("Level: 不可用（Live Client API 仅在对局进行中监听 2999）")

    # ---- champion name ----
    out("")
    out("-" * 72)
    out("champion-summary.json (id -> 名称/alias)")
    out("-" * 72)
    st, bd = try_get(info, "/lol-game-data/assets/v1/champion-summary.json")
    champ_name = None
    if st == 200:
        champs = jload(bd) or []
        by_id = {c.get("id"): c for c in champs if isinstance(c, dict)}
        out("HTTP 200  条目数: %d" % len(champs))
        if champ:
            c = by_id.get(champ) or {}
            champ_name = c.get("name")
            out("  当前英雄 id=%s name=%s alias=%s" % (champ, c.get("name"), c.get("alias")))
    else:
        out("HTTP %s %s" % (st, str(bd)[:160]))

    # ---- summary ----
    out("")
    out("=" * 72)
    out("SUMMARY")
    out("=" * 72)
    out("LCU:            CONNECTED")
    out("Port:           %s" % info["port"])
    out("Token:          已获取")
    out("Gameflow:       %s" % phase)
    out("ChampSelect:    %s" % ("可访问" if cs_status == 200 else "当前不在选人"))
    out("Active Player:  %s" % ("可访问" if ap_lcu else "不可访问"))
    out("Level:          %s" % ("可用" if level is not None else "不可用"))
    out("当前英雄:        %s" % (champ_name or champ or "当前状态无法获取"))
    flush_report()
    return 0


def wait_for_client(seconds):
    deadline = time.time() + seconds
    out("waiting up to %d seconds for the client ..." % seconds)
    while time.time() < deadline:
        info = discover()
        if info:
            st, _ = try_get(info, "/lol-gameflow/v1/gameflow-phase")
            if st == 200:
                out("client is ready.")
                return report()
        time.sleep(3)
    out("timeout: no usable LCU found.")
    return 1


def watch(seconds):
    info = discover()
    if not info:
        out("LCU: FAILED - cannot enter watch mode.")
        return 1
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        st, body = try_get(info, "/lol-gameflow/v1/gameflow-phase")
        phase = str(body).strip().strip('"') if st == 200 else "HTTP %s" % st
        if phase != last:
            out("  %s  phase=%s" % (time.strftime("%H:%M:%S"), phase))
            last = phase
        time.sleep(2)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0)
    ap.add_argument("--watch", type=int, default=0)
    args = ap.parse_args()
    if args.wait:
        return wait_for_client(args.wait)
    if args.watch:
        return watch(args.watch)
    return report()


if __name__ == "__main__":
    sys.exit(main())
