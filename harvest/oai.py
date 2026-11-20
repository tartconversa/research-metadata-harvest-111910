"""OAI-PMH 目录、成果详情与原件服务的真实 HTTP 客户端。

只发协议请求、解析协议响应，不做任何业务判断；
覆盖证明、去重、关联等决策都在 harvester/service 层完成。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OAI_NS = 'http://www.openarchives.org/OAI/2.0/'
DC_NS = 'http://purl.org/dc/elements/1.1/'


def _text(node):
    return node.text if node is not None else None


def http_get(url):
    """返回 (status, body)；HTTP 错误状态同样带回响应体，由上层区分事实。"""
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def parse_oai(payload):
    """解析 OAI-PMH 响应：记录列表、续传凭据或协议错误。

    非空 resumptionToken 表示还有下一页；中间页可以没有 record，
    只有凭据缺失（或空凭据）才意味着本次列表结束。
    """
    root = ET.fromstring(payload)
    result = {
        'error': None,
        'message': None,
        'response_date': _text(root.find(f'{{{OAI_NS}}}responseDate')),
        'records': [],
        'token': None,
    }
    error = root.find(f'{{{OAI_NS}}}error')
    if error is not None:
        result['error'] = error.get('code')
        result['message'] = _text(error)
        return result
    container = None
    for verb in ('ListRecords', 'GetRecord'):
        container = root.find(f'{{{OAI_NS}}}{verb}')
        if container is not None:
            break
    if container is None:
        return result
    for record in container.findall(f'{{{OAI_NS}}}record'):
        header = record.find(f'{{{OAI_NS}}}header')
        metadata = record.find(f'{{{OAI_NS}}}metadata')
        result['records'].append({
            'identifier': _text(header.find(f'{{{OAI_NS}}}identifier')),
            'datestamp': _text(header.find(f'{{{OAI_NS}}}datestamp')),
            'deleted': header.get('status') == 'deleted',
            'sets': [node.text for node in header.findall(f'{{{OAI_NS}}}setSpec')],
            'title': _text(metadata.find(f'.//{{{DC_NS}}}title')) if metadata is not None else None,
            'relation': _text(metadata.find(f'.//{{{DC_NS}}}relation')) if metadata is not None else None,
            'record_xml': ET.tostring(record, encoding='unicode'),
        })
    token = container.find(f'{{{OAI_NS}}}resumptionToken')
    if token is not None and token.text:
        result['token'] = token.text
    return result


class SourceClient:
    """对合成来源的协议访问：OAI 目录 + 详情服务 + 原件服务。"""

    def __init__(self, base_url):
        self.base = base_url.rstrip('/')

    def _oai(self, repo, params):
        status, body = http_get(f'{self.base}/{repo}/oai?' + urllib.parse.urlencode(params))
        if status != 200:
            return {'error': f'http_{status}', 'message': body.decode('utf-8', 'replace'),
                    'records': [], 'token': None, 'response_date': None}
        return parse_oai(body)

    def identify(self, repo):
        status, body = http_get(f'{self.base}/{repo}/oai?' + urllib.parse.urlencode({'verb': 'Identify'}))
        root = ET.fromstring(body)
        node = root.find(f'{{{OAI_NS}}}Identify')
        if node is None:
            return {}
        return {child.tag.split('}')[1]: child.text for child in node}

    def list_records(self, repo, set_spec=None, from_day=None, until_day=None, token=None):
        """ListRecords 一页。续传时只带 resumptionToken，其余参数由凭据绑定。"""
        if token:
            params = {'verb': 'ListRecords', 'resumptionToken': token}
        else:
            params = {'verb': 'ListRecords', 'metadataPrefix': 'oai_dc'}
            if set_spec:
                params['set'] = set_spec
            if from_day:
                params['from'] = from_day
            if until_day:
                params['until'] = until_day
        return self._oai(repo, params)

    def get_detail(self, identifier):
        """成果详情服务：返回 (status, payload|None)。503 表示详情暂时不可用。"""
        status, body = http_get(f'{self.base}/detail?' + urllib.parse.urlencode({'id': identifier}))
        if status != 200:
            return status, None
        return status, json.loads(body)

    def get_fulltext(self, identifier):
        """原件服务：返回 (status, body)。403=限期内，410=已撤回，200=合成原件。"""
        return http_get(f'{self.base}/fulltext?' + urllib.parse.urlencode({'id': identifier}))
