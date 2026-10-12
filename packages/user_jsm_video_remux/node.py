from __future__ import annotations
import os,shutil,subprocess
from typing import Any,Dict
from app.assets import Asset
from app.nodespec import NodeBase,NodeSpec,PortSpec,PortType

def _ff():
 from app.plugin import deps
 r=deps.resolve_binary('ffmpeg',env=os.environ)
 return r['path'] if r.get('ready') else 'ffmpeg'
class VideoRemuxNode(NodeBase):
 @classmethod
 def spec(cls): return NodeSpec(type_id='user/jsm_video_remux',title='视频格式转换',category='剪辑',description='流拷贝格式转换',inputs=[PortSpec('video',PortType.VIDEO,label='源视频')],outputs=[PortSpec('formatted',PortType.VIDEO,label='格式视频'),PortSpec('stats',PortType.JSON,label='格式报告')],params=[PortSpec('container',PortType.STRING,default='mkv',widget='select',options=['mp4','mkv','webm','mov','ts','m4v']),PortSpec('remove_metadata',PortType.BOOL,default=True,widget='toggle'),PortSpec('faststart',PortType.BOOL,default=True,widget='toggle'),PortSpec('output_name',PortType.STRING,default='formatted')],version='1.0.0')
 async def run(self,ctx,inputs,params)->Dict[str,Any]:
  v=inputs.get('video');
  if not v or not v.path or not os.path.exists(v.path): raise RuntimeError('需要视频输入')
  c=str(params.get('container','mkv')); out=os.path.join(ctx['workdir'],str(params.get('output_name','formatted') or 'formatted')+'.'+c); cmd=[_ff(),'-y','-i',v.path,'-map','0','-c','copy']
  if bool(params.get('remove_metadata',True)): cmd += ['-map_metadata','-1']
  if c=='mp4' and bool(params.get('faststart',True)): cmd += ['-movflags','+faststart']
  cmd += [out]; r=await ctx['run_tracked'](cmd,timeout=900,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if r.returncode!=0: raise RuntimeError('格式转换失败（流拷贝可能与目标容器不兼容）: '+str(r.stderr or '')[-1000:])
  a=ctx['store'].save_asset(Asset(id='',kind='VIDEO',path=out,meta={'container':c,'remux':True})); return {'formatted':a,'stats':{'container':c,'remux':True,'size':os.path.getsize(out)}}
