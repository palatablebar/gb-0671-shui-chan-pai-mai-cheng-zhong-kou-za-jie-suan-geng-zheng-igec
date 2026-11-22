from decimal import Decimal
from app.coerce import money

PENDING_REASONS = {
    'POOL_EXHAUSTED': '池额超限，余额不足',
    'DUPLICATE_CLAIM': '同票记录再次到达，未重复占池',
    'CHAIN_OPEN': '更正链未闭合',
}

def allocate(claims, pool):
    """按申报到达顺序分配当日补贴池。同票只占一次池额；
    池额超限、同票再次到达、更正链未闭合的申报列为待补，不占池。"""
    remaining = Decimal(str(pool))
    allocated, pending, seen = [], [], set()
    for claim in claims:
        ticket = claim['ticket']
        if ticket in seen:
            pending.append({**claim, 'code': 'DUPLICATE_CLAIM'})
            continue
        seen.add(ticket)
        if claim.get('chain_open'):
            pending.append({**claim, 'code': 'CHAIN_OPEN'})
            continue
        if Decimal(claim['subsidy']) > remaining:
            pending.append({**claim, 'code': 'POOL_EXHAUSTED'})
            continue
        remaining -= Decimal(claim['subsidy'])
        allocated.append(claim)
    return allocated, pending, remaining

def subsidy_of(row, rate):
    return money(Decimal(row['billable_kg']) * rate)

def build_claims(state, rate):
    """生效票按票号排序即早场先核先分；更正链未闭合的票被更正裁决整票阻断，
    在此补一条未闭合申报，使其进入待补而不是静默丢失。"""
    open_tickets = {c['ticket'] for c in state['version_conflicts']}
    claims = []
    for row in state['final']:
        claims.append({'ticket': row['ticket'], 'version': row['version'],
                       'billable_kg': row['billable_kg'], 'subsidy': subsidy_of(row, rate),
                       'due': row['due'], 'chain_open': row['ticket'] in open_tickets})
    for ticket in sorted(open_tickets):
        versions = [s for s in state['settlements'] if s['ticket'] == ticket]
        latest = max(versions, key=lambda s: s['version']) if versions else None
        claims.append({'ticket': ticket,
                       'version': latest['version'] if latest else '',
                       'billable_kg': latest['billable_kg'] if latest else '',
                       'subsidy': subsidy_of(latest, rate) if latest else '0.00',
                       'due': '', 'chain_open': True})
    return sorted(claims, key=lambda c: c['ticket'])

def build_adjustments(state, rate):
    """迟到更正只调整、不重复占池：同票按最终生效版占一次池额，
    被替代版与生效版的补贴差额在此列示。"""
    adjustments = []
    for row in state['final']:
        if row.get('code') != 'CORRECTED':
            continue
        versions = sorted((s for s in state['settlements'] if s['ticket'] == row['ticket']),
                          key=lambda s: s['version'])
        if len(versions) < 2:
            continue
        previous = versions[-2]
        old, new = subsidy_of(previous, rate), subsidy_of(row, rate)
        adjustments.append({'ticket': row['ticket'], 'from_version': previous['version'],
                            'to_version': row['version'], 'old_subsidy': old,
                            'new_subsidy': new, 'delta': money(Decimal(new) - Decimal(old))})
    return adjustments

def ledger_row(claim, status, code, subsidy):
    return {'ticket': claim['ticket'], 'version': claim['version'],
            'billable_kg': claim['billable_kg'], 'subsidy': subsidy, 'due': claim['due'],
            'net': money(Decimal(claim['due'] or '0') + Decimal(subsidy)),
            'status': status, 'code': code}

def apply(state):
    context = state['context']
    rate = Decimal(str(context.get('subsidy_rate', 0.5)))
    pool = money(Decimal(str(context.get('subsidy_pool', 600))))
    claims = build_claims(state, rate)
    allocated, pending, remaining = allocate(claims, pool)
    pending_view = [{'ticket': p['ticket'], 'version': p['version'], 'subsidy': p['subsidy'],
                     'code': p['code'], 'reason': PENDING_REASONS[p['code']]} for p in pending]
    ledger = [ledger_row(c, '入围', '', c['subsidy']) for c in allocated]
    ledger += [ledger_row(p, '待补', p['code'], '0.00') for p in pending]
    ledger.sort(key=lambda r: r['ticket'])
    adjustments = build_adjustments(state, rate)
    allocated_total = sum((Decimal(c['subsidy']) for c in allocated), Decimal(0))
    due_total = sum((Decimal(r['due']) for r in state['final']), Decimal(0))
    summary = {'pool': pool, 'allocated': money(allocated_total), 'remaining': money(remaining),
               'pool_balanced': Decimal(pool) == allocated_total + remaining,
               'pending_subsidy': money(sum((Decimal(p['subsidy']) for p in pending), Decimal(0))),
               'due_total': money(due_total), 'net_total': money(due_total + allocated_total)}
    state['subsidy'] = {'ledger': ledger, 'pending': pending_view,
                        'adjustments': adjustments, 'summary': summary}
    state['views']['subsidy'] = {'title': '冷藏补贴', 'rows': ledger,
        'metrics': {'池额_CNY': pool, '余额_CNY': summary['remaining'],
                    '入围票': len(allocated), '待补票': len(pending)}}
