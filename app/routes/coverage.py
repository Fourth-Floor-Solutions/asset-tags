"""Tag coverage: which of each client's Hudu devices have a tag (issue #33). Only while Hudu is set up."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import auth, coverage, csvout, db, hudu, settings
from ..web import flash, go, page

router = APIRouter()


def _client(conn, client_id: int):
    return conn.execute("SELECT * FROM clients WHERE id = ? AND hudu_id IS NOT NULL", (client_id,)).fetchone()


@router.get("/coverage", response_class=HTMLResponse)
def coverage_page(request: Request):
    auth.require_user(request)
    hudu.require()
    exclude = coverage.excluded_layouts()
    with db.connection() as conn:
        rows = [coverage.for_client(conn, c, exclude) for c in coverage.hudu_clients(conn)]
        layouts = coverage.layouts_seen(conn)
    checked = [r for r in rows if r["checked"]]
    totals = {
        "assets": sum(r["assets"] for r in checked),
        "tagged": sum(r["tagged"] for r in checked),
        "unlinked": sum(len(r["unlinked"]) for r in rows),
    }
    totals["pct"] = round(100 * totals["tagged"] / totals["assets"]) if totals["assets"] else None
    return page(
        request,
        "coverage.html",
        rows=rows,
        totals=totals,
        layouts=layouts,
        exclude=exclude,
        refreshing=coverage.refreshing(),
    )


@router.post("/coverage/refresh")
def coverage_refresh_all(request: Request):
    auth.require_user(request)
    hudu.require()
    if coverage.refresh_all_in_background():
        flash(request, "Fetching every client's devices from Hudu. This takes a minute or so; reload to see progress.")
    else:
        flash(request, "A refresh is already running.", "warn")
    return go("/coverage")


@router.post("/coverage/layouts")
async def coverage_layouts(request: Request):
    user = auth.require_admin(request)
    hudu.require()
    form = await request.form()
    counted = {int(v) for v in form.getlist("count") if isinstance(v, str) and v.isdigit()}
    shown = {int(v) for v in form.getlist("shown") if isinstance(v, str) and v.isdigit()}
    # Layouts not on the form (not seen yet) keep their current setting.
    exclude = (coverage.excluded_layouts() - shown) | (shown - counted)
    value = ",".join(str(x) for x in sorted(exclude))
    if value != (settings.get("hudu_coverage_exclude") or ""):
        settings.save({"hudu_coverage_exclude": value})
        settings.audit(auth.label(user), "hudu_coverage_exclude", value or "(none)")
    flash(request, "Saved which asset types count towards coverage.")
    return go("/coverage")


@router.get("/coverage/{client_id}", response_class=HTMLResponse)
def coverage_client(request: Request, client_id: int):
    auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        c = _client(conn, client_id)
        if not c:
            return page(
                request,
                "message.html",
                status_code=404,
                heading="Not found",
                body="No Hudu-linked client with that id.",
            )
        cov = coverage.for_client(conn, c)
    return page(request, "coverage_client.html", cov=cov, c=c)


@router.post("/coverage/{client_id}/refresh")
def coverage_refresh_one(request: Request, client_id: int):
    auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        c = _client(conn, client_id)
        if not c:
            return go("/coverage")
        err = coverage.refresh(conn, c)
    flash(request, f"Hudu didn't answer: {err}" if err else "Refreshed from Hudu.", "bad" if err else "ok")
    return go(f"/coverage/{client_id}")


@router.get("/coverage/{client_id}/untagged.csv")
def coverage_csv(request: Request, client_id: int):
    auth.require_user(request)
    hudu.require()
    with db.connection() as conn:
        c = _client(conn, client_id)
        if not c:
            return go("/coverage")
        cov = coverage.for_client(conn, c)
    return csvout.response(
        ["Device", "Type", "Model", "Serial", "Hudu link"],
        ([a["name"], a["type"], a["model"], a["serial"], a["url"]] for a in cov["untagged"]),
        f"untagged-{csvout.safe_name(c['name'])}.csv",
    )
