"""实验室签发页面与独立档案接收台。"""
import base64
from source.common import *

class Site(Provider):
    def initial(self):
        return {'report_revision':1,'revoked':False,'retest':False,'receipts':{},'uploads':{},'partial':False,'lose_reply':False}
    def event(self,d):
        e=d['event']
        if e=='retest':self.state['retest']=True;self.state['report_revision']+=1
        elif e=='revoke':self.state['revoked']=True
        elif e in ['partial','lose_reply']:self.state[e]=True
        elif e=='complete':self.state['partial']=False
        else:return Reply({'error':'unknown_event'},400)
        return {'ok':True}
    def raw(self,name):return b'LAB\x00'+name.encode()+b'\x00synthetic-instrument\xff'
    def reports(self):
        rows=[{'id':'R-A','sample':'s1','configuration':'card-c1/driver-d10/fw-f2','items':['latency'],'status':'revoked' if self.state['revoked'] else 'signed','revision':1,'originals':['latency-1.bin'],'supersedes':[]},
              {'id':'R-J','sample':'s1','configuration':'card-c1/driver-d10/fw-f2','items':['power','thermal'],'status':'signed','revision':1,'originals':['power-1.bin','thermal-1.bin'],'joint':True,'supersedes':[]},
              {'id':'R-C','sample':'s2','configuration':'card-c2/driver-d9/fw-f1','items':['bandwidth','stability'],'status':'signed','revision':1,'originals':['bandwidth-1.bin','stability-1.bin'],'supersedes':[]},
              {'id':'PRE','sample':'s2','configuration':'card-c2/driver-d9/fw-f1','items':['latency'],'status':'preview','revision':1,'originals':['latency-2.bin']}]
        if self.state['retest']:rows.append({'id':'R-B','sample':'s1','configuration':'card-c1/driver-d10/fw-f3','items':['latency'],'status':'signed','revision':2,'originals':['latency-2.bin'],'supersedes':['R-A:latency']})
        return rows
    def get(self,p,q,h):
        if p=='/':
            rows=''.join('<tr><td>'+escaped(r['id'])+'</td><td>'+r['sample']+'</td><td>'+r['status']+'</td><td><a href="/report?id='+r['id']+'">正式依据</a></td></tr>' for r in self.reports())
            return web(page('实验室结果领取','<p>任务结束状态不等于签发状态。企业领取权限：s1、s2。</p><table><tr><th>报告</th><th>样品</th><th>状态</th><th>详情</th></tr>'+rows+'</table><a href="/archive">项目档案台</a>'))
        if p=='/report':
            r=next(x for x in self.reports() if x['id']==q['id']);links=''.join('<p><a class="original" href="/original?name='+n+'">'+n+'</a> SHA256 '+digest(self.raw(n))+'</p>' for n in r['originals'])
            return web(page('签发详情','<pre id="report">'+escaped(dump(r))+'</pre>'+links))
        if p=='/original':
            n=q['name']
            if n not in {n for r in self.reports() for n in r['originals']}:return Reply({'error':'missing'},404)
            return Reply(self.raw(n),mime='application/octet-stream',headers={'Content-Disposition':'attachment; filename="instrument.bin"'})
        if p=='/archive':return web(page('项目档案台','''<p>只接收已签发项目；联合项目需成套。文件名按签发清单填写。</p><label>文件名<input id="filename" value="latency-1.bin"></label><input id="file" type="file"><button id="upload">上传原件</button><pre id="upload-result"></pre><label>移交号<input id="key" value="handoff-1"></label><label>报告号<input id="report" value="R-A"></label><label>项目（逗号分隔）<input id="items" value="latency"></label><button id="submit">归档</button><pre id="result"></pre><a href="/history">归档历史</a>''','''
document.querySelector('#upload').onclick=async()=>{let f=document.querySelector('#file').files[0];let bytes=new Uint8Array(await f.arrayBuffer());let b64=btoa(Array.from(bytes,b=>String.fromCharCode(b)).join(''));let r=await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.querySelector('#filename').value,bytes:b64})});document.querySelector('#upload-result').textContent=await r.text();};
document.querySelector('#submit').onclick=async()=>{let r=await fetch('/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:document.querySelector('#key').value,report:document.querySelector('#report').value,items:document.querySelector('#items').value.split(',')})});document.querySelector('#result').textContent=await r.text();};'''))
        if p=='/history':return web(page('逐项归档历史','<pre id="history">'+escaped(dump(self.state['receipts']))+'</pre>'))
        return super().get(p,q,h)
    def post(self,p,d,h):
        if p=='/upload':
            raw=base64.b64decode(d['bytes']);name=d['name']
            if raw!=self.raw(name):return Reply({'error':'digest_mismatch'},422)
            self.state['uploads'][name]=digest(raw);return {'uploaded':name,'sha256':digest(raw)}
        if p=='/archive':
            if d['key'] in self.state['receipts']:return self.state['receipts'][d['key']]
            r=next(x for x in self.reports() if x['id']==d['report'])
            if r['status']!='signed':return Reply({'error':'not_signed'},409)
            if not set(d['items'])<=set(r['items']) or (r.get('joint') and set(d['items'])!=set(r['items'])):return Reply({'error':'joint_scope'},422)
            if any(n not in self.state['uploads'] for n in r['originals']):return Reply({'error':'missing_original'},422)
            accepted=([] if r.get('joint') else d['items'][:1]) if self.state['partial'] else d['items']
            receipt={'report':r['id'],'revision':r['revision'],'accepted':{i:'ARC-'+d['key']+'-'+i for i in accepted},'pending':list(set(d['items'])-set(accepted))}
            self.state['receipts'][d['key']]=receipt
            if self.state['lose_reply']:self.state['lose_reply']=False;return Reply({'error':'gateway_after_write'},503)
            return receipt
        return super().post(p,d,h)

if __name__=='__main__':main(Site)
