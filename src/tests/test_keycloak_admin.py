"""Tests for services.keycloak_admin.list_reviewer_emails.

Best-effort by design: never raises, so a Keycloak misconfiguration or outage
can never block the upload flow that calls it to notify reviewers.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import keycloak_admin


def test_returns_empty_when_not_configured(monkeypatch):
    monkeypatch.setattr(keycloak_admin, "is_configured", lambda: False)
    assert keycloak_admin.list_reviewer_emails() == []


def test_returns_empty_on_keycloak_error(monkeypatch):
    monkeypatch.setattr(keycloak_admin, "is_configured", lambda: True)

    def raise_error(**kwargs):
        raise keycloak_admin.KeycloakAdminError("unreachable")

    monkeypatch.setattr(keycloak_admin, "list_users", raise_error)
    assert keycloak_admin.list_reviewer_emails() == []


def test_filters_by_reviewer_role_and_enabled(monkeypatch):
    monkeypatch.setattr(keycloak_admin, "is_configured", lambda: True)
    monkeypatch.setattr(
        keycloak_admin,
        "list_users",
        lambda **kwargs: [
            {"email": "editor@x.org", "enabled": True, "roles": ["database_editor"]},
            {"email": "admin@x.org", "enabled": True, "roles": ["admin"]},
            {"email": "viewer@x.org", "enabled": True, "roles": ["viewer"]},
            {"email": "disabled_editor@x.org", "enabled": False, "roles": ["database_editor"]},
            {"email": None, "enabled": True, "roles": ["admin"]},
        ],
    )
    assert sorted(keycloak_admin.list_reviewer_emails()) == ["admin@x.org", "editor@x.org"]
