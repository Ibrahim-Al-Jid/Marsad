"""Read-only snapshot intake through an operator-bound GitHub App installation."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
from pathlib import Path
import re
from threading import BoundedSemaphore
from time import monotonic, time
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey
from pydantic import BaseModel, ConfigDict, Field, field_validator
import sqlalchemy as sa

from . import platform_runtime as store
from .artifact_review import MAX_ARTIFACT, InputError, redact_label
from .identity import MANAGERS, ROLE_ACTIONS, project_access, require_membership, require_project, require_user
from .scan_contracts import DEFAULT_INPUTS, Identifier, ModelConnection
from .platform_db import Base, bind_project, platform_session

router = APIRouter(prefix="/api/platform", tags=["GitHub source"])
API = "https://api.github.com"
API_VERSION = "2026-03-10"
OWNER_REPO = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"
SHA = r"[a-fA-F0-9]{40}"
MAX_INSTALLATION = 2**63 - 1
MAX_KEY_BYTES = 16_384
MAX_ARCHIVE_SECONDS = 20
GITHUB_SOURCES = "project_github_sources"
PROVIDER_SLOTS = BoundedSemaphore(4)
provider_request = ContextVar("basira_github_provider_request", default=False)


class PrivateProviderTraffic(logging.Filter):
    def filter(self, record):
        # HTTPX logs signed request URLs. HTTP Core traces include 302 Location headers.
        return not provider_request.get()


for logger_name in ("httpx", "httpcore.http11", "httpcore.http2"):
    logging.getLogger(logger_name).addFilter(PrivateProviderTraffic())

sources = sa.Table(
    GITHUB_SOURCES, Base.metadata,
    sa.Column("org_id", sa.String(32), primary_key=True),
    sa.Column("project_id", sa.String(32), primary_key=True),
    sa.Column("id", sa.String(32), nullable=False, unique=True),
    sa.Column("installation_id", sa.BigInteger, nullable=False),
    sa.Column("repository_id", sa.BigInteger, nullable=False),
    sa.Column("repository", sa.String(140), nullable=False),
    sa.ForeignKeyConstraint(("org_id", "project_id"), ("projects.org_id", "projects.id"), ondelete="CASCADE"),
)


class LinkRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Identifier
    installation_id: int = Field(gt=0, le=MAX_INSTALLATION, strict=True)
    repository: str = Field(pattern="^" + OWNER_REPO + "$", max_length=140)

    @field_validator("repository")
    @classmethod
    def no_dot_segment(cls, value):
        if value.split("/")[1] in {".", ".."}:
            raise ValueError("Invalid repository name")
        return value


class SnapshotSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern="^" + SHA + "$", max_length=40)
    scope_version: int = Field(ge=0)
    acknowledge_scope: Literal[True]
    model_connection: ModelConnection | None = None


SNAPSHOT_TOO_BIG = "لقطة GitHub أكبر من حد الرفع (٢ ميجابايت). اختار مستودعًا أصغر أو ارفع ZIP لمجلد الكود فقط."


class GitHubFailure(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail


def installations():
    """Bind a GitHub installation to at most one organization on this deployment."""
    try:
        config = json.loads(os.environ.get("BASIRA_GITHUB_INSTALLATIONS", "{}"))
    except ValueError:
        return {}
    if not isinstance(config, dict):
        return {}
    normalized = {}
    for org, ids in config.items():
        if not re.fullmatch("[a-f0-9]{32}", org) or not isinstance(ids, list):
            continue
        normalized[org] = {item for item in ids if type(item) is int and 0 < item <= MAX_INSTALLATION}
    counts = Counter(item for ids in normalized.values() for item in ids)
    return {org: sorted(item for item in ids if counts[item] == 1) for org, ids in normalized.items()}


def app_config():
    app_id = os.environ.get("BASIRA_GITHUB_APP_ID", "")
    key = os.environ.get("BASIRA_GITHUB_APP_KEY_FILE", "")
    return (app_id, Path(key)) if re.fullmatch("[1-9][0-9]{0,19}", app_id) and key and Path(key).is_file() else None


def app_jwt():
    config = app_config()
    if config:
        app_id, path = config
        try:
            with path.open("rb") as stream:
                secret = stream.read(MAX_KEY_BYTES + 1)
            if 0 < len(secret) <= MAX_KEY_BYTES:
                key = RSAKey.import_key(secret)
                current = int(time())
                return jwt.encode({"alg": "RS256"}, {"iat": current - 60, "exp": current + 480, "iss": app_id}, key)
        except (JoseError, ValueError, TypeError, OSError):
            pass
    raise GitHubFailure(503, "اتصال GitHub App محتاج إعداد على الخادم.")


def client():
    return httpx.Client(timeout=httpx.Timeout(10, connect=4), follow_redirects=False, trust_env=False)


def headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION}


def accepted(response, status):
    if response.status_code == status:
        return response
    if response.status_code in {401, 403, 404, 422}:
        raise GitHubFailure(409, "GitHub رفض الوصول للمستودع أو النسخة المطلوبة.")
    raise GitHubFailure(502, "GitHub تعذّر عليه إرجاع اللقطة.")


def provider_json(response, status):
    value = accepted(response, status).json()
    if not isinstance(value, dict):
        raise ValueError("Invalid GitHub response")
    return value


@contextmanager
def installation_token(session, installation_id, **subset):
    response = session.post(f"{API}/app/installations/{installation_id}/access_tokens", headers=headers(app_jwt()),
                            json={"permissions": {"contents": "read"}, **subset})
    value = provider_json(response, 201)
    token = value.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{10,2048}", token):
        raise ValueError("Invalid token format")
    try:
        permissions = value.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("contents") != "read" or any(
            key not in {"contents", "metadata"} or permission != "read" for key, permission in permissions.items()
        ):
            raise GitHubFailure(409, "اتصال GitHub محتاج صلاحية قراءة محتوى المستودع فقط.")
        yield token
    finally:
        try:
            session.delete(f"{API}/installation/token", headers=headers(token))
        except httpx.HTTPError:
            pass


def visible_repository(session, token, expected, repository_id=None):
    response = session.get(f"{API}/installation/repositories", headers=headers(token), params={"per_page": 100})
    value = provider_json(response, 200)
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or len(repositories) > 100:
        raise ValueError("Invalid repository listing")
    for row in repositories:
        if not isinstance(row, dict):
            continue
        name, identity = row.get("full_name"), row.get("id")
        if (isinstance(name, str) and name.casefold() == expected.casefold()
                and type(identity) is int and 0 < identity <= MAX_INSTALLATION
                and (repository_id is None or identity == repository_id)):
            return name, identity
    raise GitHubFailure(409, "المستودع مش ضمن صلاحيات تثبيت GitHub App.")


def verify_repository(installation_id, repository):
    with client() as session, installation_token(session, installation_id, repositories=[repository.split("/")[1]]) as token:
        return visible_repository(session, token, repository)


def archive_url(response, repository, commit):
    location = accepted(response, 302).headers.get("location", "")
    if len(location) > 4096:
        raise ValueError("Invalid archive location")
    url = httpx.URL(location)
    paths = {f"/{repository}/zip/{commit}".casefold(), f"/{repository}/legacy.zip/{commit}".casefold()}
    if (url.scheme != "https" or url.host != "codeload.github.com" or url.port not in {443, None}
            or url.userinfo or url.fragment or url.path.casefold() not in paths):
        raise GitHubFailure(502, "رابط لقطة GitHub غير متوافق.")
    return url


def snapshot(source, commit):
    with client() as session, installation_token(
        session, source["installation_id"], repository_ids=[source["repository_id"]]
    ) as token:
        repo, _ = visible_repository(session, token, source["repository"], source["repository_id"])
        commit_response = session.get(f"{API}/repos/{repo}/commits/{commit}", headers=headers(token))
        if provider_json(commit_response, 200).get("sha") != commit:
            raise GitHubFailure(409, "GitHub لم يؤكد معرّف النسخة المطلوب.")
        return download_archive(session, token, repo, commit)


def download_archive(session, token, repo, commit):
    redirect = session.get(f"{API}/repos/{repo}/zipball/{commit}", headers=headers(token))
    destination = archive_url(redirect, repo, commit)
    # The signed codeload URL carries its own capability. The API token is never forwarded.
    started = monotonic()
    result = bytearray()
    with session.stream("GET", destination, headers={"Accept-Encoding": "identity"}) as response:
        accepted(response, 200)
        length = response.headers.get("content-length")
        if length is not None and int(length) > MAX_ARTIFACT:
            raise GitHubFailure(413, SNAPSHOT_TOO_BIG)
        for block in response.iter_raw():
            if monotonic() - started > MAX_ARCHIVE_SECONDS:
                raise GitHubFailure(504, "انتهت مهلة تنزيل لقطة GitHub.")
            if len(result) + len(block) > MAX_ARTIFACT:
                raise GitHubFailure(413, SNAPSHOT_TOO_BIG)
            result.extend(block)
    raw = bytes(result)
    if not raw.startswith(b"PK\x03\x04"):
        raise GitHubFailure(502, "GitHub لم يرجع ملف ZIP صالح.")
    return raw


def provider_call(operation, *args):
    if not PROVIDER_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "اتصال GitHub مشغول. جرّب مرة تانية.", headers={"Retry-After": "10"})
    trace_scope = provider_request.set(True)
    try:
        return operation(*args)
    except GitHubFailure as exc:
        raise HTTPException(exc.status, exc.detail) from None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
        raise HTTPException(502, "تعذّر الاتصال بـGitHub أو قراءة اللقطة.") from None
    finally:
        provider_request.reset(trace_scope)
        PROVIDER_SLOTS.release()


def available_installations(org_id):
    return installations().get(org_id, []) if app_config() else []


def rows_for(org_id):
    return sa.select(sources).where(sources.c.org_id == org_id)


def project_source(db, ctx):
    return db.execute(rows_for(ctx.org_id).where(sources.c.project_id == ctx.project_id)).mappings().first()


def public_source(row, *, management=False):
    value = {key: row[key] for key in ("id", "project_id", "repository")}
    if management:
        value["installation_id"] = row["installation_id"]
    return value


def require_manager(request, project_id, org_id):
    ctx = require_project(request, project_id, "manage")
    if ctx.org_id != org_id:
        raise HTTPException(404, "المشروع غير متاح.")
    return ctx


def recheck_access(db, ctx, action):
    current = project_access(db, ctx.project_id, ctx.user_id)
    if not current or current.org_id != ctx.org_id or action not in ROLE_ACTIONS[current.role]:
        raise HTTPException(403, "صلاحيتك للمشروع اتغيّرت.")


@router.get("/organizations/{org_id}/github-source")
def organization_sources(org_id: Identifier, request: Request):
    user = require_user(request)
    with platform_session() as db:
        member = require_membership(db, org_id, user.id)
        if member.role not in MANAGERS:
            raise HTTPException(403, "إدارة اتصالات GitHub للمالك أو المدير.")
        bind_project(db, org_id, "")
        rows = db.execute(rows_for(org_id).order_by(sources.c.project_id)).mappings().all()
    allowed = available_installations(org_id)
    return {"available": bool(allowed), "installation_ids": allowed,
            "connections": [public_source(row, management=True) for row in rows]}


@router.put("/organizations/{org_id}/github-source")
def link_source(org_id: Identifier, body: LinkRepository, request: Request):
    ctx = require_manager(request, body.project_id, org_id)
    if body.installation_id not in available_installations(org_id):
        raise HTTPException(409, "تثبيت GitHub App ده مش متاح للمؤسسة.")
    name, repository_id = provider_call(verify_repository, body.installation_id, body.repository)
    with platform_session() as db:
        bind_project(db, ctx.org_id, ctx.project_id)
        store.lock_project(db, ctx.org_id, ctx.project_id)
        recheck_access(db, ctx, "manage")
        if body.installation_id not in available_installations(org_id):
            raise HTTPException(409, "صلاحية تثبيت GitHub App اتغيّرت.")
        current = project_source(db, ctx)
        if current and (current["installation_id"], current["repository_id"], current["repository"]) == (
            body.installation_id, repository_id, name
        ):
            return public_source(current, management=True)
        row = {"org_id": org_id, "project_id": ctx.project_id, "id": uuid4().hex,
               "repository": name, "repository_id": repository_id, "installation_id": body.installation_id}
        condition = (sources.c.org_id == org_id, sources.c.project_id == ctx.project_id)
        if current:
            db.execute(sources.update().where(*condition).values(**row))
        else:
            db.execute(sources.insert().values(**row))
    return public_source(row, management=True)


@router.delete("/organizations/{org_id}/github-source", status_code=204)
def revoke_source(org_id: Identifier, project_id: Identifier, request: Request):
    ctx = require_manager(request, project_id, org_id)
    with platform_session() as db:
        bind_project(db, org_id, project_id)
        store.lock_project(db, org_id, project_id)
        recheck_access(db, ctx, "manage")
        db.execute(sources.delete().where(sources.c.org_id == org_id, sources.c.project_id == project_id))
    return Response(status_code=204)


@router.get("/projects/{project_id}/github-source")
def project_github_source(project_id: Identifier, request: Request):
    ctx = require_project(request, project_id, "scan")
    with platform_session() as db:
        bind_project(db, ctx.org_id, ctx.project_id)
        row = project_source(db, ctx)
    ready = bool(row and row["installation_id"] in available_installations(ctx.org_id))
    return {"available": ready, "connection": public_source(row) if ready else None}


def locked_source(db, ctx, source_id):
    recheck_access(db, ctx, "scan")
    latest = project_source(db, ctx)
    if (not latest or latest["id"] != source_id
            or latest["installation_id"] not in available_installations(ctx.org_id)):
        raise HTTPException(409, "اتصال GitHub اتغيّر. راجع مصدر الفحص مرة تانية.")


@router.post("/projects/{project_id}/github-source/scans", status_code=202)
def import_snapshot(project_id: Identifier, body: SnapshotSubmission, request: Request,
                    key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)]):
    ctx = require_project(request, project_id, "scan")
    commit = body.commit.lower()
    with platform_session() as db:
        bind_project(db, ctx.org_id, ctx.project_id)
        row = project_source(db, ctx)
        if not row or row["installation_id"] not in available_installations(ctx.org_id):
            raise HTTPException(409, "اتصال GitHub مش متاح للمشروع.")
        version, policy = store.project_policy(db, ctx.org_id, project_id)
        if version != body.scope_version or "repository" not in policy.get("allowed_kinds", DEFAULT_INPUTS):
            raise HTTPException(409, "راجع سياسة المشروع قبل استيراد GitHub.")
        locator = {"repository": redact_label(row["repository"]), "commit": commit, "connection_id": row["id"]}
        columns = store.scans.c
        existing = db.execute(sa.select(store.scans).where(
            columns.org_id == ctx.org_id, columns.project_id == project_id, columns.user_id == ctx.user_id,
            columns.idempotency_key == key,
        )).mappings().first()
        if existing:
            metadata = existing["input_meta"]
            if (metadata.get("github") != locator or existing["policy"].get("version") != body.scope_version
                    or metadata.get("model_connection") != body.model_connection):
                raise HTTPException(409, "المفتاح ده مرتبط بطلب مختلف.")
            return store.scan_view(existing)
    raw = provider_call(snapshot, row, commit)
    name = redact_label(row["repository"].split("/")[1]) + ".zip"
    envelope = {"kind": "repository", "name": name, "bytes": len(raw), "github": locator}
    try:
        return store.create_scan(ctx, envelope, raw, key, scope_version=body.scope_version,
                                 model_connection=body.model_connection,
                                 before_create=lambda db: locked_source(db, ctx, row["id"]))
    except store.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except store.QueueLimit:
        raise HTTPException(429, "وصل المشروع لحد الفحوصات المؤقت.", headers={"Retry-After": "60"}) from None
    except InputError as exc:
        raise HTTPException(422, str(exc)) from None
