"""合成 OAI-PMH 仓储及独立详情、全文授权。"""
import time,uuid
import xml.etree.ElementTree as ET
from source.common import *

NS='http://www.openarchives.org/OAI/2.0/'
DC='http://purl.org/dc/elements/1.1/'
ODC='http://www.openarchives.org/OAI/2.0/oai_dc/'
ET.register_namespace('',NS);ET.register_namespace('dc',DC);ET.register_namespace('oai_dc',ODC)
def node(parent,name,text=None,**attrs):
    n=ET.SubElement(parent,'{'+NS+'}'+name,attrs)
    if text is not None:n.text=str(text)
    return n

class Site(Provider):
    def initial(self):return {'revision':1,'tokens':{},'open':False,'withdraw':False,'moved':False,'relation':'urn:work:alpha','offline':False}
    def event(self,d):
        e=d['event']
        if e=='expire':
            for t in self.state['tokens'].values():t['until']=0
        elif e=='open':self.state['open']=True
        elif e=='withdraw':self.state['withdraw']=True
        elif e=='move':self.state['moved']=True
        elif e=='relation':self.state['relation']='urn:work:beta'
        elif e=='offline':self.state['offline']=True
        else:return Reply({'error':'unknown_event'},400)
        self.state['revision']+=1;return {'ok':True}
    def records(self,repo):
        return [{'id':'oai:'+repo+':1','sets':['robot'] if self.state['moved'] else ['robot','compute'],'date':'2026-11-02','deleted':self.state['withdraw'],'title':'边缘推理研究'},
                {'id':'oai:'+repo+':2','sets':['compute'],'date':'2026-11-02','deleted':False,'title':'边缘推理研究'},
                {'id':'oai:'+repo+':3','sets':['robot'],'date':'2026-11-03','deleted':False,'title':'机器人示教'}]
    def xml(self,q,repo):
        root=ET.Element('{'+NS+'}OAI-PMH');node(root,'responseDate','2026-11-03T12:00:00Z');node(root,'request','http://localhost/oai',verb=q.get('verb',''))
        verb=q.get('verb');s=self.state
        if verb=='Identify':
            n=node(root,'Identify');node(n,'repositoryName','高校 '+repo);node(n,'baseURL','http://localhost/'+repo+'/oai');node(n,'protocolVersion','2.0');node(n,'adminEmail','synthetic@example.test');node(n,'earliestDatestamp','2026-01-01');node(n,'deletedRecord','persistent');node(n,'granularity','YYYY-MM-DD')
        elif verb=='ListSets':
            n=node(root,'ListSets')
            for x in ['robot','compute']:row=node(n,'set');node(row,'setSpec',x);node(row,'setName',x)
        elif verb in ['ListRecords','GetRecord']:
            cursor=0;scope=q.get('set','');since=q.get('from','');until=q.get('until','9999-12-31')
            if q.get('resumptionToken'):
                t=s['tokens'].get(q['resumptionToken'])
                if not t or t['until']<time.time() or t['revision']!=s['revision'] or t['repo']!=repo:
                    node(root,'error','续传凭据失效',code='badResumptionToken');return ET.tostring(root,encoding='utf-8',xml_declaration=True)
                cursor,scope,since,until=t['cursor'],t['scope'],t['since'],t['end']
            elif q.get('metadataPrefix','oai_dc')!='oai_dc':
                node(root,'error','只支持 oai_dc',code='cannotDisseminateFormat');return ET.tostring(root,encoding='utf-8',xml_declaration=True)
            rows=[r for r in self.records(repo) if (not scope or scope in r['sets']) and since<=r['date']<=until and (verb!='GetRecord' or r['id']==q.get('identifier'))]
            n=node(root,verb)
            # 第二页允许空记录但仍有续传，模拟异步过滤窗口。
            offset=cursor if cursor<1 or verb=='GetRecord' else cursor-1
            visible=[] if cursor==1 and verb=='ListRecords' else rows[offset:offset+1]
            for r in visible:
                item=node(n,'record');header=node(item,'header',status='deleted') if r['deleted'] else node(item,'header')
                node(header,'identifier',r['id']);node(header,'datestamp',r['date'])
                for x in r['sets']:node(header,'setSpec',x)
                if not r['deleted']:
                    meta=node(item,'metadata');dc=ET.SubElement(meta,'{'+ODC+'}dc');ET.SubElement(dc,'{'+DC+'}title').text=r['title'];ET.SubElement(dc,'{'+DC+'}relation').text=s['relation'] if r['id'].endswith(':1') else r['id'];ET.SubElement(dc,'{'+DC+'}identifier').text='/detail?id='+r['id']
            next_cursor=cursor+1
            if verb=='ListRecords' and next_cursor<len(rows)+1:
                token=uuid.uuid4().hex;s['tokens'][token]={'cursor':next_cursor,'scope':scope,'since':since,'end':until,'until':time.time()+30,'revision':s['revision'],'repo':repo};node(n,'resumptionToken',token,cursor=str(cursor),completeListSize=str(len(rows)))
            elif verb=='ListRecords':node(n,'resumptionToken','',cursor=str(cursor))
        else:node(root,'error','不支持的 verb',code='badVerb')
        return ET.tostring(root,encoding='utf-8',xml_declaration=True)
    def get(self,p,q,h):
        if p=='/':return web(page('高校成果来源','<p>两个独立仓储：/university-a/oai 与 /university-b/oai；详情 /detail；全文 /fulltext。</p>'))
        if p in ['/university-a/oai','/university-b/oai']:return Reply(self.xml(q,p.split('/')[1]),mime='text/xml; charset=utf-8')
        if p=='/detail':
            if self.state['offline']:return Reply({'error':'temporary_offline'},503)
            return {'id':q['id'],'revision':self.state['revision'],'relation':self.state['relation'],'metadata_public':not self.state['withdraw'],'fulltext_open':self.state['open'] and not self.state['withdraw'],'fulltext':'/fulltext?id='+q['id']}
        if p=='/fulltext':
            if self.state['withdraw']:return Reply({'error':'withdrawn'},410)
            if not self.state['open']:return Reply({'error':'embargo','open_at':'2026-11-05T08:00:00Z'},403)
            return Reply(b'SYNTHETIC RESEARCH ORIGINAL\x00'+q['id'].encode(),mime='application/octet-stream')
        return super().get(p,q,h)

if __name__=='__main__':main(Site)
