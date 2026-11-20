"""接收端业务编排：订阅调整、团队清单、引用创建与状态、覆盖证明。

交付的四个动作：
- 初次采集 / 日常接续：harvest(mode='auto'|'full'|'incremental')
- 订阅调整：subscribe / unsubscribe（取消只停用，绝不清空共享数据）
- 引用状态查询：citation_status / citation_content
- 覆盖证明：coverage（清楚区分完整覆盖与仍待核实的目录）
"""
import json

from harvest.harvester import Harvester
from harvest.oai import SourceClient
from harvest.store import Store


class Receiver:
    def __init__(self, base_url, db_path):
        self.store = Store(db_path)
        self.client = SourceClient(base_url)
        self.harvester = Harvester(self.store, self.client)

    def close(self):
        self.store.close()

    # ---- 订阅调整 ----
    def subscribe(self, team, repo, set_spec):
        self.store.subscribe(team, repo, set_spec)

    def unsubscribe(self, team, repo, set_spec):
        # 只停用订阅关系；其他团队的有效订阅继续使用共享成果与原件。
        self.store.unsubscribe(team, repo, set_spec)

    def load_config(self, path):
        with open(path, encoding='utf-8') as fh:
            config = json.load(fh)
        for sub in config['subscriptions']:
            for set_spec in sub['sets']:
                self.subscribe(sub['team'], sub['repository'], set_spec)

    # ---- 初次采集与日常接续 ----
    def harvest(self, mode='auto', page_hook=None):
        """按有效订阅逐 (仓储, 集合) 采集，然后独立同步详情、原件与标识关联。"""
        runs = [self.harvester.harvest_set(repo, set_spec, mode=mode, page_hook=page_hook)
                for repo, set_spec in self.store.active_pairs()]
        self.harvester.sync_details()
        self.harvester.sync_fulltext()
        events = self.harvester.resolve_identity(cause='harvest:' + mode)
        return {'runs': runs, 'identity_events': events}

    # ---- 团队订阅范围清单 ----
    def team_scope(self, team):
        subs = self.store.subscriptions(team=team)
        scope = {}
        for state in self.store.all_state():
            via = [s['set_spec'] for s in subs
                   if s['repo'] == state['repo'] and s['set_spec'] in state['sets']]
            if via:
                scope[state['identifier']] = via
        return scope

    def list_team(self, team):
        scope = self.team_scope(team)
        membership = self.store.membership()
        items = []
        for ident, via in sorted(scope.items()):
            state = self.store.state(ident)
            detail = self.store.detail_state(ident)
            fulltext = self.store.fulltext(ident)
            items.append({
                'identifier': ident,
                'repo': state['repo'],
                'title': state['title'],
                'sets': state['sets'],
                'subscribed_via': via,
                'work_key': membership.get(ident),
                'deleted': bool(state['deleted']),
                'datestamp': state['datestamp'],
                'coverage': 'verified' if self.store.is_verified(
                    state['repo'], state['sets'], state['datestamp']) else 'pending',
                'detail': None if not detail else {
                    'revision': detail['revision'],
                    'metadata_public': bool(detail['metadata_public']),
                    'fulltext_open': bool(detail['fulltext_open']),
                    'fetch_status': detail['fetch_status'],
                    'unverified_since': detail['unverified_since'],
                },
                'fulltext': None if not fulltext else {
                    'status': fulltext['status'],
                    'open_at': fulltext['open_at'],
                    'content_hash': fulltext['content_hash'],
                },
            })
        return items

    # ---- 引用 ----
    def cite(self, team, identifier):
        scope = self.team_scope(team)
        if identifier not in scope:
            raise ValueError(f'{identifier} 不在团队 {team} 的订阅范围内')
        state = self.store.state(identifier)
        if state['deleted']:
            raise ValueError(f'{identifier} 已被来源显式删除，不能新建引用')
        detail = self.store.detail_state(identifier)
        if detail and detail['fetch_status'] == 'ok' and not detail['metadata_public']:
            raise ValueError(f'{identifier} 的公开依据已被来源撤回，不能新建引用')
        work = self.harvester.current_work(identifier) or identifier
        return self.store.add_citation(
            team, identifier, work, state['revision_id'],
            detail['revision'] if detail else None)

    def citation_status(self, citation_id):
        """引用状态：区分撤回、关联变化、详情未核实、退出订阅范围、全文限期。"""
        citation = self.store.citation(citation_id)
        if not citation:
            raise ValueError(f'引用 {citation_id} 不存在')
        ident = citation['identifier']
        state = self.store.state(ident)
        detail = self.store.detail_state(ident)
        fulltext = self.store.fulltext(ident)
        current_work = self.harvester.current_work(ident)
        status, reason, evidence, notes = 'valid', None, {}, []

        if state['deleted']:
            # 显式删除标记：公开依据撤回，当前引用停止提供内容。
            status, reason = 'invalid', 'withdrawn_deleted_marker'
            obs = self.store.first_deleted_observation(ident) or {}
            evidence = {'observation_id': obs.get('id'), 'run_id': obs.get('run_id'),
                        'datestamp': obs.get('datestamp'), 'invalid_since': obs.get('recorded_at')}
        elif detail and detail['fetch_status'] == 'ok' and not detail['metadata_public']:
            # 详情服务声明元数据不再公开：同样是公开依据撤回，但事实链不同。
            status, reason = 'invalid', 'public_basis_withdrawn'
            version = self.store.first_nonpublic_detail(ident) or {}
            evidence = {'detail_version_id': version.get('id'),
                        'invalid_since': version.get('fetched_at')}

        if current_work != citation['work_key']:
            notes.append({
                'type': 'association_changed',
                'from_work': citation['work_key'], 'to_work': current_work,
                'events': self.store.identity_events_for(ident),
                'explain': '来源更正改变了标识关系；原观察记录保留，关联按来源最新断言解释',
            })
        if detail and detail['fetch_status'] != 'ok':
            notes.append({'type': 'detail_unverified', 'since': detail['unverified_since'],
                          'last_http': detail['last_http'],
                          'explain': '详情暂时无法访问，按最后已知版本解释，不等于撤回'})
        if ident not in self.team_scope(citation['team']):
            notes.append({'type': 'out_of_scope',
                          'explain': '成果已退出该团队当前订阅范围（集合退出或取消订阅），'
                                     '不等于成果被撤回'})
        if fulltext and fulltext['status'] == 'embargoed':
            notes.append({'type': 'fulltext_embargoed', 'open_at': fulltext['open_at'],
                          'explain': '题录可引用，全文仍在限期内，开放时刻以来源声明为准'})

        return {
            'citation_id': citation_id, 'team': citation['team'], 'identifier': ident,
            'title': state['title'], 'status': status, 'reason': reason,
            'invalid_since': evidence.get('invalid_since'), 'evidence': evidence,
            'work_key_cited': citation['work_key'], 'work_key_current': current_work,
            'notes': notes,
            'fulltext': None if not fulltext else {
                'status': fulltext['status'], 'open_at': fulltext['open_at'],
                'content_hash': fulltext['content_hash']},
        }

    def citation_content(self, citation_id):
        """提供引用内容；公开依据撤回后停止提供，并说明原来的引用为什么失效。"""
        status = self.citation_status(citation_id)
        if status['status'] != 'valid':
            return {'served': False, 'reason': status['reason'],
                    'invalid_since': status['invalid_since'],
                    'explain': '公开依据已被来源撤回，当前引用停止提供相应内容',
                    'evidence': status['evidence']}
        fulltext = self.store.fulltext(status['identifier'])
        if not fulltext or fulltext['status'] != 'retrieved':
            return {'served': False,
                    'reason': 'fulltext_' + (fulltext['status'] if fulltext else 'unknown'),
                    'open_at': fulltext['open_at'] if fulltext else None,
                    'explain': '题录可引用不代表全文可下载'}
        return {'served': True, 'content_hash': fulltext['content_hash'],
                'bytes': len(fulltext['content'] or b'')}

    # ---- 覆盖证明 ----
    def coverage(self):
        pairs = sorted(set(self.store.active_pairs()) | set(self.store.coverage_pairs())
                       | set(self.store.all_run_pairs()))
        report = []
        for repo, set_spec in pairs:
            windows = self.store.coverage(repo, set_spec)
            runs = self.store.runs_for(repo, set_spec)
            report.append({
                'repo': repo, 'set': set_spec,
                'status': 'complete' if windows else 'pending',
                'windows': [{'from': w['from_day'], 'until': w['until_day'], 'run': w['run_id']}
                            for w in windows],
                'runs': [{'id': r['id'], 'mode': r['mode'], 'status': r['status'],
                          'pages': r['pages']} for r in runs],
            })
        pending_records = [
            s['identifier'] for s in self.store.all_state()
            if not self.store.is_verified(s['repo'], s['sets'], s['datestamp'])]
        return {'pairs': report, 'pending_records': sorted(pending_records)}

    # ---- 原件保留 ----
    def prune(self, apply=False):
        """无任何有效订阅覆盖的原件才可清理；共享原件必须保留。"""
        pairs = self.store.active_pairs()
        candidates = []
        for row in self.store.fulltext_all():
            if row['content'] is None:
                continue
            state = self.store.state(row['identifier'])
            covered = any(repo == state['repo'] and set_spec in state['sets']
                          for repo, set_spec in pairs)
            if not covered:
                candidates.append(row['identifier'])
                if apply:
                    self.store.clear_fulltext_content(row['identifier'])
        return {'candidates': sorted(candidates), 'applied': bool(apply)}
