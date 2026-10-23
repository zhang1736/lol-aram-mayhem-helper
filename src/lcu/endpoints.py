"""LCU 端点常量（Day 1 已在本机实测）。"""

GAMEFLOW_PHASE = "/lol-gameflow/v1/gameflow-phase"
GAMEFLOW_SESSION = "/lol-gameflow/v1/session"

CHAMP_SELECT_SESSION = "/lol-champ-select/v1/session"
CHAMP_SELECT_CURRENT_CHAMPION = "/lol-champ-select/v1/current-champion"

#: 对局内才可用；不在对局时会返回 404 RESOURCE_NOT_FOUND
LIVE_CLIENT_ACTIVE_PLAYER = "/lol-live-client/v1/active-player"

SUMMONER_CURRENT = "/lol-summoner/v1/current-summoner"
CHAMPION_SUMMARY = "/lol-game-data/assets/v1/champion-summary.json"
OWNED_CHAMPIONS = "/lol-champions/v1/owned-champions-minimal"
