from app.modules.subsidy.rules import allocate, build_adjustments
from app.engine import evaluate
from app import store
from copy import deepcopy
import pytest

def claim(ticket, subsidy, **kw):
    return dict({'ticket': ticket, 'version': 1, 'billable_kg': '1', 'subsidy': subsidy,
                 'due': '0.00', 'chain_open': False}, **kw)

def test_pool_fixed_real(sample):
    s = sample['subsidy']['summary']
    assert s['pool'] == '600.00' and s['allocated'] == '559.10' and s['remaining'] == '40.90'
    assert s['pool_balanced']

def test_early_session_allocated_first_real(sample):
    granted = [r['ticket'] for r in sample['subsidy']['ledger'] if r['status'] == '入围']
    assert granted == [f'000{i:02d}' for i in range(1, 13)]

def test_pool_exhausted_pending_real(sample):
    pending = sample['subsidy']['pending']
    assert len(pending) == 8 and all(p['code'] == 'POOL_EXHAUSTED' for p in pending)
    assert pending[0]['ticket'] == '00013' and pending[0]['subsidy'] == '48.93'

def test_correction_occupies_pool_once_real(sample):
    # 迟到更正不重复占池：00001 只按生效版 v2 占 52.25，v1 的 47.50 不进池
    row = next(r for r in sample['subsidy']['ledger'] if r['ticket'] == '00001')
    assert row['subsidy'] == '52.25' and row['version'] == 2
    adj = sample['subsidy']['adjustments'][0]
    assert (adj['old_subsidy'], adj['new_subsidy'], adj['delta']) == ('47.50', '52.25', '4.75')

def test_net_payable_real(sample):
    row = next(r for r in sample['subsidy']['ledger'] if r['ticket'] == '00001')
    assert row['net'] == '169.10'  # 待付 116.85 + 补贴 52.25
    s = sample['subsidy']['summary']
    assert s['net_total'] == '23099.51' and s['due_total'] == '22540.41'

def test_pending_ticket_no_subsidy_no_pool(sample):
    row = next(r for r in sample['subsidy']['ledger'] if r['ticket'] == '00013')
    assert row['subsidy'] == '0.00' and row['status'] == '待补' and row['net'] == row['due']

def test_duplicate_claim_not_double_allocated():
    claims = [claim('00001', '10.00'), claim('00001', '10.00')]
    allocated, pending, remaining = allocate(claims, 100)
    assert len(allocated) == 1 and remaining == 90
    assert pending[0]['code'] == 'DUPLICATE_CLAIM'

def test_chain_open_claim_pending():
    claims = [claim('00001', '10.00', chain_open=True), claim('00002', '10.00')]
    allocated, pending, remaining = allocate(claims, 100)
    assert [c['ticket'] for c in allocated] == ['00002'] and remaining == 90
    assert pending[0]['code'] == 'CHAIN_OPEN'

def test_pool_exhausted_boundary():
    allocated, pending, remaining = allocate([claim('a', '60'), claim('b', '40'), claim('c', '0.01')], 100)
    assert [c['ticket'] for c in allocated] == ['a', 'b'] and remaining == 0
    assert pending[0]['code'] == 'POOL_EXHAUSTED'

def test_chain_open_ticket_pending_real(clean):
    # 更正链未闭合的票整票阻断后列为待补，不静默丢失
    s = deepcopy(clean)
    s['version_conflicts'] = [{'ticket': '00020', 'code': 'STALE_CORRECTION', 'version': 3}]
    s['final'] = [r for r in s['final'] if r['ticket'] != '00020']
    from app.modules.subsidy.rules import apply
    apply(s)
    pending = s['subsidy']['pending']
    hit = next(p for p in pending if p['ticket'] == '00020')
    assert hit['code'] == 'CHAIN_OPEN'
    assert all(r['ticket'] != '00020' or r['status'] == '待补' for r in s['subsidy']['ledger'])

def test_rerun_same_input_no_double_pool():
    a, b = evaluate(), evaluate()
    assert a['manifest']['run_id'] == b['manifest']['run_id']
    assert a['subsidy']['summary'] == b['subsidy']['summary']
    assert a['subsidy']['ledger'] == b['subsidy']['ledger']

def test_pool_override_shrinks_allocation():
    s = evaluate({'overrides': {'subsidy_pool': 100}})
    summary = s['subsidy']['summary']
    assert summary['pool'] == '100.00' and summary['pool_balanced']
    assert len(s['subsidy']['pending']) > 8

def test_adjustments_empty_without_correction(clean):
    s = deepcopy(clean)
    s['final'] = [dict(r, code='ORIGINAL') for r in s['final']]
    assert build_adjustments(s, clean['context']['subsidy_rate']) == []

def test_publish_happy_path(tmp_path, sample):
    path = str(tmp_path / 'p.sqlite')
    store.save(path, sample)
    rid = sample['manifest']['run_id']
    result = store.publish(path, rid, 1)
    assert result['status'] == '已发布' and result['revision'] == 2

def test_publish_interrupt_restores_previous(tmp_path, sample, monkeypatch):
    path = str(tmp_path / 'p.sqlite')
    store.save(path, sample)
    rid = sample['manifest']['run_id']
    def boom(con, run_id, revision):
        raise RuntimeError('发布中断')
    monkeypatch.setattr(store, '_audit_publish', boom)
    with pytest.raises(RuntimeError):
        store.publish(path, rid, 1)
    restored = store.get(path, rid)
    assert restored['status'] == '待复核' and restored['revision'] == 1
    assert restored['subsidy'] == sample['subsidy']  # 旧版结果与证据原样恢复

def test_publish_stale_version(tmp_path, sample):
    from app.errors import BusinessError
    path = str(tmp_path / 'p.sqlite')
    store.save(path, sample)
    rid = sample['manifest']['run_id']
    store.publish(path, rid, 1)
    with pytest.raises(BusinessError) as e:
        store.publish(path, rid, 1)
    assert e.value.status == 409

def test_publish_keeps_evidence(tmp_path, sample):
    path = str(tmp_path / 'p.sqlite')
    store.save(path, sample)
    rid = sample['manifest']['run_id']
    store.publish(path, rid, 1)
    kept = store.get(path, rid)
    assert kept['records'] == sample['records'] and kept['quarantine'] == sample['quarantine']
    assert kept['final'] == sample['final'] and kept['version_audit'] == sample['version_audit']
