"""One bounded background month at a time, separate from the BI delivery range."""
import asyncio
import hashlib
from datetime import timedelta

from sqlalchemy import delete, select, text, update

from .bi_models import BIDirtyScope, BIUploadDay, PageIdentityState
from .bi_projection import enqueue
from .bi_push_contract import canonical
from .db import utcnow


def refresh_versions(db, master):
    signature = hashlib.sha256(canonical({'policy': master.get('policyVersion'), 'directory': master.get('monthlyDirectoryVersions')})).hexdigest()
    state = db.get(PageIdentityState, 1)
    if state and state.master_hash == signature:
        return
    months = list(db.scalars(select(BIUploadDay.month).distinct()))
    enqueue(db, BIDirtyScope, ['page_month/'+m for m in months if m])
    if state is None:
        state = PageIdentityState(id=1)
        db.add(state)
    state.master_hash = signature


async def prepare_slice(sessions, remote):
    from .bi_push import prepare_identities
    with sessions() as db:
        row = db.execute(select(BIDirtyScope.scope_key, BIDirtyScope.generation).where(BIDirtyScope.scope_key.like('page_month/%'),
                         BIDirtyScope.requested_at <= utcnow()).order_by(BIDirtyScope.requested_at, BIDirtyScope.scope_key.desc()).limit(1)).first()
        if not row:
            return False
        scope, generation = row
        month = scope[len('page_month/'):]
        # Includes personal and unconfirmed libraries for local diagnostics;
        # the existing publisher still selects its own narrower governance scope.
        keys = db.execute(select(BIUploadDay.identity_kind, BIUploadDay.source_key)
                          .where(BIUploadDay.month == month).distinct()).all()
        inputs = {month: {'months': [month], 'groups': {('page', kind, source, month+'-01'): 0 for kind, source in keys}, 'quality': {}}}
    try:
        async with asyncio.timeout(120):
            await prepare_identities(sessions, inputs, remote)
    except Exception:
        with sessions.begin() as db:
            db.execute(update(BIDirtyScope).where(BIDirtyScope.scope_key == scope, BIDirtyScope.generation == generation)
                       .values(requested_at=utcnow()+timedelta(minutes=5)))
        raise
    with sessions.begin() as db:
        db.execute(delete(BIDirtyScope).where(BIDirtyScope.scope_key == scope, BIDirtyScope.generation == generation))
    return True


def run_slice(sessions, settings):
    from .bi_push import RemoteClient
    # This worker is the only page-cache role across publisher replicas.
    with sessions.kw['bind'].connect() as conn:
        mysql = conn.dialect.name == 'mysql'
        got = not mysql or conn.scalar(text("SELECT GET_LOCK('kg_page_identities',0)")) == 1
        conn.commit()
        try:
            if got:
                return asyncio.run(prepare_slice(sessions, RemoteClient(settings)))
        finally:
            if mysql and got:
                conn.execute(text("SELECT RELEASE_LOCK('kg_page_identities')"))
                conn.commit()
