# 高校成果仓储协议

两个独立来源端点 /university-a/oai、/university-b/oai 提供OAI-PMH 2.0 XML：Identify、ListSets、ListRecords、GetRecord；metadataPrefix=oai_dc，日期粒度YYYY-MM-DD。robot与compute集合有交叉，相同题名不同identifier不能合并。dc:relation给显式关联，不等于两个库的原记录已被替换。
ListRecords支持set、from、until，边界包含当日。非空resumptionToken表示尚有下一页，中间页可以没有record但继续给token；分页令牌30秒有效并绑定来源修订。协议错误通过200 XML中的error code=badResumptionToken返回。删除用header status=deleted；集合退出在当前记录的setSpec中体现，不生成整条成果删除。
GET /detail?id=oai:university-a:1返回详情独立修订、元数据公开与全文开放状态。GET /fulltext在限期内403、公开后返回合成原件，撤回后410。控制open、withdraw、move、relation、offline分别改变全文开放/公开撤回/集合成员/关联标识/详情可用性；expire仅使续传凭据过期。除显式删除外，不可用或某集合未见并不等于成果撤回。

本地来源控制通过 POST /__control 发送JSON，例如 {"event":"reset"} 可重置样本。控制切换只影响合成来源，不能用来替业务处理方决定批准、签发或确认。来源服务与自动化客户端分别承担各自责任。
