from __future__ import annotations
import base64, json, os, time, urllib.request, urllib.error, hashlib
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

def _key(ctx,v):
 v=str(v or '');
 if v.startswith('@secret:'): return ctx.get('resolve_secret',lambda x:'')(v[8:]) or ''
 if v.startswith('env:'): return os.environ.get(v[4:],'')
 return v
def _post(url,key,body,timeout=120):
 req=urllib.request.Request(url,data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,json.loads(r.read().decode())
 except urllib.error.HTTPError as e: raise RuntimeError(f'图像 API HTTP {e.code}: '+e.read().decode(errors='replace')[-800:])
class ImageGenerateNode(NodeBase):
 @classmethod
 def spec(cls):
  return NodeSpec(type_id='user/jsm_image_generate',title='文生图',category='输出',description='OpenAI兼容文生图',inputs=[PortSpec('prompt',PortType.SCRIPT,label='提示词')],outputs=[PortSpec('image',PortType.IMAGE,label='第一张图片'),PortSpec('images',PortType.JSON,label='图片列表'),PortSpec('report',PortType.JSON,label='处理报告')],params=[PortSpec('provider',PortType.STRING,default='openai',widget='select',options=['openai','custom']),PortSpec('base_url',PortType.STRING,default='https://api.openai.com/v1'),PortSpec('model',PortType.STRING,default='gpt-image-1'),PortSpec('api_key',PortType.STRING,default='',widget='password'),PortSpec('aspect_ratio',PortType.STRING,default='1:1',widget='select',options=['1:1','16:9','9:16','4:3','3:4','2.35:1','2:1','1.85:1','9:19.5']),PortSpec('size',PortType.STRING,default='1024x1024'),PortSpec('quality',PortType.STRING,default='auto',widget='select',options=['auto','low','medium','high']),PortSpec('count',PortType.INT,default=1,widget='number')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  prompt=inputs.get('prompt'); prompt=ctx['store'].read_json(prompt.id).get('text','') if hasattr(prompt,'id') and isinstance(ctx['store'].read_json(prompt.id),dict) else str(prompt or '')
  if not prompt.strip(): raise RuntimeError('提示词为空')
  key=_key(ctx,params.get('api_key')); 
  if not key: raise RuntimeError('需要 API Key（使用密码参数或 @secret:NAME）')
  base=str(params.get('base_url','https://api.openai.com/v1')).rstrip('/'); model=str(params.get('model','gpt-image-1')); n=max(1,min(4,int(params.get('count',1) or 1))); ratio=str(params.get('aspect_ratio','1:1')); size=str(params.get('size','1024x1024'))
  body={'model':model,'prompt':prompt,'n':n,'size':size}
  if model.startswith('gpt-image'): body['aspect_ratio']=ratio; body['quality']=str(params.get('quality','auto'))
  st=time.time(); _,resp=_post(base+'/images/generations',key,body); data=resp.get('data') if isinstance(resp,dict) else None
  if not isinstance(data,list) or not data: raise RuntimeError('图像 API 返回空结果')
  assets=[]; records=[]
  for i,item in enumerate(data):
   raw=base64.b64decode(item['b64_json']) if item.get('b64_json') else None
   if raw is None and item.get('url'):
    with urllib.request.urlopen(item['url'],timeout=120) as r: raw=r.read()
   if not raw: raise RuntimeError(f'第 {i+1} 张图片缺少 b64_json/url')
   path=os.path.join(ctx['workdir'],f'image_gen_{int(time.time()*1000)}_{i}.png'); open(path,'wb').write(raw); a=ctx['store'].save_asset(Asset(id='',kind='IMAGE',path=path,meta={'prompt':prompt,'model':model,'aspect_ratio':ratio})); assets.append(a); records.append({'index':i,'image_id':a.id,'path':path,'sha256':hashlib.sha256(raw).hexdigest()})
  return {'image':assets[0],'images':{'items':records,'prompt':prompt,'model':model},'report':{'provider':params.get('provider','openai'),'model':model,'aspect_ratio':ratio,'count':len(records),'elapsed':round(time.time()-st,3),'warnings':[]}}
