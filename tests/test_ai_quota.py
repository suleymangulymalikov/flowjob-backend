"""Quota tests use only the test launcher's disposable MongoDB and mocked provider."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from beanie import PydanticObjectId
from fastapi import HTTPException
from pymongo.errors import AutoReconnect

from app.config import settings
from app.matches.models import MatchRecord, MatchUsage
from app.services import ai_matching, ai_provider, ai_quota, ai_tailoring
from app.services.ai_cache import coalesce_request
from app.tailor.models import TailorDraft
from tests.test_matches import BASE, JOB, make_cv, signup

DAY = '2031-01-01'
NEXT_DAY = '2031-01-02'


@pytest.fixture(autouse=True)
def pin_day(monkeypatch):
    monkeypatch.setattr(ai_quota, '_utc_day', lambda: DAY)


def quota_document(client, day=DAY):
    async def read():
        return await MatchUsage.get_pymongo_collection().find_one({'day': f'ai-quota:{day}'})

    return client.portal.call(read)


def create_job(client, headers, cv, path, variant=''):
    return client.post(f'{BASE}/{path}', headers=headers, json={
        'cvId': cv, 'jobTitle': 'Backend Engineer', 'jobDescription': JOB + variant,
    })


def test_per_user_workflow_limits_are_separate_and_counts_include_only_new_generations(client, monkeypatch):
    monkeypatch.setattr(settings, 'match_daily_quota', 1)
    monkeypatch.setattr(settings, 'tailor_daily_quota', 2)
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 10)
    owner = signup(client)
    cv = make_cv(client, owner)

    assert create_job(client, owner, cv, 'matches').status_code == 201
    blocked = create_job(client, owner, cv, 'matches', ' another post')
    assert blocked.status_code == 429 and 'Daily AI match limit' in blocked.json()['detail']
    assert create_job(client, owner, cv, 'tailor').status_code == 201
    assert create_job(client, owner, cv, 'tailor', ' second post').status_code == 201
    blocked = create_job(client, owner, cv, 'tailor', ' third post')
    assert blocked.status_code == 429 and 'Daily CV tailoring limit' in blocked.json()['detail']

    other = signup(client, 'other@example.com')
    other_cv = make_cv(client, other)
    assert create_job(client, other, other_cv, 'matches').status_code == 201
    assert create_job(client, other, other_cv, 'tailor').status_code == 201
    quota = quota_document(client)
    assert quota['used'] == 5 and quota['pending'] == {}
    assert sorted(quota['users']['match'].values()) == [1, 1]
    assert sorted(quota['users']['tailor'].values()) == [1, 2]


@pytest.mark.parametrize('first,second', [('matches', 'tailor'), ('tailor', 'matches')])
def test_global_cap_is_shared_across_users_and_workflows_but_does_not_block_cache_hits(
    client, monkeypatch, first, second,
):
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 1)
    owner = signup(client)
    other = signup(client, 'other@example.com')
    cv = make_cv(client, owner)
    other_cv = make_cv(client, other)
    created = create_job(client, owner, cv, first)
    assert created.status_code == 201
    denied = create_job(client, other, other_cv, second)
    assert denied.status_code == 503 and 'at capacity' in denied.json()['detail']

    def no_generation(*args, **kwargs):
        pytest.fail('Cache hits and saved reports/drafts must never reserve or call the provider.')

    monkeypatch.setattr(ai_quota, 'reserve_quota', no_generation)
    monkeypatch.setattr(ai_matching, 'analyze', no_generation)
    monkeypatch.setattr(ai_tailoring, 'analyze', no_generation)
    saved_id = created.json()['id']
    assert create_job(client, owner, cv, first).json()['id'] == saved_id
    assert client.get(f'{BASE}/{first}/{saved_id}', headers=owner).status_code == 200
    quota = quota_document(client)
    assert quota['used'] == 1 and quota['pending'] == {}


@pytest.mark.parametrize('scope,expected_status', [('user', 429), ('global', 503)])
def test_distinct_requests_cannot_oversubscribe_in_flight_quota_and_failure_reopens_slot(
    client, monkeypatch, scope, expected_status,
):
    monkeypatch.setattr(settings, 'match_daily_quota', 1 if scope == 'user' else 10)
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 1 if scope == 'global' else 10)
    headers = signup(client)
    cv = make_cv(client, headers)
    blocked_path = 'matches' if scope == 'user' else 'tailor'
    started, finish = Event(), Event()
    original = ai_matching.analyze

    def fail_first(*args):
        started.set()
        assert finish.wait(10)
        raise ai_provider.AiTimeout()

    monkeypatch.setattr(ai_matching, 'analyze', fail_first)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(create_job, client, headers, cv, 'matches')
        try:
            assert started.wait(5)
            denied = create_job(client, headers, cv, blocked_path, ' distinct job')
            assert denied.status_code == expected_status
            assert quota_document(client)['used'] == 1
        finally:
            finish.set()
        assert running.result(timeout=5).status_code == 504

    assert quota_document(client)['used'] == 0
    monkeypatch.setattr(ai_matching, 'analyze', original)
    assert create_job(client, headers, cv, blocked_path, ' distinct job').status_code == 201
    quota = quota_document(client)
    assert quota['used'] == 1 and quota['pending'] == {}


@pytest.mark.parametrize('path,model', [('matches', MatchRecord), ('tailor', TailorDraft)])
def test_unexpected_persistence_failure_refunds_both_counters(client, monkeypatch, path, model):
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 1)
    headers = signup(client)
    cv = make_cv(client, headers)

    async def fail_insert(*args, **kwargs):
        raise RuntimeError('synthetic persistence failure')

    with monkeypatch.context() as patch:
        patch.setattr(model, 'insert', fail_insert)
        with pytest.raises(RuntimeError, match='synthetic persistence failure'):
            create_job(client, headers, cv, path)
    quota = quota_document(client)
    assert quota['used'] == 0 and quota['pending'] == {}
    assert create_job(client, headers, cv, path).status_code == 201
    assert quota_document(client)['used'] == 1


def test_midnight_refund_uses_original_day_and_only_removes_its_own_reservation(client, monkeypatch):
    user = PydanticObjectId()
    current = [DAY]
    monkeypatch.setattr(ai_quota, '_utc_day', lambda: current[0])

    async def cross_midnight():
        with pytest.raises(RuntimeError, match='after midnight'):
            async with ai_quota.generation_quota(user, 'match'):
                current[0] = NEXT_DAY
                async with ai_quota.generation_quota(user, 'match'):
                    pass
                raise RuntimeError('failed after midnight')

    client.portal.call(cross_midnight)
    before, after = quota_document(client), quota_document(client, NEXT_DAY)
    assert before['used'] == before['users']['match'][str(user)] == 0
    assert after['used'] == after['users']['match'][str(user)] == 1
    assert before['pending'] == after['pending'] == {}


def test_repeated_refunds_and_lost_acknowledgement_cannot_refund_another_reservation(client, monkeypatch):
    user = PydanticObjectId()
    collection_type = type(MatchUsage.get_pymongo_collection())
    real_update = collection_type.update_one
    lost_ack = False

    async def lose_refund_ack(self, query, update, *args, **kwargs):
        nonlocal lost_ack
        result = await real_update(self, query, update, *args, **kwargs)
        if update.get('$inc', {}).get('used') == -1 and not lost_ack:
            lost_ack = True
            raise AutoReconnect('synthetic lost acknowledgement')
        return result

    monkeypatch.setattr(collection_type, 'update_one', lose_refund_ack)

    async def settle():
        successful = await ai_quota.reserve_quota(user, 'tailor')
        refundable = await ai_quota.reserve_quota(user, 'tailor')
        await successful.settle(refund=False)
        await asyncio.gather(*(refundable.settle(refund=True) for _ in range(3)))
        await successful.settle(refund=True)

    client.portal.call(settle)
    quota = quota_document(client)
    assert lost_ack
    assert quota['used'] == quota['users']['tailor'][str(user)] == 1
    assert quota['pending'] == {}


def test_ambiguous_reservation_write_refunds_its_exact_token(client, monkeypatch):
    user = PydanticObjectId()
    collection_type = type(MatchUsage.get_pymongo_collection())
    real_reserve = collection_type.find_one_and_update

    async def lose_reservation_ack(self, *args, **kwargs):
        await real_reserve(self, *args, **kwargs)
        raise AutoReconnect('synthetic lost acknowledgement')

    async def reserve():
        kept = await ai_quota.reserve_quota(user, 'match')
        await kept.settle(refund=False)
        with monkeypatch.context() as patch:
            patch.setattr(collection_type, 'find_one_and_update', lose_reservation_ack)
            with pytest.raises(HTTPException) as error:
                async with ai_quota.generation_quota(user, 'match'):
                    pytest.fail('The provider must not run after ambiguous reservation.')
            assert error.value.status_code == 503

    client.portal.call(reserve)
    quota = quota_document(client)
    assert quota['used'] == quota['users']['match'][str(user)] == 1
    assert quota['pending'] == {}


def test_existing_current_day_usage_is_counted_without_rewriting_legacy_rows(client, monkeypatch):
    user = PydanticObjectId()
    monkeypatch.setattr(settings, 'match_daily_quota', 2)
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 5)

    async def seed_and_reserve():
        collection = MatchUsage.get_pymongo_collection()
        rows = [
            {'user_id': user, 'day': DAY, 'used': 2},
            {'user_id': user, 'day': f'tailor:{DAY}', 'used': 1},
            {'user_id': ai_quota._GLOBAL, 'day': f'ai-global:{DAY}', 'used': 2},
        ]
        ids = (await collection.insert_many(rows)).inserted_ids
        with pytest.raises(HTTPException) as error:
            await ai_quota.reserve_quota(user, 'match')
        assert error.value.status_code == 429
        held = await ai_quota.reserve_quota(user, 'tailor')
        await held.settle(refund=False)
        with pytest.raises(HTTPException) as error:
            await ai_quota.reserve_quota(PydanticObjectId(), 'match')
        assert error.value.status_code == 503
        for row, oid in zip(rows, ids):
            assert await collection.find_one({'_id': oid}) == row

    client.portal.call(seed_and_reserve)
    quota = quota_document(client)
    assert quota['used'] == 5 and quota['users']['tailor'][str(user)] == 2


def test_cancelling_one_waiter_keeps_the_shared_generation_alive(client):
    async def exercise():
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def lookup():
            return None

        async def generate():
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return 'generated'

        identity = ('test-disconnect',)
        first = asyncio.create_task(coalesce_request(identity, lookup, generate))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(coalesce_request(identity, lookup, generate))
        finish.set()
        assert await second == 'generated' and calls == 1

    client.portal.call(exercise)


def test_simultaneous_reservations_enforce_both_limits_on_one_day_document(client, monkeypatch):
    monkeypatch.setattr(settings, 'match_daily_quota', 1)
    monkeypatch.setattr(settings, 'tailor_daily_quota', 1)
    monkeypatch.setattr(settings, 'ai_global_daily_quota', 3)
    users = [PydanticObjectId(), PydanticObjectId()]

    async def compete():
        async def reserve(user, workflow):
            try:
                reservation = await ai_quota.reserve_quota(user, workflow)
            except HTTPException as error:
                assert error.status_code in (429, 503)
                return False
            await reservation.settle(refund=False)
            return True

        return await asyncio.gather(*(
            reserve(user, workflow)
            for user in users
            for workflow in ('match', 'tailor')
            for _ in range(3)
        ))

    assert sum(client.portal.call(compete)) == 3
    quota = quota_document(client)
    assert quota['used'] == 3 and quota['pending'] == {}
    assert all(used <= 1 for users in quota['users'].values() for used in users.values())


def test_explicit_refresh_joining_a_cache_lookup_still_generates_once(client):
    async def exercise():
        looking_up, finish_lookup, fresh_joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def lookup():
            looking_up.set()
            await finish_lookup.wait()
            return 'old cached draft'

        async def generate():
            nonlocal calls
            calls += 1
            return 'new draft'

        async def refresh():
            fresh_joined.set()
            return await coalesce_request(('test-refresh',), lookup, generate, fresh=True)

        ordinary = asyncio.create_task(coalesce_request(('test-refresh',), lookup, generate))
        await looking_up.wait()
        explicit = asyncio.create_task(refresh())
        await fresh_joined.wait()
        finish_lookup.set()
        assert await asyncio.gather(ordinary, explicit) == ['new draft', 'new draft']
        assert calls == 1

    client.portal.call(exercise)
