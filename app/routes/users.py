"""Users, roles and each user's own account page."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from .. import auth, db
from ..web import clean, flash, go, page

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    auth.require_admin(request)
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return page(request, "users.html", users=rows)


@router.post("/users/new")
def user_new(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    role: str = Form("user"),
    password: str = Form(""),
):
    auth.require_admin(request)
    username = clean(username, 80)
    if role not in ("user", "admin") or not username:
        return go("/users")
    if password and len(password) < 12:
        flash(request, "Passwords need at least 12 characters.", "bad")
        return go("/users")
    with db.connection() as conn:
        try:
            conn.execute(
                "INSERT INTO users(username, email, role, password, created_at) VALUES(?,?,?,?,?)",
                (
                    username,
                    clean(email, 200).lower(),
                    role,
                    auth.hash_password(password) if password else None,
                    db.now(),
                ),
            )
        except sqlite3.IntegrityError:
            flash(request, f"{username} already exists.", "bad")
            return go("/users")
    flash(request, f"Added {username}." + ("" if password else " They can only sign in with Microsoft."))
    return go("/users")


def _admin_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]


@router.post("/users/{uid}/role")
def user_role(request: Request, uid: int, role: str = Form(...)):
    me = auth.require_admin(request)
    if role not in ("user", "admin"):
        return go("/users")
    with db.connection() as conn:
        if role == "user" and uid == me["id"]:
            flash(request, "You can't remove your own admin role.", "bad")
        elif role == "user" and _admin_count(conn) <= 1:
            flash(request, "There must be at least one admin.", "bad")
        else:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
            flash(request, "Role updated.")
    return go("/users")


@router.post("/users/{uid}/password")
def user_reset(request: Request, uid: int, password: str = Form(...)):
    auth.require_admin(request)
    if len(password) < 12:
        flash(request, "Passwords need at least 12 characters.", "bad")
        return go("/users")
    with db.connection() as conn:
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (auth.hash_password(password), uid))
    flash(request, "Password set.")
    return go("/users")


@router.post("/users/{uid}/delete")
def user_delete(request: Request, uid: int):
    me = auth.require_admin(request)
    if uid == me["id"]:
        flash(request, "You can't delete your own account.", "bad")
        return go("/users")
    with db.connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    flash(request, "User deleted.")
    return go("/users")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    auth.require_user(request)
    return page(request, "account.html")


@router.post("/account/name")
def account_name(request: Request, name: str = Form("")):
    user = auth.require_user(request)
    name = clean(name, 80)
    with db.connection() as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, user["id"]))
    flash(request, f"Name changed to {name}." if name else "Name cleared; your username is shown instead.")
    return go("/account")


@router.post("/users/{uid}/name")
def user_name(request: Request, uid: int, name: str = Form("")):
    auth.require_admin(request)
    with db.connection() as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (clean(name, 80), uid))
    flash(request, "Name saved.")
    return go("/users")


@router.post("/account/password")
def account_password(request: Request, current: str = Form(""), new: str = Form(...)):
    user = auth.require_user(request)
    with db.connection() as conn:
        row = conn.execute("SELECT password FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row["password"] and not auth.verify_password(current, row["password"]):
            flash(request, "Your current password is wrong.", "bad")
        elif len(new) < 12:
            flash(request, "Passwords need at least 12 characters.", "bad")
        else:
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (auth.hash_password(new), user["id"]))
            flash(request, "Password changed.")
    return go("/account")
