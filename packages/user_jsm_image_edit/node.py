from __future__ import annotations
import base64, json, os, time, urllib.request, urllib.error, hashlib, uuid
from typing import Any, Dict
from app.assets import Asset
from app.nodespec import NodeBase, NodeSpec, PortSpec, PortType

def _key(ctx,v):
 v=str(v or ''); return (ctx.get('resolve_secret',lambda x:'')(v[8:]) if v.startswith('@secret:') else os.environ.get(v[4:],'') if v.startswith('env:') else v)
def _text(store,a):
 if hasattr(a,'id'):
  d=store.read_json(a.id); return d.get('text','') if isinstance(d,dict) else str(d or '')
 return str(a or '')
def _multipart(url,key,fields,files,timeout=120):
 boundary='----FrameWeave'+uuid.uuid4().hex; chunks=[]
 for k,v in fields.items(): chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()]
 for name,(fn,ctype,data) in files.items(): chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\nContent-Type: {ctype}\r\n\r\n'.encode(),data,b'\r\n']
 body=b''.join(chunks)+f'--{boundary}--\r\n'.encode(); req=urllib.request.Request(url,data=body,method='POST',headers={'Content-Type':f'multipart/form-data; boundary={boundary}','Authorization':'Bearer '+key})
 try:
  with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode())
 except urllib.error.HTTPError as e: raise RuntimeError(f'图像编辑 API HTTP {e.code}: '+e.read().decode(errors='replace')[-800:])
class ImageEditNode(NodeBase):
 @classmethod
 def spec(cls):
  return NodeSpec(type_id='user/jsm_image_edit',title='图生图/图片编辑',category='输出',description='参考图编辑',inputs=[PortSpec('reference_image',PortType.IMAGE,label='参考图'),PortSpec('prompt',PortType.SCRIPT,label='编辑提示词')],outputs=[PortSpec('image',PortType.IMAGE,label='第一张图片'),PortSpec('images',PortType.JSON,label='图片列表'),PortSpec('report',PortType.JSON,label='处理报告')],params=[PortSpec('base_url',PortType.STRING,default='https://api.openai.com/v1'),PortSpec('model',PortType.STRING,default='gpt-image-1'),PortSpec('api_key',PortType.STRING,default='',widget='password'),PortSpec('size',PortType.STRING,default='1024x1024'),PortSpec('count',PortType.INT,default=1,widget='number')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  im=inputs.get('reference_image'); p=inputs.get('prompt'); store=ctx['store'];
  if not im or not im.path or not os.path.exists(im.path): raise RuntimeError('需要参考图片输入')
  prompt=_text(store,p)
  if not prompt.strip(): raise RuntimeError('编辑提示词为空')
  key=_key(ctx,params.get('api_key'))
  if not key: raise RuntimeError('需要 API Key')
  body=_multipart(str(params.get('base_url','https://api.openai.com/v1')).rstrip('/')+'/images/edits',key,{'model':str(params.get('model','gpt-image-1')),'prompt':prompt,'n':str(max(1,min(4,int(params.get('count',1) or 1)))),'size':str(params.get('size','1024x1024'))},{'image':(os.path.basename(im.path),'image/png',open(im.path,'rb').read())})
  data=body.get('data') if isinstance(body,dict) else None
  if not isinstance(data,list) or not data: raise RuntimeError('图像编辑 API 返回空结果')
  assets=[]; records=[]
  for i,item in enumerate(data):
   raw=base64.b64decode(item['b64_json']) if item.get('b64_json') else None
   if raw is None and item.get('url'):
    with urllib.request.urlopen(item['url'],timeout=120) as r: raw=r.read()
   if not raw: raise RuntimeError(f'第 {i+1} 张图片缺少数据')
   path=os.path.join(ctx['workdir'],f'image_edit_{int(time.time()*1000)}_{i}.png'); open(path,'wb').write(raw); a=store.save_asset(Asset(id='',kind='IMAGE',path=path,meta={'prompt':prompt})); assets.append(a); records.append({'index':i,'image_id':a.id,'path':path,'sha256':hashlib.sha256(raw).hexdigest()})
  return {'image':assets[0],'images':{'items':records,'prompt':prompt},'report':{'count':len(records),'endpoint':'images/edits','warnings':[]}}
