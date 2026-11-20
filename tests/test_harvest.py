"""采集端端到端验证：真实 OAI-PMH / 详情 / 原件协议请求。

覆盖：初次采集与空中间页、增量边界去重、续传凭据失效与重新开始、
集合交叉与取消订阅、同日更正与标识关联变化、限期与开放、
删除标记 / 集合退出 / 详情不可用三种事实的区分、撤回后引用失效。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from harvest.service import Receiver
from tests.support import SourceTest

CONFIG = Path(__file__).resolve().parent.parent / 'inputs' / 'request.json'
A1, A2, A3 = 'oai:university-a:1', 'oai:university-a:2', 'oai:university-a:3'
B1 = 'oai:university-b:1'


class HarvestTest(SourceTest):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.receiver = Receiver(self.base, str(Path(self.tmp) / 'harvest.db'))
        self.addCleanup(self.receiver.close)
        self.receiver.load_config(CONFIG)

    def harvest(self, **kwargs):
        return self.receiver.harvest(**kwargs)

    def listed(self, team):
        return {item['identifier']: item for item in self.receiver.list_team(team)}

    # 初次采集：空中间页不判尾，集合交叉，同题不合并，覆盖可证明
    def test_initial_harvest_empty_middle_page_and_coverage(self):
        result = self.harvest()
        runs = {r['set'] + '@' + r['repo']: r for r in result['runs']}
        self.assertTrue(all(r['status'] == 'complete' for r in result['runs']))
        # robot 集合两条记录中间夹一个空页：3 页才到末尾，空页不算结束。
        self.assertEqual(runs['robot@university-a']['pages'], 3)
        team_a, team_b = self.listed('transfer-a'), self.listed('transfer-b')
        # 集合交叉：a:1 同时在 robot/compute；transfer-a 看到 a 库三条 + b 库 robot 两条。
        self.assertEqual(sorted(team_a), sorted([A1, A2, A3, B1, 'oai:university-b:3']))
        self.assertEqual(sorted(team_a[A1]['subscribed_via']), ['compute', 'robot'])
        # transfer-b 只订 compute：看到 a:1, a:2。
        self.assertEqual(sorted(team_b), sorted([A1, A2]))
        # 相同题名不同 identifier 不合并；来源 relation 相同的跨库记录才关联。
        self.assertNotEqual(team_a[A1]['work_key'], team_a[A2]['work_key'])
        self.assertEqual(team_a[A1]['work_key'], 'urn:work:alpha')
        self.assertEqual(team_a[A1]['work_key'], team_a[B1]['work_key'])
        # 覆盖证明：每个 (库, 集合) 都有完整窗口，清单记录均为 verified。
        coverage = self.receiver.coverage()
        self.assertEqual(coverage['pending_records'], [])
        for pair in coverage['pairs']:
            self.assertEqual(pair['status'], 'complete')
            self.assertEqual(pair['windows'][0]['from'], '2026-01-01')
            self.assertEqual(pair['windows'][0]['until'], '2026-11-03')
        self.assertTrue(all(i['coverage'] == 'verified' for i in team_a.values()))

    # 日常接续：增量边界日重复，内容哈希去重，不产生重复版本
    def test_incremental_boundary_overlap_dedupes(self):
        self.harvest()
        before = {i: len(self.receiver.store.revisions_for(i)) for i in (A1, A2, A3)}
        result = self.harvest()
        modes = {r['mode'] for r in result['runs']}
        self.assertEqual(modes, {'incremental'})
        after = {i: len(self.receiver.store.revisions_for(i)) for i in (A1, A2, A3)}
        self.assertEqual(before, after)
        # 证据仍在累积：重复观察写入了新的 observation，但不建新版本。
        self.assertGreater(len(self.receiver.store.observations_for(A3)), 1)
        windows = self.receiver.store.coverage('university-a', 'robot')
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[-1]['from_day'], '2026-11-03')  # 边界日重复

    # 续传凭据失效：运行中止、覆盖不推进、证据保留、其他订阅不清空、允许重新开始
    def test_token_expiry_aborts_then_restart_preserves_others(self):
        fired = []

        def expire_once(info):
            if info['repo'] == 'university-a' and info['set'] == 'robot' and not fired:
                fired.append(info['page'])
                self.control('expire')

        result = self.harvest(page_hook=expire_once)
        robot = next(r for r in result['runs']
                     if r['repo'] == 'university-a' and r['set'] == 'robot')
        self.assertEqual(robot['status'], 'aborted_badResumptionToken')
        self.assertTrue(fired)
        # 失效运行的部分观察保留为证据，但不产生覆盖窗口。
        self.assertEqual(self.receiver.store.coverage('university-a', 'robot'), [])
        self.assertGreater(len(self.receiver.store.runs_for('university-a', 'robot')), 0)
        coverage = self.receiver.coverage()
        self.assertIn('pending', {p['status'] for p in coverage['pairs']})
        # 其他订阅（transfer-b 经 compute）已发布的清单不受中止影响。
        self.assertEqual(sorted(self.listed('transfer-b')), sorted([A1, A2]))
        # 来源允许重新开始：无覆盖则全量重采，其他订阅数据原样保留。
        result = self.harvest()
        self.assertTrue(all(r['status'] == 'complete' for r in result['runs']))
        self.assertEqual(self.receiver.coverage()['pending_records'], [])
        self.assertEqual(sorted(self.listed('transfer-b')), sorted([A1, A2]))
        windows = self.receiver.store.coverage('university-a', 'robot')
        self.assertEqual(len(windows), 1)

    # 同日更正：datestamp 不变内容变化，标识关系改变，引用可解释，原记录不抹掉
    def test_same_day_correction_changes_identity_and_citation_explains(self):
        self.harvest()
        citation_id = self.receiver.cite('transfer-a', A1)
        self.control('relation')  # 来源把 a:1 的 dc:relation 改为 urn:work:beta，datestamp 不变
        # 增量窗口只覆盖边界日 11-03，触及不到 11-02 的更正：目录版本不变，仍待核实；
        # 但详情服务独立修订已经能看到变化（两条事实链独立）。
        self.harvest(mode='incremental')
        self.assertEqual(len(self.receiver.store.revisions_for(A1)), 1)
        detail = self.receiver.store.detail_state(A1)
        self.assertEqual(detail['relation'], 'urn:work:beta')
        status = self.receiver.citation_status(citation_id)
        self.assertEqual(status['work_key_current'], 'urn:work:alpha')  # 目录尚未核实
        # 全量核对后：同日更正进入目录新版本，关联变化，引用给出解释。
        self.harvest(mode='full')
        revisions = self.receiver.store.revisions_for(A1)
        self.assertEqual(len(revisions), 2)
        self.assertEqual({r['datestamp'] for r in revisions}, {'2026-11-02'})
        self.assertNotEqual(revisions[0]['content_hash'], revisions[1]['content_hash'])
        status = self.receiver.citation_status(citation_id)
        self.assertEqual(status['status'], 'valid')
        self.assertEqual(status['work_key_cited'], 'urn:work:alpha')
        self.assertEqual(status['work_key_current'], 'urn:work:beta')
        changed = [n for n in status['notes'] if n['type'] == 'association_changed']
        self.assertTrue(changed and changed[0]['events'])
        # 原来源记录不抹掉：两个版本的观察证据都还在。
        self.assertGreaterEqual(len(self.receiver.store.observations_for(A1)), 2)

    # 题录可引用不代表全文可下载；开放时刻以来源声明为准
    def test_embargo_then_open_tracks_fulltext_independently(self):
        self.harvest()
        citation_id = self.receiver.cite('transfer-a', A1)
        fulltext = self.receiver.store.fulltext(A1)
        self.assertEqual(fulltext['status'], 'embargoed')
        self.assertEqual(fulltext['open_at'], '2026-11-05T08:00:00Z')
        content = self.receiver.citation_content(citation_id)
        self.assertFalse(content['served'])
        self.assertEqual(content['reason'], 'fulltext_embargoed')
        status = self.receiver.citation_status(citation_id)
        self.assertEqual(status['status'], 'valid')  # 题录引用有效
        self.control('open')
        self.harvest()
        fulltext = self.receiver.store.fulltext(A1)
        self.assertEqual(fulltext['status'], 'retrieved')
        self.assertTrue(fulltext['content_hash'])
        self.assertTrue(self.receiver.citation_content(citation_id)['served'])

    # 删除标记、集合退出、详情不可用是三种不同事实
    def test_move_offline_and_deleted_are_distinct_facts(self):
        self.harvest()
        cite_b = self.receiver.cite('transfer-b', A1)
        cite_a2 = self.receiver.cite('transfer-b', A2)
        # 集合退出：a:1 退出 compute，不是删除；transfer-b 清单消失但引用仍有效。
        # 更正落在 11-02 的记录上，需全量核对才能观察到（增量只覆盖边界日）。
        self.control('move')
        self.harvest(mode='full')
        state = self.receiver.store.state(A1)
        self.assertEqual(state['sets'], ['robot'])
        self.assertFalse(state['deleted'])
        self.assertNotIn(A1, self.listed('transfer-b'))
        self.assertIn(A1, self.listed('transfer-a'))
        status = self.receiver.citation_status(cite_b)
        self.assertEqual(status['status'], 'valid')
        self.assertIn('out_of_scope', {n['type'] for n in status['notes']})
        # 详情暂时不可用：保留最后已知版本，标记未核实，不等于撤回。
        self.control('offline')
        self.harvest()
        detail = self.receiver.store.detail_state(A2)
        self.assertEqual(detail['fetch_status'], 'unverified')
        self.assertFalse(self.receiver.store.state(A2)['deleted'])
        status = self.receiver.citation_status(cite_a2)
        self.assertEqual(status['status'], 'valid')
        self.assertIn('detail_unverified', {n['type'] for n in status['notes']})

    # 撤回：删除标记与详情层公开依据撤回分别使引用失效并给出证据
    def test_withdraw_invalidates_citations_with_evidence(self):
        self.harvest()
        cite_a1 = self.receiver.cite('transfer-a', A1)
        cite_a3 = self.receiver.cite('transfer-a', A3)
        self.control('withdraw')
        self.harvest(mode='full')  # 删除标记落在 11-02 的记录上，全量核对予以确认
        # a:1 带显式删除标记：引用失效，停止提供内容，证据指向观察记录。
        self.assertTrue(self.receiver.store.state(A1)['deleted'])
        status = self.receiver.citation_status(cite_a1)
        self.assertEqual(status['status'], 'invalid')
        self.assertEqual(status['reason'], 'withdrawn_deleted_marker')
        self.assertTrue(status['invalid_since'])
        self.assertTrue(status['evidence']['observation_id'])
        content = self.receiver.citation_content(cite_a1)
        self.assertFalse(content['served'])
        self.assertEqual(content['reason'], 'withdrawn_deleted_marker')
        # a:3 无删除标记，但详情声明公开依据撤回：不同事实链，同样停止提供。
        self.assertFalse(self.receiver.store.state(A3)['deleted'])
        status = self.receiver.citation_status(cite_a3)
        self.assertEqual(status['status'], 'invalid')
        self.assertEqual(status['reason'], 'public_basis_withdrawn')
        self.assertEqual(self.receiver.store.fulltext(A3)['status'], 'withdrawn')
        # 撤回后不能新建引用。
        with self.assertRaises(ValueError):
            self.receiver.cite('transfer-a', A3)

    # 订阅调整：取消一个集合不影响另一团队正在使用的共享成果与原件
    def test_unsubscribe_keeps_shared_originals(self):
        self.control('open')
        self.harvest()
        self.assertEqual(self.receiver.store.fulltext(A2)['status'], 'retrieved')
        self.receiver.unsubscribe('transfer-a', 'university-a', 'compute')
        # transfer-a 不再看到 compute 独有的 a:2；transfer-b 照常。
        self.assertNotIn(A2, self.listed('transfer-a'))
        self.assertIn(A2, self.listed('transfer-b'))
        self.assertIn(A1, self.listed('transfer-a'))  # robot 仍在订
        # 原件保留：a:2 仍被 transfer-b 的有效订阅覆盖，不得进入清理候选。
        self.assertEqual(self.receiver.prune()['candidates'], [])
        self.assertIsNotNone(self.receiver.store.fulltext(A2)['content'])
        # 两个团队都退订 compute 后，a:2 才成为可清理对象。
        self.receiver.unsubscribe('transfer-b', 'university-a', 'compute')
        candidates = self.receiver.prune()['candidates']
        self.assertIn(A2, candidates)
        self.receiver.prune(apply=True)
        self.assertIsNone(self.receiver.store.fulltext(A2)['content'])
        # 重新订阅 compute 后 transfer-a 恢复可见（目录与详情记录从未被清空）。
        self.receiver.subscribe('transfer-a', 'university-a', 'compute')
        self.assertIn(A2, self.listed('transfer-a'))


if __name__ == '__main__':
    unittest.main()
