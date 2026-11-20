"""采集调度：全量/增量交替、续传凭据失效处理、详情与原件独立跟踪、标识关联。

关键规则（对应 docs/source-contract.md）：
- 非空 resumptionToken 才表示还有下一页；中间空页不是末尾；
- 凭据失效（badResumptionToken）时本次运行中止：已观察页保留为证据，
  覆盖窗口不推进，其他订阅已发布的数据不受任何影响，来源允许重新开始；
- 增量窗口按日闭区间重复边界日，重复内容靠哈希去重；
- 目录观察、详情版本、原件领取是三条独立事实链，分别记录。
"""
import json

from harvest.store import content_hash


class Harvester:
    def __init__(self, store, client):
        self.store = store
        self.client = client

    # ---- 目录采集 ----
    def harvest_set(self, repo, set_spec, mode='auto', page_hook=None):
        """采集一个 (仓储, 集合)。page_hook 仅供演练在翻页间隙注入来源事件。"""
        upto = self.store.covered_upto(repo, set_spec)
        if mode == 'auto':
            mode = 'incremental' if upto else 'full'
        if mode == 'incremental' and not upto:
            mode = 'full'
        # 增量起点取已覆盖截止日（含当日）：天粒度边界必然重复，由内容哈希去重。
        from_day = None if mode == 'full' else upto
        run_id = self.store.start_run(repo, set_spec, mode, from_day)
        token, page, until_day = None, 0, None
        while True:
            resp = self.client.list_records(repo, set_spec, from_day=from_day, token=token)
            if page == 0:
                # 以来源 responseDate 作为本次窗口右端，不猜测本地时间。
                until_day = (resp.get('response_date') or '')[:10] or None
            if resp['error']:
                status = 'aborted_' + str(resp['error'])
                self.store.finish_run(run_id, status, until_day=until_day, pages=page,
                                      note=resp.get('message'))
                return {'run': run_id, 'repo': repo, 'set': set_spec, 'mode': mode,
                        'status': status, 'pages': page, 'error': resp['error']}
            for rec in resp['records']:
                self._observe(run_id, page, repo, rec)
            page += 1
            if page_hook:
                page_hook({'repo': repo, 'set': set_spec, 'run': run_id, 'page': page})
            if not resp['token']:
                break
            token = resp['token']
        self.store.finish_run(run_id, 'complete', until_day=until_day, pages=page)
        cover_from = from_day
        if cover_from is None:
            cover_from = self.client.identify(repo).get('earliestDatestamp') or '0000-01-01'
        if until_day:
            self.store.add_coverage(repo, set_spec, cover_from, until_day, run_id)
        return {'run': run_id, 'repo': repo, 'set': set_spec, 'mode': mode,
                'status': 'complete', 'from': cover_from, 'until': until_day, 'pages': page}

    def _observe(self, run_id, page, repo, rec):
        self.store.add_observation(run_id, page, rec)
        digest = content_hash({
            'deleted': rec['deleted'], 'sets': sorted(rec['sets']),
            'datestamp': rec['datestamp'], 'title': rec['title'], 'relation': rec['relation'],
        })
        revision_id, _ = self.store.ensure_revision(run_id, repo, rec, digest)
        self.store.upsert_state(repo, rec, revision_id, run_id)

    # ---- 详情版本（独立事实链）----
    def sync_details(self, identifiers=None):
        identifiers = identifiers or [s['identifier'] for s in self.store.all_state()]
        for ident in identifiers:
            status, payload = self.client.get_detail(ident)
            if status == 200 and payload is not None:
                self.store.ensure_detail_version(ident, payload, content_hash(payload))
                self.store.set_detail_ok(ident, payload)
            else:
                # 暂时无法访问详情：保留最后已知版本，标记未核实，不等于撤回。
                self.store.set_detail_unverified(ident, status)

    # ---- 原件领取（独立事实链）----
    def sync_fulltext(self, identifiers=None):
        identifiers = identifiers or [s['identifier'] for s in self.store.all_state()]
        for ident in identifiers:
            status, body = self.client.get_fulltext(ident)
            if status == 200:
                self.store.set_fulltext(ident, 'retrieved', 200, content=body)
            elif status == 403:
                open_at = None
                try:
                    open_at = json.loads(body).get('open_at')
                except ValueError:
                    pass
                # 限期内：记录来源声明的开放时刻，不猜测、不当作失败。
                self.store.set_fulltext(ident, 'embargoed', 403, open_at=open_at)
            elif status == 410:
                self.store.set_fulltext(ident, 'withdrawn', 410)
            else:
                self.store.set_fulltext(ident, 'unavailable', status)

    # ---- 标识关联：只用来源给出的 dc:relation，题名绝不参与合并 ----
    def resolve_identity(self, cause):
        states = self.store.all_state()
        membership = {s['identifier']: (s['relation'] or s['identifier']) for s in states}
        epoch = self.store.latest_epoch()
        old = self.store.membership(epoch) if epoch else {}
        if epoch is not None and old == membership:
            return []
        new_epoch = self.store.new_epoch(membership, cause)
        events = [{'identifier': ident, 'from_work': old.get(ident), 'to_work': work}
                  for ident, work in membership.items() if old.get(ident) != work]
        if events:
            self.store.add_identity_events(new_epoch, events, cause)
        return events

    def current_work(self, identifier):
        return self.store.membership().get(identifier)
