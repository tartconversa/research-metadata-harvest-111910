import xml.etree.ElementTree as E
from tests.support import SourceTest
N={'o':'http://www.openarchives.org/OAI/2.0/'}

class OAITest(SourceTest):
    def test_protocol_token_empty_and_embargo(self):
        path='/university-a/oai?verb=ListRecords&metadataPrefix=oai_dc'
        root=E.fromstring(self.request(path)[1]);token=root.find('.//o:resumptionToken',N).text;self.assertTrue(token)
        second=E.fromstring(self.request(path+'&resumptionToken='+token)[1]);self.assertIsNone(second.find('.//o:record',N));self.assertTrue(second.find('.//o:resumptionToken',N).text)
        self.control('expire');error=E.fromstring(self.request(path+'&resumptionToken='+token)[1]);self.assertEqual(error.find('o:error',N).get('code'),'badResumptionToken')
        self.assertEqual(self.request('/fulltext?id=oai:university-a:1')[0],403);self.control('open');self.assertEqual(self.request('/fulltext?id=oai:university-a:1')[0],200)
        self.control('move');root=E.fromstring(self.request(path)[1]);self.assertEqual([x.text for x in root.findall('.//o:header/o:setSpec',N)],['robot'])
        self.control('withdraw');root=E.fromstring(self.request(path)[1]);self.assertEqual(root.find('.//o:header',N).get('status'),'deleted');self.assertEqual(self.request('/fulltext?id=oai:university-a:1')[0],410)
