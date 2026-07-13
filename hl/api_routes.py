"""HTTP route dispatch tables for the dashboard API."""

import time

from .api_commands import ALLOWED_COMMANDS, PROCESS_COMMANDS, ep_command, exec_process_command, insert_command
from .api_discovery import (
    ep_discovery,
    ep_pipeline_audit,
    ep_pipeline_summary,
    ep_scan_runs,
    ep_scan_status,
    ep_score_dist,
)
from .api_overview import ep_equity, ep_insights, ep_overview, ep_shadow, ep_strategy_revisions
from .api_params import ep_params, patch_params, reset_params
from .api_positions import ep_position_detail, ep_positions
from .api_wallets import ep_wallet_detail, ep_wallets


TOKEN_TTL_S = 24 * 3600
NO_ROUTE = object()


def _iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


def _position_detail_payload(db, path, qs):
    pid = path.rsplit("/", 1)[1]
    if not pid.isdigit():
        return NO_ROUTE
    return ep_position_detail(db, int(pid))


def _wallet_detail_payload(db, path, qs):
    return ep_wallet_detail(db, path.rsplit("/", 1)[1], qs)


def _command_payload(db, path, qs):
    return ep_command(db, int(path.rsplit("/", 1)[1]))


def _truthy(qs, key):
    return str((qs.get(key, [""]) or [""])[0]).lower() in {"1", "true", "yes", "on"}


GET_ROUTES = {
    "/api/overview": lambda db, qs: ep_overview(db),
    "/api/equity": lambda db, qs: ep_equity(db, qs.get("range", ["all"])[0]),
    "/api/insights": lambda db, qs: ep_insights(db),
    "/api/positions": lambda db, qs: ep_positions(db, qs),
    "/api/wallets": lambda db, qs: ep_wallets(db, qs),
    "/api/discovery": lambda db, qs: ep_discovery(db),
    "/api/scan-runs": lambda db, qs: ep_scan_runs(db, int(qs.get("limit", [20])[0])),
    "/api/params": lambda db, qs: ep_params(db, include_score_dist=_truthy(qs, "includeScoreDist")),
    "/api/scan-status": lambda db, qs: ep_scan_status(db),
    "/api/score-dist": lambda db, qs: ep_score_dist(db),
    "/api/pipeline-audit": lambda db, qs: ep_pipeline_audit(db, qs),
    "/api/pipeline-summary": lambda db, qs: ep_pipeline_summary(db, qs),
    "/api/shadow": lambda db, qs: ep_shadow(db),
    "/api/strategy-revisions": lambda db, qs: ep_strategy_revisions(
        db, int(qs.get("limit", [50])[0])
    ),
}


GET_PREFIX_ROUTES = (
    ("/api/positions/", _position_detail_payload),
    ("/api/wallets/", _wallet_detail_payload),
    ("/api/commands/", _command_payload),
)


def dispatch_get(db, path, qs):
    handler = GET_ROUTES.get(path)
    if handler:
        return True, handler(db, qs)
    for prefix, handler in GET_PREFIX_ROUTES:
        if path.startswith(prefix):
            data = handler(db, path, qs)
            if data is NO_ROUTE:
                return False, None
            return True, data
    return False, None


def _post_login_payload(db_path, auth, path, body, authed):
    token, err = auth.login(body.get("username"), body.get("password"))
    if err:
        code = 429 if err == "rate_limited" else 401
        return code, {"error": err}
    return 200, {"token": token, "expiresAt": _iso_ago(-TOKEN_TTL_S)}


def _post_command_payload(db_path, auth, path, body, authed):
    if not authed:
        return 401, {"error": "unauthorized"}
    ctype = body.get("type")
    if ctype not in ALLOWED_COMMANDS:
        return 400, {"error": "bad_command_type", "detail": ctype}
    try:
        if ctype in PROCESS_COMMANDS:
            cmd_id, status = exec_process_command(db_path, ctype, body.get("payload"))
        else:
            cmd_id, status = insert_command(db_path, ctype, body.get("payload"), body.get("idempotencyKey"))
        return 202, {"commandId": cmd_id, "status": status}
    except Exception as e:  # noqa: BLE001
        return 500, {"error": "server_error", "detail": str(e)}


def _post_params_reset_payload(db_path, auth, path, body, authed):
    if not path.endswith("/reset"):
        return NO_ROUTE
    if not authed:
        return 401, {"error": "unauthorized"}
    cat = path.split("/")[3]
    if cat not in ("follow", "scanner", "all"):
        return 400, {"error": "bad_category"}
    try:
        resp = {"reset": reset_params(db_path, cat)}
        if cat in ("scanner", "all"):
            resp["pendingRescan"] = True
        return 200, resp
    except Exception as e:  # noqa: BLE001
        return 500, {"error": "server_error", "detail": str(e)}


POST_ROUTES = {
    "/api/auth/login": _post_login_payload,
    "/api/commands": _post_command_payload,
}


POST_PREFIX_ROUTES = (
    ("/api/params/", _post_params_reset_payload),
)


def dispatch_post(db_path, auth, path, body, authed):
    handler = POST_ROUTES.get(path)
    if handler:
        code, payload = handler(db_path, auth, path, body, authed)
        return True, code, payload
    for prefix, handler in POST_PREFIX_ROUTES:
        if path.startswith(prefix):
            result = handler(db_path, auth, path, body, authed)
            if result is NO_ROUTE:
                return False, None, None
            code, payload = result
            return True, code, payload
    return False, None, None


def _patch_params_payload(db_path, path, body):
    cat = path.rsplit("/", 1)[1]
    if cat not in ("follow", "scanner"):
        return 400, {"error": "bad_category"}
    try:
        resp = {"updated": patch_params(db_path, cat, body)}
        if cat == "scanner":
            resp["pendingRescan"] = True
        return 200, resp
    except ValueError as e:
        return 422, {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return 500, {"error": "server_error", "detail": str(e)}


PATCH_PREFIX_ROUTES = (
    ("/api/params/", _patch_params_payload),
)


def dispatch_patch(db_path, path, body):
    for prefix, handler in PATCH_PREFIX_ROUTES:
        if path.startswith(prefix):
            code, payload = handler(db_path, path, body)
            return True, code, payload
    return False, None, None
